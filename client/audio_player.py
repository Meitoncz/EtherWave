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
from collections import deque

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

# Adaptive jitter sizing (opt-in, see JitterBuffer.adaptive_enabled): how
# much to grow the margin on each resync, and how long a clean stretch has
# to last before shrinking it back down by the same step. Kept within the
# same MIN/MAX_JITTER_MS range as the manual slider -- automating movement
# within an already-tested range, not exploring new extremes.
ADAPTIVE_STEP_MS = 5
ADAPTIVE_DECAY_INTERVAL_S = 15.0

# How long since the last push() before a new one is treated as resuming
# after a genuine gap (server stopped sending, not just ordinary network
# jitter) -- used both to reset cleanly in push() and to stop the coarse
# resync guard in pull() from firing on a frozen write frontier. See the
# comments at each use for why a time-based signal is the reliable one.
STALLED_STREAM_S = 0.2

MIN_CHANNEL_GAIN_DB = -24
MAX_CHANNEL_GAIN_DB = 24


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

        self.jitter_ms = jitter_ms
        self.jitter_frames = int(jitter_ms / 1000.0 * samplerate)
        self.base_seq = None
        self.frames_per_packet = None
        self.read_frame = 0
        self._max_written = 0
        self.started = False
        self.underruns = 0
        self.resyncs = 0
        self.packets_received = 0
        # Real-world clock timestamp of the last push() call -- lets both
        # push() and pull() tell "the write frontier is temporarily behind
        # due to clock drift, still an active stream" apart from "no new
        # packets have arrived in a while, e.g. the server stopped
        # streaming" (see STALLED_STREAM_S and its uses in each method).
        self._last_push_time = 0.0
        # Sustained-drift guard, see pull(): a trailing ~2s window of
        # (duration, was_outside_healthy_band) entries, plus running sums
        # so the bad-fraction check is O(1) per pull instead of re-summing
        # the window every call.
        self._drift_window = deque()
        self._drift_window_total = 0.0
        self._drift_window_bad = 0.0
        # Adaptive jitter sizing, opt-in via the GUI checkbox (see pull()):
        # off by default, so a fresh connection behaves exactly like before
        # unless the user asks for it.
        self.adaptive_enabled = False
        self._clean_seconds = 0.0

    def _set_jitter_ms_locked(self, ms: float):
        self.jitter_ms = ms
        self.jitter_frames = int(ms / 1000.0 * self.samplerate)

    def set_jitter_ms(self, ms: float):
        with self._lock:
            self._set_jitter_ms_locked(ms)

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
            now = time.monotonic()
            resumed_after_gap = (now - self._last_push_time) > STALLED_STREAM_S
            self._last_push_time = now
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
            # Originally this compared abs_frame against _max_written minus
            # a full ring -- "a packet landing more than a full ring behind
            # our progress means the sequence numbering itself reset, not
            # just reordering". That's *unreliable* across quick repeated
            # restarts though (verified with a direct reproduction, not
            # just reasoning about it): each successful reset also moves
            # base_seq up to the new stream's own low seq, and if that
            # stream's run is short (rapid Stop/Start cycling), it never
            # builds up enough _max_written for the *next* restart's
            # abs_frame to fall a full ring behind it. Worse, if two
            # restarts both happen to start their seq at 0 (the normal
            # case) while the first only got a few packets in, comparing
            # seq/abs_frame magnitude alone genuinely cannot tell the two
            # streams apart -- there is no threshold that fixes this,
            # confirmed by reproducing it directly.
            #
            # The reliable signal is instead *time*: resumed_after_gap
            # (computed above, before updating _last_push_time) means no
            # packet arrived for STALLED_STREAM_S -- long enough that
            # ordinary network jitter never triggers it, but a genuine
            # Stop-then-Start cycle always does. Once we know a gap
            # happened, there's no need to guess *why* from the numbers:
            # starting clean is correct whether it's a brand new stream or
            # the same one resuming, and costs nothing an ordinary resync
            # doesn't already cost.
            if resumed_after_gap or abs_frame < self._max_written - self.capacity_frames:
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
            old_read_frame = self.read_frame
            did_resync = False

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
            # available racing very negative has two structurally different
            # causes that look identical from the numbers alone: genuine
            # clock drift during active streaming (the write frontier is
            # still advancing, just slower than we're consuming -- resync
            # is correct) vs. the server having stopped sending entirely
            # (_max_written is frozen; there's nothing to resync *to*).
            # Resyncing in the second case sets read_frame to the same
            # frozen position every time, and since nothing ever writes
            # past it again, each subsequent pull() drains forward through
            # those same already-buffered frames and re-triggers this exact
            # guard once it laps -- replaying the last ~jitter_ms of real
            # audio in an endless loop instead of silence. Only resync here
            # if a packet has actually arrived recently.
            stalled = (time.monotonic() - self._last_push_time) > STALLED_STREAM_S
            if not stalled and (available > self.capacity_frames or available < -self.capacity_frames):
                self.resyncs += 1
                self.read_frame = max(0, self._max_written - self.jitter_frames)
                available = self._max_written - self.read_frame
                did_resync = True

            # Sustained-drift guard: catches a persistent small
            # misalignment -- e.g. from a brief real network hiccup that
            # nudges read_frame out of step with the write frontier -- at
            # a magnitude the coarse capacity_frames guard above
            # deliberately ignores (that one's threshold is ~2 seconds on
            # purpose; see its comment). Left alone, a small misalignment
            # like this doesn't correct itself except by incidental
            # server/client clock drift, on a completely unpredictable
            # timescale (seconds to minutes) -- that's the "recovers on
            # its own after a while" behavior this was chasing.
            #
            # This tracks the *fraction* of time spent outside the healthy
            # band over a trailing window, not "how long has it been
            # continuously bad" -- a first version used the latter (reset
            # to zero on any single healthy-looking pull) and measured
            # live cases slipped straight past it: a connection stuck
            # oscillating through a repeating pattern like
            # [0, -240, 240] never accumulates continuous bad time
            # because every third pull looks fine on its own, even though
            # 2 of every 3 pulls are underruns. An ordinary brief blip is a
            # tiny fraction of the window and never approaches the
            # trigger; a connection stuck cycling through a bad pattern
            # like that one is ~67% bad and trips it almost immediately
            # once the window fills. The window is intentionally short
            # (0.5s, not several seconds) to catch and correct a stuck
            # pattern quickly -- the crossfade below is what keeps that
            # correction itself from being an audible click, so there's
            # much less cost to reacting fast.
            healthy_high = self.jitter_frames * 2
            is_bad = not (0 < available <= healthy_high)
            duration = frame_count / self.samplerate
            self._drift_window.append((duration, is_bad))
            self._drift_window_total += duration
            if is_bad:
                self._drift_window_bad += duration
            while self._drift_window_total > 0.5:
                old_duration, old_bad = self._drift_window.popleft()
                self._drift_window_total -= old_duration
                if old_bad:
                    self._drift_window_bad -= old_duration
            # The eviction loop above keeps _drift_window_total resting at
            # or just under 0.5 by construction once the window has filled
            # -- floating-point accumulation of many small additions never
            # lands on exactly 0.5, so gating on ">= 0.5" here would almost
            # never pass (measured live with the previous 2.0s version:
            # stuck at 1.99999999999998 after far more than 2s of
            # continuous data -- same issue, smaller number). 0.45 has
            # enough margin below that resting point to trigger reliably
            # without waiting for a coincidental exact match.
            if (not stalled and self._drift_window_total >= 0.45
                    and self._drift_window_bad / self._drift_window_total > 0.10):
                self.resyncs += 1
                self.read_frame = max(0, self._max_written - self.jitter_frames)
                available = self._max_written - self.read_frame
                did_resync = True
                self._drift_window.clear()
                self._drift_window_total = 0.0
                self._drift_window_bad = 0.0

            # Adaptive jitter sizing (opt-in via the GUI checkbox): a
            # resync is concrete evidence the current margin was too tight
            # for whatever just happened, so grow it -- trading a little
            # more latency for headroom automatically instead of making
            # the user guess a single fixed value that has to suit both
            # calm and bad-network conditions. A long clean stretch with no
            # resync at all is the opposite signal, and shrinks the margin
            # back down (recovering the latency) the same way, one step at
            # a time in each direction rather than jumping straight to a
            # bound.
            if self.adaptive_enabled:
                if did_resync:
                    self._clean_seconds = 0.0
                    if self.jitter_ms < MAX_JITTER_MS:
                        self._set_jitter_ms_locked(min(MAX_JITTER_MS, self.jitter_ms + ADAPTIVE_STEP_MS))
                else:
                    self._clean_seconds += frame_count / self.samplerate
                    if self._clean_seconds >= ADAPTIVE_DECAY_INTERVAL_S and self.jitter_ms > MIN_JITTER_MS:
                        self._clean_seconds = 0.0
                        self._set_jitter_ms_locked(max(MIN_JITTER_MS, self.jitter_ms - ADAPTIVE_STEP_MS))

            if available <= 0:
                self.underruns += 1
                self.read_frame += frame_count
                return out

            n = min(frame_count, available)
            idx = self.read_frame % self.capacity_frames

            if did_resync and old_read_frame != self.read_frame:
                # A resync jumps read_frame to a different ring position
                # instantly -- read cleanly picks up in-sequence audio
                # again, but the jump itself is a hard discontinuity in the
                # waveform (whatever sample value was playing right before
                # it very likely doesn't match whatever comes right after),
                # which is exactly what's audible as a click. Crossfading
                # this one callback's worth of audio between where we
                # *would* have kept reading (old_read_frame) and where we
                # jumped to smooths that discontinuity into a brief blend
                # instead of a hard edge -- the old side may itself be
                # stale/glitchy (that's *why* we're resyncing), but a fade
                # between two imperfect signals reads as far less jarring
                # than an instant switch between them.
                old_idx = old_read_frame % self.capacity_frames
                old_audio = self._read_ring(old_idx, n)
                new_audio = self._read_ring(idx, n)
                fade_in = np.linspace(0.0, 1.0, n, dtype=np.float32).reshape(-1, 1)
                out[:n] = old_audio * (1.0 - fade_in) + new_audio * fade_in
            else:
                out[:n] = self._read_ring(idx, n)

            self.read_frame += frame_count
            return out

    def get_buffered_ms(self) -> float:
        """Currently buffered audio depth, in milliseconds.

        This is the latency this buffering strategy actually adds --
        deliberately not a cross-machine "packet age" measurement (comparing
        this device's clock to a timestamp from the server's clock), which
        would require the two machines' clocks to be synchronized (NTP) to
        mean anything. They aren't assumed to be, and on a LAN the real
        network transit time is sub-millisecond anyway (see CLAUDE.md);
        what actually matters for perceived latency is how much audio is
        sitting in this ring buffer before playback.
        """
        with self._lock:
            if not self.started:
                return 0.0
            available = self._max_written - self.read_frame
            return max(0.0, available / self.samplerate * 1000.0)

    def reset(self):
        with self._lock:
            self.base_seq = None
            self.frames_per_packet = None
            self.read_frame = 0
            self._max_written = 0
            self.started = False
            self.resyncs = 0
            self._drift_window.clear()
            self._drift_window_total = 0.0
            self._drift_window_bad = 0.0
            self._clean_seconds = 0.0
            self._last_push_time = 0.0
            self.ring.fill(0.0)


