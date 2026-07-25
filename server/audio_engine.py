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

import os
import select
import socket
import threading
import struct
import subprocess
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

# --- Network protocol constants (must match client/audio_player.py) -------
AUDIO_PORT = 51235
BROADCAST_ADDRESS = "255.255.255.255"
# Clients announce themselves here so audio can be unicast straight to them
# instead of broadcast at the whole LAN. See SubscriberRegistry.
CONTROL_PORT = 51236
SUBSCRIBE_MAGIC = b"EWS1"
# How long a subscription stays valid without being renewed. Clients renew
# roughly every second, so this tolerates a couple of lost renewals before
# the server drops them and (if nobody else is listening) falls back to
# broadcasting.
SUBSCRIBER_TIMEOUT_S = 5.0
# How often the send loop re-reads the subscriber list.
DESTINATION_REFRESH_S = 0.5
MAGIC = b"EWv1"
# magic(4s) | sequence_num(I) | timestamp(d) | channels(B) | frame_count(H)
HEADER_FORMAT = "!4sIdBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
SAMPLE_DTYPE = np.float32
BYTES_PER_SAMPLE = 4

MIN_CHANNEL_GAIN_DB = -24
MAX_CHANNEL_GAIN_DB = 24

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

# How many whole packets' worth of data are allowed to sit in read_buffer
# before it's treated as stale backlog to drop rather than a normal burst.
# See the trim logic in AudioCaptureThread.run() for why this exists.
BACKLOG_TRIM_THRESHOLD_CHUNKS = 3


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

    def _cleanup_stale_modules(self):
        """Unloads any pre-existing 'EtherWave_Sink' null-sink module(s),
        regardless of whether *this* process created them.

        _module_id only tracks what this process instance itself created.
        If a previous instance crashed (or was restarted by systemd's
        Restart=on-failure in the packaged service) without going through
        remove_sink(), its module keeps running as an orphan under the same
        name. PipeWire allows duplicate sink names and resolves them
        ambiguously, so name-based lookups (parec --device=..., an app
        auto-connecting to "EtherWave_Sink") can silently land on the
        orphan instead of the live one -- this was observed directly: a
        RUNNING sink actually in use alongside a SUSPENDED orphan of the
        same name from an earlier crashed instance. Symptoms match exactly
        what users reported: intermittent random distortion (ambiguous
        resolution) and "stream active but nothing plays" (a newly-started
        app landing on the orphan instead of the one actually being
        captured). Running this unconditionally at every create_sink()
        call, independent of in-memory state, makes startup self-healing
        regardless of prior crash history.
        """
        try:
            result = subprocess.run(["pactl", "list", "short", "modules"],
                                     capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return
        target = f"sink_name={self.SINK_NAME}"
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            module_id, module_name, args = fields[0], fields[1], fields[2]
            if module_name == "module-null-sink" and target in args:
                subprocess.run(["pactl", "unload-module", module_id],
                                capture_output=True, timeout=5)

    def create_sink(self, channels: int) -> int:
        if channels not in CHANNEL_MAPS:
            raise ValueError(f"Unsupported channel count: {channels}")
        if self._module_id is not None:
            raise RuntimeError("Sink already active; remove it before creating a new one")

        self._cleanup_stale_modules()

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


class SubscriberRegistry(QThread):
    """Tracks which clients currently want the audio stream.

    The stream used to go to 255.255.255.255, which every device on the LAN
    has to receive and discard -- roughly 1.15 MB/s of it at 6ch/48kHz. That
    is wasteful everywhere and genuinely disruptive over Wi-Fi, where
    broadcast is transmitted at the lowest basic rate.

    Clients now send a tiny renewal datagram here while they are connected,
    and the capture thread unicasts to whoever is currently subscribed. If
    nobody is (an older client, or the renewals being dropped by a firewall
    between the two machines), it falls back to broadcasting exactly as
    before -- so this is a pure improvement rather than a new way to end up
    with no audio.
    """

    subscribers_changed = Signal(int)

    def __init__(self, control_port: int = CONTROL_PORT, parent=None):
        super().__init__(parent)
        self.control_port = control_port
        self._lock = threading.Lock()
        self._subscribers = {}
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def current_destinations(self):
        """IPs whose subscription hasn't expired. Empty means 'broadcast'."""
        cutoff = time.monotonic() - SUBSCRIBER_TIMEOUT_S
        with self._lock:
            expired = [ip for ip, seen in self._subscribers.items() if seen < cutoff]
            for ip in expired:
                del self._subscribers[ip]
            live = list(self._subscribers)
        if expired:
            self.subscribers_changed.emit(len(live))
        return live

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.control_port))
            sock.settimeout(0.5)
        except OSError:
            # Not fatal: without a control channel we simply keep
            # broadcasting, which is what every previous version did.
            return
        try:
            while not self._stop_flag:
                try:
                    data, addr = sock.recvfrom(64)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                if not data.startswith(SUBSCRIBE_MAGIC):
                    continue
                with self._lock:
                    is_new = addr[0] not in self._subscribers
                    self._subscribers[addr[0]] = time.monotonic()
                    count = len(self._subscribers)
                if is_new:
                    self.subscribers_changed.emit(count)
        finally:
            sock.close()


