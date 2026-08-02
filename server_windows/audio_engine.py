"""
EtherWave Windows Server - Audio Engine

Captures VB-Audio Virtual Cable's "CABLE Output" recording device via WASAPI
shared-mode capture (through `sounddevice`/PortAudio) and streams the raw
float32 PCM out over UDP with a small sequenced header for the client's
jitter buffer to reassemble -- the Windows counterpart of server/audio_engine.py,
which instead shells out to `parec` against a PipeWire null-sink's monitor
source (there is no PipeWire on Windows; see docs/WINDOWS_PORT.md).

Unlike the Linux server, this one DOES depend on sounddevice/PortAudio for
capture: PortAudio's Windows/WASAPI backend natively sees ordinary recording
endpoints (VB-Cable's "CABLE Output" is just that -- a normal WASAPI capture
device, not a PipeWire-protocol object needing a special client), so there is
no `parec`-equivalent subprocess needed at all.

Networking constants (AUDIO_PORT, HEADER_FORMAT, MAGIC) must match the values
in client/audio_player.py exactly, since there is no shared module between
the two independently-deployed applications -- kept byte-for-byte identical
to server/audio_engine.py's copy of the same constants.
"""

import ctypes
import queue
import socket
import struct
import threading
import time
from ctypes import wintypes

import comtypes
import numpy as np
import sounddevice as sd
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

# Channel counts this project's client-side downmix matrices and VU-meter
# labels are defined for (client/audio_player.py's STANDARD_DOWNMIX,
# gui.py's CHANNEL_LABELS). Windows has no PulseAudio-style channel_map
# string to declare here (see CLAUDE.md's "Wire protocol is duplicated"
# section) -- the L/R/C/LFE/RL/RR/SL/SR ordering for each count is enforced
# implicitly by which physical channel VB-Cable's driver assigns each index
# to, confirmed to match this project's expected ordering by direct
# measurement (see docs/WINDOWS_PORT.md).
SUPPORTED_CHANNELS = {2, 3, 4, 6, 8}

# How many queued capture blocks are allowed to sit in the queue before the
# oldest is dropped in favor of the newest -- the WASAPI-capture analog of
# server/audio_engine.py's BACKLOG_TRIM_THRESHOLD_CHUNKS byte-buffer trim.
# Kept small: a WASAPI callback already delivers exactly `blocksize` frames
# per call (PortAudio reblocks internally), so there is no equivalent need
# to accumulate several chunks' worth before draining -- this queue only
# exists to decouple the real-time callback thread from this thread's own
# pacing loop, not to smooth out a bursty producer the way parec's pipe was.
QUEUE_MAXSIZE_CHUNKS = 4

# Stream-open retry policy: measured directly during development that a
# WASAPI capture stream opened on VB-Cable's capture device immediately
# after DefaultDeviceManager.create_sink() just switched the default output
# device can fail once with a spurious PortAudio host error (observed:
# "GetNameFromCategory: usbTerminalGUID = ..." from the WDM-KS backend, on
# a machine with several USB audio devices attached) and then succeed
# cleanly on retry a moment later -- the same category of "device needs a
# beat to settle after a config change" issue that motivated
# server/audio_engine.py's PAREC_LAUNCH_RETRIES for PipeWire.
STREAM_OPEN_RETRIES = 5
STREAM_OPEN_RETRY_DELAY = 0.4


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


# --- MMCSS thread-priority boost (Windows equivalent of the Linux code's
# SCHED_RR request) -----------------------------------------------------
_avrt = ctypes.WinDLL("avrt")
_avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
_avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]


def _boost_thread_priority():
    """Best-effort MMCSS "Pro Audio" boost for this thread -- keeps the
    queue-draining/pacing/sendto loop from being starved by other
    workloads. Safe no-op if it fails, mirroring the Linux code's
    silently-ignored SCHED_RR denial."""
    task_index = wintypes.DWORD(0)
    handle = _avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_index))
    return handle or None


def _revert_thread_priority(handle):
    if handle:
        _avrt.AvRevertMmThreadCharacteristics(handle)


