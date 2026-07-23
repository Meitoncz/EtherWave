"""
EtherWave Client - Audio Player

Receives the raw float32 PCM UDP stream from a chosen EtherWave server,
reassembles it into order through a ring-buffer based jitter buffer (5-50ms,
adjustable live), remaps it to the selected output device's channel count,
and plays it back through sounddevice/CoreAudio.

Networking constants (AUDIO_PORT, HEADER_FORMAT, MAGIC) must match the values
in server/audio_engine.py exactly, since there is no shared module between
the two independently-deployed applications.
"""

import socket
import struct
import threading
import time

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QThread, QObject, Signal

# --- Network protocol constants (must match server/audio_engine.py) -------
AUDIO_PORT = 51235
MAGIC = b"EWv1"
# magic(4s) | sequence_num(I) | timestamp(d) | channels(B) | frame_count(H)
HEADER_FORMAT = "!4sIdBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
BYTES_PER_SAMPLE = 4

DEFAULT_JITTER_MS = 20
MIN_JITTER_MS = 5
MAX_JITTER_MS = 50


def _downmix_matrix(rows):
    return np.array(rows, dtype=np.float32)


# Standard channel layouts, in the order the server packs them:
#   2: L,R              3: L,R,LFE            4: L,R,RL,RR
#   6: L,R,C,LFE,RL,RR  8: L,R,C,LFE,RL,RR,SL,SR
# Downmix coefficients follow common ITU-R BS.775-style practice (center and
# rears folded into L/R at -3dB / 0.707 gain).
STANDARD_DOWNMIX = {
    (3, 2): _downmix_matrix([[1, 0, 0.3], [0, 1, 0.3]]),
    (4, 2): _downmix_matrix([[1, 0, 0.707, 0], [0, 1, 0, 0.707]]),
    (6, 2): _downmix_matrix([
        [1, 0, 0.707, 0, 0.707, 0],
        [0, 1, 0.707, 0, 0, 0.707],
    ]),
    (8, 2): _downmix_matrix([
        [1, 0, 0.707, 0, 0.707, 0, 0.707, 0],
        [0, 1, 0.707, 0, 0, 0.707, 0, 0.707],
    ]),
    (6, 4): _downmix_matrix([
        [1, 0, 0.5, 0, 0, 0],
        [0, 1, 0.5, 0, 0, 0],
        [0, 0, 0, 0, 1, 0],
        [0, 0, 0, 0, 0, 1],
    ]),
    (8, 6): _downmix_matrix([
        [1, 0, 0, 0, 0, 0, 0, 0],
        [0, 1, 0, 0, 0, 0, 0, 0],
        [0, 0, 1, 0, 0, 0, 0, 0],
        [0, 0, 0, 1, 0, 0, 0, 0],
        [0, 0, 0, 0, 1, 0, 0.7, 0],
        [0, 0, 0, 0, 0, 1, 0, 0.7],
    ]),
    (8, 4): _downmix_matrix([
        [1, 0, 0.707, 0, 0, 0, 0, 0],
        [0, 1, 0.707, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 1, 0, 0.7, 0],
        [0, 0, 0, 0, 0, 1, 0, 0.7],
    ]),
}


def remap_channels(frames: np.ndarray, dst_channels: int) -> np.ndarray:
    """Down/up-mix a (frame_count, src_channels) block to dst_channels."""
    src_channels = frames.shape[1]
    if src_channels == dst_channels:
        return frames

    if dst_channels == 1:
        return frames.mean(axis=1, keepdims=True).astype(np.float32)

    matrix = STANDARD_DOWNMIX.get((src_channels, dst_channels))
    if matrix is not None:
        out = frames @ matrix.T
        return np.clip(out, -1.0, 1.0).astype(np.float32)

    if dst_channels > src_channels:
        out = np.zeros((frames.shape[0], dst_channels), dtype=np.float32)
        out[:, :src_channels] = frames
        return out

    # Generic fallback: fold extra source channels cyclically into the
    # available destination channels and average.
    out = np.zeros((frames.shape[0], dst_channels), dtype=np.float32)
    counts = np.zeros(dst_channels, dtype=np.float32)
    for src_ch in range(src_channels):
        dst_ch = src_ch % dst_channels
        out[:, dst_ch] += frames[:, src_ch]
        counts[dst_ch] += 1
    counts[counts == 0] = 1
    out /= counts
    return np.clip(out, -1.0, 1.0).astype(np.float32)