class NetworkReceiveThread(QThread):
    """Receives UDP audio packets from one server and feeds a JitterBuffer."""

    stats_updated = Signal(float, int, object)  # buffered_ms, packets_received, bytes_received
    error_occurred = Signal(str)

    def __init__(self, server_ip: str, jitter_buffer: JitterBuffer,
                 audio_port: int = AUDIO_PORT, parent=None):
        super().__init__(parent)
        self.server_ip = server_ip
        self.jitter_buffer = jitter_buffer
        self.audio_port = audio_port
        self._stop_flag = False
        self._bytes_received = 0

    def stop(self):
        self._stop_flag = True

    def run(self):
        # Best-effort scheduling hint, mirroring the server's capture
        # thread: favor this thread so a brief load spike elsewhere on the
        # Mac is less likely to delay draining the socket long enough to
        # cause an audible gap. Safe no-op if the OS ignores it.
        self.setPriority(QThread.Priority.TimeCriticalPriority)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            # Request a generous receive buffer: at 6-8ch float32 @ 48kHz
            # this stream runs at roughly 1-1.5 MB/s, and the OS default
            # (often just tens of KB, especially on macOS) can overflow
            # during any brief scheduling hiccup on this thread -- the
            # kernel silently drops what doesn't fit, which is
            # indistinguishable from real network packet loss and shows up
            # as jitter-buffer underruns. The OS clamps this to its own max
            # if 1 MiB isn't allowed; no error either way.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
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

                self._bytes_received += len(data)

                frames = np.frombuffer(payload, dtype=np.float32).reshape(frame_count, channels)
                self.jitter_buffer.push(seq, frame_count, frames)

                now = time.time()
                if now - last_emit > 0.1:
                    last_emit = now
                    self.stats_updated.emit(
                        self.jitter_buffer.get_buffered_ms(), self.jitter_buffer.packets_received,
                        self._bytes_received
                    )
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
        # Per-output-channel linear gain, applied post-remap so it matches
        # what the VU meters and channel labels in the GUI actually
        # represent (physical output channels, not the server's source
        # channels). A single float32 element write/read isn't behind a
        # lock -- fine here since it's a plain scalar swap, not a
        # multi-step update, and gain changes are infrequent UI actions,
        # not something contended every callback.
        self._channel_gains = np.ones(output_channels, dtype=np.float32)
        self._gains_active = False  # skips the multiply/clip below when every channel is at 0dB

    def set_channel_gain_db(self, channel_index: int, db: float):
        if not (0 <= channel_index < self.output_channels):
            return
        db = max(MIN_CHANNEL_GAIN_DB, min(MAX_CHANNEL_GAIN_DB, db))
        self._channel_gains[channel_index] = 10.0 ** (db / 20.0)
        self._gains_active = not np.allclose(self._channel_gains, 1.0)

    @property
    def output_latency_ms(self) -> float:
        """CoreAudio/PortAudio's own actual (negotiated, not requested)
        output buffering, in milliseconds. This sits downstream of the
        JitterBuffer entirely -- get_buffered_ms() only reports what's
        queued in the ring, not what happens to audio after pull() hands
        it to this stream -- so the two need to be added together for a
        real picture of this device's contribution to end-to-end latency."""
        if self._stream is None:
            return 0.0
        try:
            return float(self._stream.latency) * 1000.0
        except (AttributeError, TypeError):
            return 0.0

    def _callback(self, outdata, frames, time_info, status):
        block = self.jitter_buffer.pull(frames)
        remapped = remap_channels(block, self.output_channels)
        if self._gains_active:
            # remapped is always a fresh, single-use buffer for this
            # callback (either pull()'s own np.zeros() or a freshly
            # computed remix), so mutating it in place is safe and avoids
            # allocating extra arrays on this realtime callback thread.
            np.multiply(remapped, self._channel_gains, out=remapped)
            np.clip(remapped, -1.0, 1.0, out=remapped)
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
                # Without an explicit latency, PortAudio/CoreAudio picks its
                # own default output buffer for the device -- on some macOS
                # devices/host APIs that default is "high" latency (can be
                # several hundred ms), stacking invisibly on top of the
                # jitter buffer (5-50ms) this app already manages, and
                # nothing in the GUI's stats reflects it. The symbolic "low"
                # string isn't a fixed small number -- PortAudio maps it to
                # the device's own self-reported "default low output
                # latency", and for a macOS aggregate/virtual device (e.g.
                # combining two independently-clocked USB interfaces via
                # Loopback) that self-reported "low" value can itself still
                # be conservatively large, to cover inter-device clock-drift
                # compensation. Requesting an explicit small number of
                # seconds instead asks CoreAudio for that literal buffer
                # size regardless of what the device claims as its own
                # default, matching what a purpose-built low-latency tool
                # would request.
                latency=self.blocksize / self.samplerate,
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
