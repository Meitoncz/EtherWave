# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

EtherWave streams uncompressed multichannel audio (2.0 up to 7.1 surround)
over LAN from a CachyOS/Arch Linux PipeWire server to a macOS/CoreAudio
client, with UDP auto-discovery. Two independent PySide6 (Qt6) desktop apps
— `server/` and `client/` — talk over a small custom UDP protocol; there is
no shared Python module between them (the wire format is intentionally
duplicated in both, see below).

A Windows 11 port of the server (`server_windows/`, mirroring `server/`,
not sharing code with it, same as `client/` doesn't) exists and has been
live-tested end-to-end against a real macOS client. It captures VB-Audio
Virtual Cable's "CABLE Output" via WASAPI (`sounddevice`) instead of
PipeWire/`parec`, and switches the Windows default output device via the
undocumented COM `IPolicyConfig` interface instead of `pactl
set-default-sink`. If you're working on it, read
[`docs/WINDOWS_PORT.md`](docs/WINDOWS_PORT.md) first — it documents several
non-obvious bugs (a WASAPI stream-open failure specific to background
threads right after a default-device switch, chief among them) that were
only found by live testing on real Windows 11 hardware, not guessable from
the Linux side or from reading the `sounddevice`/WASAPI docs alone.

## Commands

There is no formal test suite (no pytest/unittest files) and no linter
configured. Verification for this project means: byte-compile, headlessly
construct both GUIs, and — for anything touching the audio path — actually
measure behavior (see "Debugging methodology" below).

```bash
# Syntax check
python -m py_compile server/*.py client/*.py

# Headless construction smoke test (mirrors .github/workflows/ci.yml) --
# catches import-time and __init__-time bugs without needing a real
# display, PipeWire, or audio hardware
QT_QPA_PLATFORM=offscreen python -c "
import sys
sys.path.insert(0, 'server')
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from gui import ServerMainWindow
ServerMainWindow()
print('server OK')
"
# (swap sys.path entry + import to 'client' / ClientMainWindow for the client;
# see ci.yml for the exact two-step version that avoids module-name collisions
# between server/gui.py and client/gui.py both being importable as 'gui')

# Run from source
cd server && python main.py
cd client && python main.py

# Regenerate the icon (only needed if assets/generate_icon.py changes)
python assets/generate_icon.py

# Build/install packages locally
cd packaging/arch && makepkg -si          # Arch: build + install
cd packaging/macos && ./build.sh && ./install.sh   # macOS: build .app + install

# Release a new version (triggers .github/workflows/release.yml, which
# builds the macOS .app and Arch package and attaches them to a GitHub
# Release)
git tag -a v1.0.X -m "..." && git push --tags
```

Dependencies: `requirements.txt` covers the client (`PySide6`, `sounddevice`,
`numpy`). The server does **not** need `sounddevice`/PortAudio — see
Architecture below for why — so a server-only install only needs `PySide6`
and `numpy` (plus system `pactl`/`parec` and PipeWire, already present on
CachyOS).

## Architecture

### Server never uses PortAudio/sounddevice for capture — it shells out to `parec`

This is the single most important non-obvious fact in the codebase.
`server/audio_engine.py` creates a PipeWire null-sink (`EtherWave_Sink`) via
`pactl load-module module-null-sink`, then captures its `.monitor` source by
spawning `parec` as a subprocess and reading raw PCM from its stdout —
**not** via `sounddevice.InputStream`. PortAudio's Linux backend only
enumerates ALSA hardware devices; it can never see a PipeWire virtual sink's
monitor source, no matter how the device list is rescanned. `parec` speaks
the same protocol `pactl` already uses, so it finds it reliably. The
`--channel-map` argument passed to `parec` must exactly match the sink's own
`channel_map` (`CHANNEL_MAPS` dict) — omitting it lets PulseAudio assume its
own default map for the channel count, causing a silent channel remix
(sample data ends up correct-looking but scrambled between channels, e.g.
content meant for R arrives on LFE).

### Wire protocol is duplicated, not shared, between server and client

`server/audio_engine.py` and `client/audio_player.py` each independently
define `HEADER_FORMAT = "!4sIdBH"`, `MAGIC`, `AUDIO_PORT`, etc. Same for
`server/discovery.py` / `client/discovery.py` (`DISCOVERY_PORT`,
`SERVICE_ID`, the JSON beacon schema). If you change the packet header or
beacon schema, update both sides — there is no import between `server/` and
`client/` to keep them in sync automatically.

**Audio packets** (UDP, port `51235`, broadcast from server), header packed
with `struct.pack("!4sIdBH", ...)` (19 bytes):

| Field | Type | Bytes | Notes |
|---|---|---|---|
| magic | `4s` | 4 | `b"EWv1"` |
| sequence_num | `I` | 4 | Big-endian uint32, wraps at 2^32 |
| timestamp | `d` | 8 | `time.time()` at capture |
| channels | `B` | 1 | 2, 3, 4, 6, or 8 |
| frame_count | `H` | 2 | Frames in this packet |
| payload | float32[] | frame_count × channels × 4 | Interleaved PCM |

**Discovery beacons** (UDP, port `51234`, broadcast from server every 2s,
from launch until the app closes — not just while streaming), JSON body:

```json
{
  "service": "EtherWave",
  "version": 1,
  "name": "hostname",
  "audio_port": 51235,
  "channels": 6,
  "sample_rate": 48000,
  "streaming": false,
  "timestamp": 1234567890.12
}
```

`streaming` is `false` while the server app is open but "Start Streaming"
hasn't been clicked (or after "Stop Streaming"), and `true` while audio is
actively flowing. `channels` reflects the currently-selected layout even
while idle, updating live if changed before streaming starts.

### `JitterBuffer` (`client/audio_player.py`) is a ring buffer indexed by absolute frame position, not a packet queue

Each packet's frame is placed at `(seq - base_seq) * frames_per_packet`
modulo the ring capacity — this makes reordering resolve for free (write
position is independent of arrival order) but means the buffer has three
distinct failure modes that were each found and fixed by direct
measurement, not inspection:

1. **Anchor timing**: `started`/`read_frame` must be anchored on the first
   real *pull* (playback start), not the first *push* (network arrival) —
   anchoring on push bakes in whatever delay `sd.OutputStream` takes to
   start its first real callback as permanent latency.
2. **Clock drift resync**: server capture clock and client playback clock
   are independent oscillators; over time `read_frame` drifts relative to
   the write frontier. The resync guard in `pull()` fires only when
   `available` exceeds a full ring's worth (`capacity_frames`, ~2s) in
   *either* direction — deliberately not the tighter `jitter_frames`
   margin, because that was measured to fire on ordinary network jitter
   (routine underrun bursts), causing audible clicks on every false
   resync instead of only on genuine sustained drift.
3. **Stream-restart detection**: a fresh `AudioCaptureThread` (each Start
   Streaming click) restarts its sequence counter at 0. `push()` detects a
   packet landing more than a full ring behind the already-known
   `_max_written` and does a clean reset (new `base_seq`, zeroed ring)
   instead of writing the restarted stream's early packets into stale
   ring slots at the wrong position.

### UDP packet pacing on the server matters as much as the jitter buffer on the client

`AudioCaptureThread.run()` explicitly paces `sendto()` calls to the packet's
real-time duration (`next_send_time += packet_duration`, sleeping if ahead
of schedule) rather than firing every packet accumulated from one read of
`parec`'s pipe back-to-back. Without this, packets leave in bursts (multiple
packets microseconds apart, then a gap) even though the *average* rate is
correct — this was confirmed by capturing live packet arrival timestamps and
measuring inter-packet interval stddev, not by reasoning about the code in
the abstract.