class JitterBuffer:
    """Absolute-position ring buffer that reassembles out-of-order UDP audio.

    Incoming packets are written at the ring position implied by their
    sequence number (seq * frames_per_packet), so reordering resolves itself
    naturally and duplicate packets simply overwrite the same slot. Playback
    reads lag the newest write by `jitter_ms` worth of frames; if a read
    catches up to (or laps) the write position, silence is emitted for that
    slice rather than blocking.
    """

    def __init__(self, channels: int, samplerate: int = 48000,
                 jitter_ms: float = DEFAULT_JITTER_MS, capacity_seconds: float = 2.0):
        self.channels = channels
        self.samplerate = samplerate
        self.capacity_frames = int(capacity_seconds * samplerate)
        self.ring = np.zeros((self.capacity_frames, channels), dtype=np.float32)
        self._lock = threading.Lock()

        self.jitter_frames = int(jitter_ms / 1000.0 * samplerate)
        self.base_seq = None
        self.frames_per_packet = None
        self.read_frame = 0
        self._max_written = 0
        self.started = False
        self.underruns = 0
        self.resyncs = 0
        self.packets_received = 0

    def set_jitter_ms(self, ms: float):
        with self._lock:
            self.jitter_frames = int(ms / 1000.0 * self.samplerate)

    def _write_ring(self, start_idx: int, data: np.ndarray):
        n = data.shape[0]
        end = start_idx + n
        if end <= self.capacity_frames:
            self.ring[start_idx:end] = data
        else:
            first = self.capacity_frames - start_idx
            self.ring[start_idx:] = data[:first]
            self.ring[:end - self.capacity_frames] = data[first:]

    def _read_ring(self, start_idx: int, n: int) -> np.ndarray:
        end = start_idx + n
        if end <= self.capacity_frames:
            return self.ring[start_idx:end].copy()
        first = self.capacity_frames - start_idx
        return np.concatenate([self.ring[start_idx:], self.ring[:end - self.capacity_frames]], axis=0)

    def push(self, seq: int, frame_count: int, frames: np.ndarray):
        with self._lock:
            if self.frames_per_packet is None:
                self.frames_per_packet = frame_count
            if self.base_seq is None:
                self.base_seq = seq

            abs_frame = (seq - self.base_seq) * self.frames_per_packet

            # A fresh AudioCaptureThread restarts its own sequence counter
            # at 0 every time (e.g. Stop Streaming then Start Streaming
            # again). Comparing the new abs_frame against 0 doesn't catch
            # this: base_seq was anchored to *this* connection's first-ever
            # packet, so a restarted stream's early seq values often land
            # back near 0 relative to that same base_seq too -- small
            # *positive* abs_frame, not negative. And because _max_written
            # only ever grows (max()), it stays stuck at the old stream's
            # high-water mark while these low-abs_frame packets get written
            # into ring slots the read position lapped long ago: prolonged
            # silence, then garbled/discontinuous audio once the new
            # stream's own counter happens to grow past the old one.
            #
            # The real signal is a packet landing more than a full ring
            # behind our already-known progress (_max_written), not just
            # behind zero -- that's not ordinary reordering (at most a few
            # packets late), it means the sequence numbering itself reset.
            # Start clean instead of trying to reconcile old and new.
            if abs_frame < self._max_written - self.capacity_frames:
                self.base_seq = seq
                self.read_frame = 0
                self._max_written = 0
                self.started = False
                self.ring.fill(0.0)
                abs_frame = 0

            if abs_frame < 0 or frame_count > self.capacity_frames:
                return

            idx = abs_frame % self.capacity_frames
            self._write_ring(idx, frames)
            self._max_written = max(self._max_written, abs_frame + frame_count)
            self.packets_received += 1

    def pull(self, frame_count: int) -> np.ndarray:
        with self._lock:
            out = np.zeros((frame_count, self.channels), dtype=np.float32)

            # Anchor the read position on the first actual playback pull, not
            # on network arrival: sd.OutputStream can take an arbitrary
            # (device-dependent) amount of time between start() and its first
            # real callback, and anchoring too early would bake that delay in
            # as permanent, uncorrectable latency.
            if not self.started:
                if self._max_written >= self.jitter_frames:
                    self.read_frame = max(0, self._max_written - self.jitter_frames)
                    self.started = True
                else:
                    return out

            available = self._max_written - self.read_frame

            # Clock-drift guard: the server's capture clock and this
            # device's playback clock are independent oscillators and will
            # never match exactly, so read_frame slowly drifts relative to
            # the write frontier even under perfect network conditions. If
            # it falls far enough behind, it starts reading ring positions
            # that have since been overwritten by newer packets -- stale,
            # discontinuous samples that sound like crackling, not silence.
            # If it races too far ahead, it can end up permanently chasing a
            # write frontier it never catches up to. Either way, resync back
            # to the configured jitter offset behind the current write
            # frontier rather than let the drift compound.
            #
            # Both thresholds use capacity_frames (the full ring, seconds),
            # not jitter_frames (tens of ms): jitter_frames is far too tight
            # for the "racing ahead" side -- a handful of ordinary,
            # already-gracefully-handled underruns from routine network
            # jitter crosses it easily, which was firing resyncs (each one
            # an audible click) on normal network hiccups instead of only on
            # genuine sustained clock drift, sounding like stuttering rather
            # than fixing it.
            if available > self.capacity_frames or available < -self.capacity_frames:
                self.resyncs += 1
                self.read_frame = max(0, self._max_written - self.jitter_frames)
                available = self._max_written - self.read_frame

            if available <= 0:
                self.underruns += 1
                self.read_frame += frame_count
                return out

            n = min(frame_count, available)
            idx = self.read_frame % self.capacity_frames
            out[:n] = self._read_ring(idx, n)
            self.read_frame += frame_count
            return out

    def reset(self):
        with self._lock:
            self.base_seq = None
            self.frames_per_packet = None
            self.read_frame = 0
            self._max_written = 0
            self.started = False
            self.resyncs = 0
            self.ring.fill(0.0)