class AudioCaptureThread(QThread):
    """Captures the EtherWave_Sink monitor (via `parec`) and streams it over UDP."""

    levels_changed = Signal(list)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    # bytes_sent is declared as `object`, not `int`: Qt/shiboken marshals a
    # plain `int` signal argument into a 32-bit C `int` (max ~2.1 billion),
    # and this counter is an unbounded running total for the life of the
    # stream -- at ~1.15 MB/s (6ch/48kHz/float32) it crosses that limit
    # after ~31 minutes of continuous streaming. Past that point every
    # single emit() raised OverflowError right inside the packet-pacing
    # loop (see the `next_send_time` scheduling just above), repeatedly,
    # for the rest of the session -- exactly the kind of per-packet
    # overhead that disrupts the pacing this project depends on for
    # smooth playback. `object` passes the Python int through unmarshaled,
    # with no width limit.
    stats_updated = Signal(int, object)  # packets_sent, bytes_sent

    def __init__(self, channels: int, sink_name: str, samplerate: int = 48000,
                 blocksize: int = 240, audio_port: int = AUDIO_PORT,
                 broadcast_address: str = BROADCAST_ADDRESS,
                 subscribers: "SubscriberRegistry" = None, parent=None):
        super().__init__(parent)
        self.channels = channels
        self.sink_name = sink_name
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.audio_port = audio_port
        self.broadcast_address = broadcast_address
        self.subscribers = subscribers
        self._stop_flag = False
        self._packets_sent = 0
        self._bytes_sent = 0
        # Per-source-channel linear gain, applied to captured audio before
        # it's packed into a packet -- lets the source mix be trimmed at
        # the server (e.g. a channel that's just quieter at the capture
        # source) independently of whatever gain trim the client applies
        # on its own end for its physical output channels. A single
        # float32 element write/read isn't behind a lock -- fine here
        # since it's a plain scalar swap, not a multi-step update, and
        # gain changes are infrequent UI actions, not something contended
        # every packet.
        self._channel_gains = np.ones(channels, dtype=np.float32)
        self._gains_active = False  # skips the multiply/clip below when every channel is at 0dB

    def set_channel_gain_db(self, channel_index: int, db: float):
        if not (0 <= channel_index < self.channels):
            return
        db = max(MIN_CHANNEL_GAIN_DB, min(MAX_CHANNEL_GAIN_DB, db))
        self._channel_gains[channel_index] = 10.0 ** (db / 20.0)
        self._gains_active = not np.allclose(self._channel_gains, 1.0)

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
            # Reverted from 20ms back to the original 10ms: raising it was
            # a speculative guard against CPU contention that a controlled
            # test never actually confirmed (synthetic load never budged
            # per-core or aggregate CPU% on the real hardware), while the
            # added latency was real and directly felt during use. Not
            # worth trading confirmed latency for a hypothetical, unproven
            # benefit -- the actual root cause found for the reported
            # glitches was duplicate PipeWire sink instances (see
            # _cleanup_stale_modules), not capture-buffer starvation.
            "--latency-msec=10",
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
            # This attempt's process has already exited, but its stdout/
            # stderr pipe file objects are still open on our end -- close
            # them before the loop replaces `proc` with a new Popen, or
            # each failed attempt leaks a pipe fd pair for the life of this
            # process. Only matters when a retry actually happens (PipeWire
            # occasionally isn't quite ready right after we just
            # (re)created the sink), but repeated Stop/Start cycles hit
            # this path often enough that leaked fds were confirmed
            # accumulating across a session (checked directly via
            # /proc/<pid>/fd).
            proc.stdout.close()
            proc.stderr.close()
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

        # Qt's TimeCriticalPriority above is only a nice-level hint *within*
        # the default SCHED_OTHER scheduling class -- it does not carry real
        # scheduling guarantees. Measured directly: under a demanding
        # concurrent workload (a game saturating several CPU cores), actual
        # packet send spacing showed frequent multi-ms bursts (up to
        # ~100ms) despite that hint already being set and despite the
        # capture pipeline's own backlog/pacing being otherwise healthy --
        # this is OS wake-up scheduling jitter for this specific thread,
        # not anything this app's own logic controls. Requesting a real
        # SCHED_RR policy for this thread fixed it completely in the same
        # measured conditions (send spacing became exactly 5.00ms, zero
        # jitter events, for 100+ consecutive seconds). This only affects
        # this one thread, not the system's default scheduler -- everything
        # else keeps running under normal SCHED_OTHER. Requires the
        # process's RLIMIT_RTPRIO to be nonzero (standard on Linux audio
        # setups via a `95-audio.conf`-style limits.d rule granting the
        # `audio` group rtprio, which is what makes this succeed without
        # root); silently no-ops otherwise, e.g. if run in a context that
        # denies it (observed: blocked under an ad-hoc interactive SSH
        # session's systemd scope, but allowed when run as a proper
        # systemd --user service).
        try:
            os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(10))
        except OSError:
            pass

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
        # Refreshed periodically rather than per packet: the registry takes a
        # lock, and at 200 packets/second that contention buys nothing when
        # subscriptions only change on a human timescale.
        destinations = ()
        last_dest_refresh = 0.0

        try:
            while not self._stop_flag:
                if proc.poll() is not None:
                    stderr_data = proc.stderr.read().decode(errors="ignore").strip() if proc.stderr else ""
                    self.error_occurred.emit(f"parec exited unexpectedly: {stderr_data or 'no output'}")
                    break

                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if not ready:
                    continue

                # Deliberately NOT `read(chunk_bytes - len(read_buffer))`:
                # that caps every single read to at most one chunk's worth,
                # even when parec has far more than that already sitting in
                # the pipe (it delivers in bursts -- see the pacing
                # comment below). read() on a pipe returns as soon as
                # anything is available, up to the requested size, so
                # asking for far more than one chunk is free when only one
                # chunk is actually ready -- but it's exactly what lets a
                # backlog that builds up in the pipe (from this thread
                # being a little slow for even one iteration -- GIL
                # contention with the Qt main thread, a scheduling
                # hiccup) get fully drained in one go instead of being
                # structurally limited to clawing back one chunk per
                # outer-loop iteration forever. Measured directly: with
                # the one-chunk cap, real end-to-end latency (a trigger on
                # this machine to audible output, measured independently
                # of this app on both ends) was ~350ms within minutes of a
                # fresh start and grew to ~900ms over ~80 minutes.
                chunk = proc.stdout.read(chunk_bytes * 16)
                if not chunk:
                    continue
                read_buffer += chunk

                # Drop stale backlog instead of replaying it at the correct
                # rate but shifted in time. Measured directly: even with
                # behind_schedule_ms sitting at ~0 (the pacing loop below is
                # NOT falling behind wall-clock), read_buffer held a rock
                # steady 11-12 chunks (~55-60ms) pending indefinitely. That
                # existing catch-up branch only fires when we fall behind
                # the *schedule*; it can never fire here because the
                # schedule itself is fine -- the data being sent is simply
                # stale, a phase offset baked in by whatever burst parec (or
                # PipeWire handing off its monitor buffer on first connect)
                # delivered before our very first read, which then just
                # perpetuates forever since we drain at exactly the rate
                # it's produced. Trimming down to the single newest chunk
                # whenever backlog exceeds a small allowance (bursts of 2-3
                # chunks together are normal/expected, per the pacing
                # comment above) re-anchors the schedule to now and lets the
                # client's jitter buffer absorb the resulting seq gap the
                # same way it already absorbs an ordinary network hiccup.
                pending_chunks = len(read_buffer) // chunk_bytes
                if pending_chunks > BACKLOG_TRIM_THRESHOLD_CHUNKS:
                    drop_chunks = pending_chunks - 1
                    read_buffer = read_buffer[drop_chunks * chunk_bytes:]
                    seq += drop_chunks
                    next_send_time = time.perf_counter()

                while len(read_buffer) >= chunk_bytes:
                    frame_bytes = read_buffer[:chunk_bytes]
                    read_buffer = read_buffer[chunk_bytes:]

                    arr = np.frombuffer(frame_bytes, dtype=SAMPLE_DTYPE).reshape(self.blocksize, self.channels)
                    if self._gains_active:
                        arr = np.clip(arr * self._channel_gains, -1.0, 1.0).astype(SAMPLE_DTYPE)
                        frame_bytes = arr.tobytes()

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

                    if now - last_dest_refresh > DESTINATION_REFRESH_S:
                        last_dest_refresh = now
                        destinations = tuple(self.subscribers.current_destinations()) \
                            if self.subscribers is not None else ()

                    if destinations:
                        for ip in destinations:
                            try:
                                sock.sendto(packet, (ip, self.audio_port))
                                self._bytes_sent += len(packet)
                            except OSError:
                                # A client that vanished can make the kernel
                                # surface an earlier ICMP unreachable here.
                                # That concerns exactly one subscriber and
                                # resolves itself when its subscription
                                # expires -- never a reason to tear down the
                                # stream for everyone else.
                                pass
                    else:
                        try:
                            sock.sendto(packet, (self.broadcast_address, self.audio_port))
                            self._bytes_sent += len(packet)
                        except OSError as exc:
                            self.error_occurred.emit(f"UDP send failed: {exc}")
                            return
                    seq += 1
                    self._packets_sent += 1

                    now = time.time()
                    if now - last_emit > 0.05:
                        last_emit = now
                        peaks = np.abs(arr).max(axis=0).tolist()
                        self.levels_changed.emit(peaks)
                        self.stats_updated.emit(self._packets_sent, self._bytes_sent)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            # Explicit, not left to GC: this thread (and its `proc`) gets
            # torn down and rebuilt from scratch on every Stop/Start
            # Streaming click. Relying on the Popen object's own __del__ to
            # eventually close these pipes is one more thing that has to
            # happen promptly and reliably across many rapid cycles for
            # this process to stay clean -- closing them here directly
            # removes that dependency entirely.
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            sock.close()
            self.status_changed.emit("Capture stopped")