class AudioCaptureThread(QThread):
    """Captures VB-Cable's "CABLE Output" (via WASAPI/sounddevice) and
    streams it over UDP."""

    levels_changed = Signal(list)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    # bytes_sent is declared as `object`, not `int`: Qt/shiboken marshals a
    # plain `int` signal argument into a 32-bit C `int` (max ~2.1 billion),
    # and this counter is an unbounded running total for the life of the
    # stream -- at ~1.15 MB/s (6ch/48kHz/float32) it crosses that limit
    # after ~31 minutes of continuous streaming. `object` passes the Python
    # int through unmarshaled, with no width limit. (Same fix as the Linux
    # server -- see server/audio_engine.py.)
    stats_updated = Signal(int, object)  # packets_sent, bytes_sent

    def __init__(self, channels: int, device_index: int, samplerate: int = 48000,
                 blocksize: int = 240, audio_port: int = AUDIO_PORT,
                 broadcast_address: str = BROADCAST_ADDRESS,
                 subscribers: "SubscriberRegistry" = None, parent=None):
        super().__init__(parent)
        self.channels = channels
        self.device_index = device_index
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
        # the server independently of whatever gain trim the client applies
        # on its own end. See server/audio_engine.py for the same field.
        self._channel_gains = np.ones(channels, dtype=np.float32)
        self._gains_active = False
        self._queue = queue.Queue(maxsize=QUEUE_MAXSIZE_CHUNKS)
        self._input_overflow_count = 0

    def set_channel_gain_db(self, channel_index: int, db: float):
        if not (0 <= channel_index < self.channels):
            return
        db = max(MIN_CHANNEL_GAIN_DB, min(MAX_CHANNEL_GAIN_DB, db))
        self._channel_gains[channel_index] = 10.0 ** (db / 20.0)
        self._gains_active = not np.allclose(self._channel_gains, 1.0)

    def stop(self):
        self._stop_flag = True

    def _callback(self, indata, frames, time_info, status):
        # Runs on PortAudio's own real-time thread -- must never block,
        # allocate unboundedly, or touch Qt. `indata` is owned by
        # PortAudio and reused next call: copy it before storing.
        if status:
            self._input_overflow_count += 1
        block = indata.copy()
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            # Falling behind: drop the OLDEST queued block and keep the
            # newest, mirroring server/audio_engine.py's backlog-trim
            # policy of re-anchoring to fresh data rather than replaying a
            # backlog late -- the client's jitter buffer absorbs the
            # resulting seq gap the same way it absorbs ordinary network
            # jitter.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(block)
            except queue.Full:
                pass

    def _open_stream_with_retry(self):
        """Opens and starts the WASAPI capture stream, retrying on failure
        -- see STREAM_OPEN_RETRIES' comment for why this is needed. Returns
        an already-started stream, or None if every attempt failed (or a
        stop was requested mid-retry)."""
        last_exc = None
        for attempt in range(STREAM_OPEN_RETRIES):
            if self._stop_flag:
                return None
            try:
                stream = sd.InputStream(
                    device=self.device_index,
                    channels=self.channels,
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    dtype="float32",
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                    callback=self._callback,
                )
                stream.start()
                return stream
            except Exception as exc:
                last_exc = exc
                self.status_changed.emit(
                    f"Capture stream failed to start (attempt {attempt + 1}/"
                    f"{STREAM_OPEN_RETRIES}): {exc}; retrying..."
                )
                time.sleep(STREAM_OPEN_RETRY_DELAY)

        self.error_occurred.emit(
            f"Could not start capture from device index {self.device_index} "
            f"after {STREAM_OPEN_RETRIES} attempts. Last error: {last_exc}"
        )
        return None

    def run(self):
        # Measured directly during development: opening a callback-mode
        # WASAPI stream from any thread OTHER than the one that switched
        # the default device (main thread, via DefaultDeviceManager) fails
        # deterministically with the same spurious WDM-KS host error that
        # motivated STREAM_OPEN_RETRIES -- but retries alone never recover
        # from it, only initializing a COM apartment on THIS thread first
        # does. Root cause not fully understood (undocumented WASAPI/COM
        # interaction, possibly around endpoint-change notifications
        # propagating across apartments), but the fix reproduced reliably
        # across repeated tries -- see docs/WINDOWS_PORT.md.
        comtypes.CoInitialize()
        mmcss_handle = _boost_thread_priority()
        try:
            self._run_capture_loop()
        finally:
            _revert_thread_priority(mmcss_handle)
            comtypes.CoUninitialize()

    def _run_capture_loop(self):
        self.status_changed.emit(f"Starting capture of device index {self.device_index}...")
        stream = self._open_stream_with_retry()
        if stream is None:
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as exc:
            self.error_occurred.emit(f"Failed to open UDP socket: {exc}")
            return

        packet_duration = self.blocksize / self.samplerate

        seq = 0
        last_emit = 0.0
        # Paces packet transmission to the real playback rate rather than
        # however fast the queue happens to drain -- same rationale as
        # server/audio_engine.py's identical pacing scheme (see its
        # comments for the measurements that motivated it).
        next_send_time = time.perf_counter()
        destinations = ()
        last_dest_refresh = 0.0

        try:
            self.status_changed.emit(
                f"Streaming {self.channels}ch @ {self.samplerate}Hz, {self.blocksize} frames/packet"
            )
            while not self._stop_flag:
                try:
                    arr = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if self._gains_active:
                    arr = np.clip(arr * self._channel_gains, -1.0, 1.0).astype(SAMPLE_DTYPE)
                frame_bytes = arr.tobytes()

                now = time.perf_counter()
                if now < next_send_time:
                    time.sleep(next_send_time - now)
                elif next_send_time < now - packet_duration:
                    # Fell meaningfully behind -- resync the schedule to
                    # now instead of trying to instantly replay backlog.
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
                            # Concerns exactly one subscriber; never a
                            # reason to tear down the stream for everyone.
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
        except Exception as exc:
            # Anything unexpected here (e.g. the capture device disappearing
            # mid-stream) must surface as a signal, not an unhandled
            # exception inside QThread.run() -- PySide6 only logs those to
            # stderr as "Error calling Python override of QThread::run()"
            # without stopping the thread cleanly or telling the GUI.
            self.error_occurred.emit(f"Capture loop failed: {exc}")
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            sock.close()
            self.status_changed.emit("Capture stopped")
