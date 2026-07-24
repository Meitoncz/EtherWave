"""
EtherWave Server - Audio Engine

Manages a PipeWire virtual sink ("EtherWave_Sink") on CachyOS/Arch Linux via
`pactl`, captures the sink's monitor stream via `parec` (the PulseAudio-
protocol client tool that ships alongside `pactl` and is fully understood by
PipeWire's pipewire-pulse compatibility layer), and streams the raw float32
PCM out over UDP with a small sequenced header for the client's jitter
buffer to reassemble.

Note: capture deliberately does NOT use sounddevice/PortAudio here. PortAudio's
Linux backend enumerates ALSA hardware devices; a PipeWire null-sink's
".monitor" source is a PulseAudio/PipeWire-protocol object with no
corresponding ALSA device node, so PortAudio can never see it no matter how
many times its device list is rescanned. `parec --device=<sink>.monitor`
talks the same protocol `pactl` already uses to create the sink, so it
reliably finds it.

Networking constants (AUDIO_PORT, HEADER_FORMAT, MAGIC) must match the values
in client/audio_player.py exactly, since there is no shared module between
the two independently-deployed applications.
"""

import select
import socket
import struct
import subprocess
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

# --- Network protocol constants (must match client/audio_player.py) -------
AUDIO_PORT = 51235
BROADCAST_ADDRESS = "255.255.255.255"
MAGIC = b"EWv1"
# magic(4s) | sequence_num(I) | timestamp(d) | channels(B) | frame_count(H)
HEADER_FORMAT = "!4sIdBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
SAMPLE_DTYPE = np.float32
BYTES_PER_SAMPLE = 4

# Channel layout name -> PulseAudio/PipeWire channel_map string
CHANNEL_MAPS = {
    2: "front-left,front-right",
    3: "front-left,front-right,lfe",
    4: "front-left,front-right,rear-left,rear-right",
    6: "front-left,front-right,front-center,lfe,rear-left,rear-right",
    8: "front-left,front-right,front-center,lfe,rear-left,rear-right,side-left,side-right",
}

# parec launch retry policy: PipeWire occasionally needs a beat after
# `pactl load-module` returns before the monitor source is fully queryable
# by other clients.
PAREC_LAUNCH_RETRIES = 5
PAREC_LAUNCH_RETRY_DELAY = 0.4
PAREC_STARTUP_GRACE = 0.3