### Discovery beacon broadcasts continuously, independent of streaming state

`DiscoveryBroadcaster` starts at server app launch (not on "Start
Streaming") and keeps running until the app closes; a `streaming: bool`
field in the JSON beacon tells clients whether audio is actually flowing.
This lets the client's "auto-connect to first detected server" toggle
connect to an idle server and simply play silence until streaming starts,
rather than requiring streaming to already be active before a client can
find the server at all.

### System tray lifecycle: closing the window hides it, not quits it

Both `ServerMainWindow` and `ClientMainWindow` override `closeEvent` to
`event.ignore(); self.hide()` unless `self._quitting` was set first (only
the tray menu's "Close" action sets it). `main.py` in both apps sets
`app.setQuitOnLastWindowClosed(False)` to match. If you add a new way to
exit the app, route it through the same `_quitting` flag + `close()`
pattern, not a direct `sys.exit()`/`QApplication.quit()`, or teardown
(stopping the capture thread, removing the PipeWire sink, disconnecting)
gets skipped.

### Settings persistence pattern

Both `gui.py` files use `QSettings` (ini-backed) with `_load_settings()` /
`_save_settings()` methods as the sole extension point — settings save
immediately on change via signal connections in `__init__`, not just on
close. To add a new persisted setting: add the widget, one line each in
`_load_settings()`/`_save_settings()`, and connect its change signal to
`_save_settings`.

### Debugging methodology this project has relied on

Bugs in the audio path here have consistently turned out to be real,
measurable effects, not things guessable from reading code in isolation —
e.g. PipeWire channel-mixing behavior depending on which client API an app
uses, `EasyEffects` silently downmixing everything to stereo before it
reaches the virtual sink, packet burstiness invisible in average-rate
statistics. When debugging streaming/audio issues: capture and measure
(`parec` piped through a small numpy script to check per-channel peaks, a
raw UDP socket bound with `SO_REUSEPORT` to sniff live traffic and compute
inter-packet timing, `pw-link -l` / `pactl list sink-inputs` to see actual
PipeWire graph connections) rather than reasoning from the source alone.
When testing against a live PipeWire sink on this machine, use a throwaway
sink name (not `EtherWave_Sink`) if a real session might be using the
default name concurrently — PipeWire allows duplicate sink names and
resolves them ambiguously, so a same-named test sink can silently steal or
mix into a live session's audio.