class NetworkReceiveThread(QThread):
    """Receives UDP audio packets from one server and feeds a JitterBuffer."""

    stats_updated = Signal(float, int)  # latency_ms, packets_received
    error_occurred = Signal(str)

    def __init__(self, server_ip: str, jitter_buffer: JitterBuffer,
                 audio_port: int = AUDIO_PORT, parent=None):
        super().__init__(parent)
        self.server_ip = server_ip
        self.jitter_buffer = jitter_buffer
        self.audio_port = audio_port
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.audio_port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error_occurred.emit(f"Failed to bind audio socket: {exc}")
            return

        last_emit = 0.0
        last_mismatch_warning = 0.0
        try:
            while not self._stop_flag:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue

                if addr[0] != self.server_ip:
                    continue
                if len(data) < HEADER_SIZE:
                    continue

                magic, seq, ts, channels, frame_count = struct.unpack(
                    HEADER_FORMAT, data[:HEADER_SIZE]
                )
                if magic != MAGIC:
                    continue

                payload = data[HEADER_SIZE:]
                expected_bytes = frame_count * channels * BYTES_PER_SAMPLE
                if len(payload) != expected_bytes:
                    continue

                # The server's channel layout can change after we connected
                # (e.g. auto-connected to an idle server, then the layout was
                # changed before streaming started). The jitter buffer's ring
                # is sized for a fixed channel count, so a stream-level
                # reconnect is required to pick up the new size -- pushing a
                # mismatched shape into it would raise. Drop and surface it
                # instead of crashing this thread silently.
                if channels != self.jitter_buffer.channels:
                    now = time.time()
                    if now - last_mismatch_warning > 2.0:
                        last_mismatch_warning = now
                        self.error_occurred.emit(
                            f"Server is now sending {channels}ch but this connection expects "
                            f"{self.jitter_buffer.channels}ch — reconnect to pick up the change."
                        )
                    continue

                frames = np.frombuffer(payload, dtype=np.float32).reshape(frame_count, channels)
                self.jitter_buffer.push(seq, frame_count, frames)

                now = time.time()
                if now - last_emit > 0.1:
                    last_emit = now
                    latency_ms = max(0.0, (now - ts) * 1000.0)
                    self.stats_updated.emit(latency_ms, self.jitter_buffer.packets_received)
        finally:
            sock.close()


class AudioOutputStream(QObject):
    """Wraps a sounddevice OutputStream, pulling audio from a JitterBuffer
    and remapping it to the output device's channel count on every callback."""

    levels_changed = Signal(list)
    underrun_updated = Signal(int)
    error_occurred = Signal(str)

    def __init__(self, device_index, output_channels: int, jitter_buffer: JitterBuffer,
                 samplerate: int = 48000, blocksize: int = 240, parent=None):
        super().__init__(parent)
        self.device_index = device_index
        self.output_channels = output_channels
        self.jitter_buffer = jitter_buffer
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._stream = None
        self._last_emit = 0.0

    def _callback(self, outdata, frames, time_info, status):
        block = self.jitter_buffer.pull(frames)
        remapped = remap_channels(block, self.output_channels)
        outdata[:] = remapped

        now = time.time()
        if now - self._last_emit > 0.05:
            self._last_emit = now
            peaks = np.abs(remapped).max(axis=0).tolist()
            self.levels_changed.emit(peaks)
            self.underrun_updated.emit(self.jitter_buffer.underruns)

    def start(self):
        try:
            self._stream = sd.OutputStream(
                device=self.device_index,
                channels=self.output_channels,
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self.error_occurred.emit(f"Audio output error: {exc}")

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