class PipeWireSinkManager:
    """Creates/destroys a null-sink virtual device via `pactl` subprocess calls."""

    SINK_NAME = "EtherWave_Sink"

    def __init__(self):
        self._module_id = None
        self._previous_default_sink = None

    @property
    def monitor_source_name(self) -> str:
        return f"{self.SINK_NAME}.monitor"

    @property
    def is_active(self) -> bool:
        return self._module_id is not None

    def create_sink(self, channels: int) -> int:
        if channels not in CHANNEL_MAPS:
            raise ValueError(f"Unsupported channel count: {channels}")
        if self._module_id is not None:
            raise RuntimeError("Sink already active; remove it before creating a new one")

        try:
            result = subprocess.run(
                ["pactl", "get-default-sink-name"],
                capture_output=True, text=True, check=True, timeout=5,
            )
            self._previous_default_sink = result.stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            self._previous_default_sink = None

        channel_map = CHANNEL_MAPS[channels]
        cmd = [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={self.SINK_NAME}",
            f"sink_properties=device.description={self.SINK_NAME}",
            f"channels={channels}",
            f"channel_map={channel_map}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
        self._module_id = int(result.stdout.strip())

        try:
            subprocess.run(["pactl", "set-default-sink", self.SINK_NAME],
                            check=True, timeout=5, capture_output=True)
        except (subprocess.SubprocessError, OSError):
            pass

        return self._module_id

    def _module_is_loaded(self, module_id: int) -> bool:
        try:
            result = subprocess.run(["pactl", "list", "short", "modules"],
                                     capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return True  # can't verify -- assume it's still there, be conservative
        target = str(module_id)
        return any(line.split("\t", 1)[0] == target for line in result.stdout.splitlines())

    def remove_sink(self):
        if self._module_id is not None:
            try:
                subprocess.run(["pactl", "unload-module", str(self._module_id)],
                                check=True, timeout=5, capture_output=True)
            except (subprocess.SubprocessError, OSError) as exc:
                # If the module is genuinely still loaded, keep _module_id
                # set rather than clearing it: forgetting it here would let
                # the next create_sink() spawn a duplicate on top of it. But
                # if it's already gone (removed externally, e.g. by hand
                # while debugging, or PipeWire itself dropped it), clearing
                # our stale reference is correct -- otherwise we'd be stuck
                # permanently refusing to start streaming again over a sink
                # that no longer exists.
                if self._module_is_loaded(self._module_id):
                    raise RuntimeError(
                        f"Failed to unload sink module {self._module_id}: {exc}"
                    ) from exc
            self._module_id = None

        if self._previous_default_sink:
            try:
                subprocess.run(["pactl", "set-default-sink", self._previous_default_sink],
                                check=True, timeout=5, capture_output=True)
            except (subprocess.SubprocessError, OSError):
                pass
            self._previous_default_sink = None


class AudioCaptureThread(QThread):
    """Captures the EtherWave_Sink monitor (via `parec`) and streams it over UDP."""

    levels_changed = Signal(list)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    stats_updated = Signal(int, int)  # packets_sent, bytes_sent

    def __init__(self, channels: int, sink_name: str, samplerate: int = 48000,
                 blocksize: int = 240, audio_port: int = AUDIO_PORT,
                 broadcast_address: str = BROADCAST_ADDRESS, parent=None):
        super().__init__(parent)
        self.channels = channels
        self.sink_name = sink_name
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.audio_port = audio_port
        self.broadcast_address = broadcast_address
        self._stop_flag = False
        self._packets_sent = 0
        self._bytes_sent = 0

    def stop(self):
        self._stop_flag = True

    def _launch_parec(self):
        # --channel-map must be given explicitly and match the sink's own
        # channel_map exactly. Without it, parec assumes PulseAudio's default
        # channel-map for the given --channels count, which can differ from
        # the sink's actual layout; PipeWire then silently inserts a
        # channel remix to reconcile the mismatch, scrambling channel
        # identity (e.g. R ends up carrying LFE's content) even though byte
        # count and format both look correct.
        cmd = [
            "parec",
            f"--device={self.sink_name}.monitor",
            "--format=float32le",
            f"--rate={self.samplerate}",
            f"--channels={self.channels}",
            f"--channel-map={CHANNEL_MAPS[self.channels]}",
            # A bit more headroom than the theoretical minimum: gives our
            # own read loop (below) more slack to fall behind briefly
            # (e.g. during a CPU-heavy moment elsewhere on the system,
            # like shader compilation) before parec's own client buffer
            # overflows and drops samples at the source -- an xrun there
            # is real, unrecoverable audio loss, not just added latency.
            "--latency-msec=20",
        ]
        last_stderr = ""
        for attempt in range(PAREC_LAUNCH_RETRIES):
            if self._stop_flag:
                return None
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            except FileNotFoundError:
                self.error_occurred.emit(
                    "`parec` not found. It ships with the same package as `pactl` "
                    "(libpulse / pulseaudio-utils) — install it alongside PipeWire."
                )
                return None
            except OSError as exc:
                self.error_occurred.emit(f"Failed to launch parec: {exc}")
                return None

            time.sleep(PAREC_STARTUP_GRACE)
            if proc.poll() is None:
                return proc  # still running past the grace period: looks healthy

            last_stderr = proc.stderr.read().decode(errors="ignore").strip()
            self.status_changed.emit(
                f"parec exited immediately (attempt {attempt + 1}/{PAREC_LAUNCH_RETRIES}), retrying..."
            )
            time.sleep(PAREC_LAUNCH_RETRY_DELAY)

        self.error_occurred.emit(
            f"Could not start capture from '{self.sink_name}.monitor' via parec. "
            f"Last error: {last_stderr or 'unknown'}"
        )
        return None

    def run(self):
        # Best-effort: ask the OS scheduler to favor this thread over
        # normal-priority work (e.g. a game compiling shaders on other
        # cores), so reading parec's pipe is less likely to be delayed
        # long enough to cause an audible gap. Purely a scheduling hint --
        # safe no-op if the platform/OS ignores it.
        self.setPriority(QThread.Priority.TimeCriticalPriority)

        self.status_changed.emit(f"Starting capture of '{self.sink_name}.monitor'...")
        proc = self._launch_parec()
        if proc is None:
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as exc:
            self.error_occurred.emit(f"Failed to open UDP socket: {exc}")
            proc.terminate()
            return

        self.status_changed.emit(
            f"Streaming {self.channels}ch @ {self.samplerate}Hz, {self.blocksize} frames/packet"
        )

        bytes_per_frame = self.channels * BYTES_PER_SAMPLE
        chunk_bytes = self.blocksize * bytes_per_frame
        packet_duration = self.blocksize / self.samplerate

        seq = 0
        last_emit = 0.0
        read_buffer = b""
        # Paces packet transmission to the real playback rate rather than
        # however parec happens to deliver data. Measured directly on this
        # server: parec/our own read loop deliver data in ~10ms bursts (two
        # packets ~2us apart, then a gap), not a smooth 5ms trickle, even
        # though the *average* rate is correct. Sending a burst back-to-back
        # instead of spacing it out stresses the client's jitter buffer with
        # irregular arrival timing, causing underruns that look like random
        # network jitter but aren't. next_send_time schedules each packet at
        # its intended real-time slot; if we're running a burst early we
        # sleep to the schedule, and if we've genuinely fallen behind we
        # catch up without trying to replay a backlog instantly.
        next_send_time = time.perf_counter()

        try:
            while not self._stop_flag:
                if proc.poll() is not None:
                    stderr_data = proc.stderr.read().decode(errors="ignore").strip() if proc.stderr else ""
                    self.error_occurred.emit(f"parec exited unexpectedly: {stderr_data or 'no output'}")
                    break

                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                chunk = proc.stdout.read(chunk_bytes - len(read_buffer))
                if not chunk:
                    continue
                read_buffer += chunk

                while len(read_buffer) >= chunk_bytes:
                    frame_bytes = read_buffer[:chunk_bytes]
                    read_buffer = read_buffer[chunk_bytes:]

                    now = time.perf_counter()
                    if now < next_send_time:
                        time.sleep(next_send_time - now)
                    elif next_send_time < now - packet_duration:
                        # Fell meaningfully behind (real stall, not just a
                        # small burst) -- resync the schedule to now instead
                        # of trying to instantly replay the backlog.
                        next_send_time = now
                    next_send_time += packet_duration

                    header = struct.pack(HEADER_FORMAT, MAGIC, seq & 0xFFFFFFFF,
                                          time.time(), self.channels, self.blocksize)
                    packet = header + frame_bytes
                    try:
                        sock.sendto(packet, (self.broadcast_address, self.audio_port))
                    except OSError as exc:
                        self.error_occurred.emit(f"UDP send failed: {exc}")
                        return
                    seq += 1
                    self._packets_sent += 1
                    self._bytes_sent += len(packet)

                    now = time.time()
                    if now - last_emit > 0.05:
                        last_emit = now
                        arr = np.frombuffer(frame_bytes, dtype=SAMPLE_DTYPE).reshape(self.blocksize, self.channels)
                        peaks = np.abs(arr).max(axis=0).tolist()
                        self.levels_changed.emit(peaks)
                        self.stats_updated.emit(self._packets_sent, self._bytes_sent)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            sock.close()
            self.status_changed.emit("Capture stopped")
