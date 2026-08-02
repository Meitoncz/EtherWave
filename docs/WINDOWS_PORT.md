# Windows 11 server port

Status: **done, live-tested end-to-end**. `server_windows/` mirrors
`server/` (same relationship `server/`/`client/` already have — ported, not
shared code) and streams to a real macOS client over the LAN exactly like
the Linux server does. This doc now records how it actually works and the
gotchas found by live testing on a real Windows 11 machine, superseding the
original pre-implementation plan (kept below in spirit, but corrected where
reality differed).

Packaging (`packaging/windows/`, a PyInstaller build producing
`EtherWave Server.exe`) and autostart-at-login (a "Start with Windows"
checkbox backed by a `HKCU\...\Run` registry value, in `autostart.py`) are
both done too — see "Packaging" below.

## Two corrections to the original plan, found by live testing

1. **No WASAPI loopback needed.** `sd.WasapiSettings` has no `loopback`
   parameter (that was a documentation-research assumption, not verified
   against the actual `sounddevice` API at the time). VB-CABLE installs as
   a *pair* of ordinary devices — `CABLE Input` (render) and `CABLE Output`
   (an ordinary WASAPI **recording** device that mirrors it) — so capture is
   just a plain `sd.InputStream` on `CABLE Output`, the direct analog of
   PipeWire's `<sink>.monitor`.
2. **VB-CABLE actually installs *two* render endpoints**, not one: plain
   `CABLE Input` (stuck at whatever format it defaulted to — 2ch on this
   install) and `CABLE In 16ch` (the one whose Windows Sound Settings
   "Advanced" tab actually offers multichannel formats). Only setting
   **`CABLE In 16ch`** as the Windows default output actually gets
   multichannel system audio into the pipe; `default_device.py` searches
   for a render endpoint with `"16ch"` in its name first, falling back to
   any `"cable"` match (covers a VB-CABLE install that only ships the plain
   2-channel-only variant).

## How channel count actually works (the biggest real limitation)

Unlike the Linux server, **`server_windows` cannot set VB-Cable's channel
count itself.** `PipeWireSinkManager.create_sink(channels)` creates a fresh
PipeWire sink at exactly the requested channel count every time; there is no
equivalent lever on Windows. VB-Cable's capture channel count is whatever is
currently selected in **Windows Settings → Sound → Recording → CABLE Output
→ Properties → Advanced → Default Format** (and the matching Playback-side
property for the render endpoint) — a WASAPI shared-mode engine-wide format,
not something an individual app can request.

Measured directly (`sd.InputStream(channels=N, extra_settings=WasapiSettings(auto_convert=True))`
against `CABLE Output`):
- `N` equal to the configured format's channel count: always opens.
- `N` less than configured: opens too (`auto_convert=True` lets PortAudio
  downmix) — so picking a lower layout than what's configured is fine.
- `N` greater than configured: **always fails** (`PortAudioError: Invalid
  number of channels`), with or without `auto_convert`.

So: `DefaultDeviceManager.create_sink(channels)` checks the requested count
against `CABLE Output`'s *current* configured format and raises a clear
`RuntimeError` with the exact fix (which Advanced-tab format to pick, on
which device) if the request exceeds it — it does not try to change the
format itself. **A user who wants 5.1/7.1 needs to set VB-Cable's Advanced
Default Format to that channel count once**, the same "one-time prerequisite
setup" category as installing VB-Cable in the first place. This was tested
live: setting both `CABLE Output` and `CABLE In 16ch` to "Channel 6, 24 bit,
48000 Hz" made 6-channel capture work immediately, confirmed with a real
per-channel tone test (FL/FR/FC/LFE/RL/RR all landed on the correct capture
channel, no remix) and a live macOS client playing all 6 channels correctly
over the LAN.

`IPolicyConfig::SetDeviceFormat` (reachable through the same COM interface
`default_device.py` already uses for `SetDefaultEndpoint`) is a plausible
lever for setting this programmatically, but no working example was found
during development — still an open, low-priority improvement, not a
blocker.

## Two non-obvious bugs found only by live testing (not guessable from code)

1. **Opening a *callback-mode* WASAPI stream from any thread other than the
   one that just switched the default device fails deterministically**, with
   a spurious host error (`PortAudioError: ... 'GetNameFromCategory:
   usbTerminalGUID = ...' [Windows WDM-KS error -9999]`) — reproduced
   reliably across dozens of runs. Blocking-mode streams and same-thread
   callback streams were unaffected; only "callback mode" + "different OS
   thread" + "shortly after `IPolicyConfig::SetDefaultEndpoint`" triggered
   it. Root cause not fully understood (undocumented WASAPI/COM apartment
   interaction, likely around default-endpoint-change notifications
   propagating across COM apartments), but the fix reproduced reliably: call
   `comtypes.CoInitialize()` at the top of `AudioCaptureThread.run()` (paired
   with `CoUninitialize()` in a `finally`) before opening the stream — see
   the comment on `AudioCaptureThread.run()` in `audio_engine.py`.
   `STREAM_OPEN_RETRIES` stays in place regardless as a defensive backstop.
2. **`DefaultDeviceManager.create_sink()` switching the default device makes
   the very next stream-open attempt fail** even with the fix above applied,
   unless PortAudio's own internal device/host-API tables are refreshed
   first — `sd._terminate()` immediately followed by `sd._initialize()`,
   called right after the `SetDefaultEndpoint` calls in `create_sink()`,
   fixes this reliably with no delay needed (a plain `time.sleep()` between
   switch and stream-open, even several seconds, did **not** fix it on its
   own). `_terminate`/`_initialize` are undocumented `sounddevice` internals
   but have been stable across the versions checked.

## Windows-side gotcha for users, found while testing (not an EtherWave bug)

**Apps with an already-open audio session don't always follow a default-
device change.** While testing, switching the Windows default output to
VB-Cable left an already-running browser tab and a freshly-opened VLC
instance producing no audible sound, while Windows system sounds and a
fresh webpage's Web Audio test played fine. Windows' shared-mode mixer
transparently adapts *newly opened* streams to whatever the current default
device's format is; some apps (VLC in particular, depending on its
"Output module" setting — WASAPI vs. DirectSound vs. WaveOut; also seen
with a browser's audio process) can end up with a stream bound to a device
reference that doesn't hot-swap. Fix is entirely user-side: restart the
affected app (fully quitting a browser, not just reloading the tab), or in
VLC's case try switching Tools → Preferences → Audio → Output module away
from "Auto"/WASAPI to DirectSound. Worth knowing before assuming EtherWave
itself broke playback.

## Architecture

`server_windows/` — mirrors `server/`'s four-file shape:

- **`main.py`** — copy of `server/main.py`'s `QLocalServer`/`QLocalSocket`
  single-instance guard verbatim (uses a named pipe on Windows transparently,
  no code changes needed). Drops `setDesktopFileName()` (Wayland-only, no
  Windows equivalent needed).
- **`discovery.py`** — verbatim copy of `server/discovery.py`. Confirmed
  100% portable (no OS-specific calls).
- **`default_device.py`** (new) — `DefaultDeviceManager`, shaped like
  `PipeWireSinkManager` (`create_sink(channels)`/`remove_sink()`/
  `is_active`) so `gui.py`'s call sites barely differ from the Linux
  version's. Wraps the undocumented COM `IPolicyConfig`/`IMMDeviceEnumerator`
  interfaces via `comtypes` (GUIDs corroborated against `pycaw` and multiple
  independent published implementations, confirmed working live on this
  Windows 11 build) to select VB-Cable's render endpoint and restore the
  previous default on stop.
- **`audio_engine.py`** — mirrors `server/audio_engine.py`: all wire-protocol
  constants and `SubscriberRegistry` copied verbatim (byte-identical to the
  client); `_launch_parec()`'s subprocess/pipe-read loop replaced with a
  `sd.InputStream` callback pushing into a bounded `queue.Queue`, drained by
  `run()`'s loop in place of the old byte-buffer chunking (WASAPI already
  delivers exact `blocksize`-frame callbacks, so no manual reblocking is
  needed the way `parec`'s raw pipe required); the packet-pacing/construction
  loop itself is unchanged. `AvSetMmThreadCharacteristicsW("Pro Audio")`
  replaces the Linux `SCHED_RR` request for thread priority.
- **`gui.py`** — copy of `server/gui.py`; only the sink-manager class and
  `AudioCaptureThread` construction differ. VU meters, gain spinboxes,
  `QSettings`, tray icon/`_quitting` pattern, About dialog, and stats are
  unchanged. Adds one thing the Linux server doesn't have: a "Start with
  Windows" checkbox (see below).
- **`autostart.py`** (new) — `HKEY_CURRENT_USER\...\Run` registry
  read/write (`winreg`, stdlib). Registry is the source of truth (not
  `QSettings`) since it's also what Windows' own Task Manager "Startup apps"
  tab shows/lets the user disable directly. Points at `sys.executable` when
  frozen (PyInstaller), or the interpreter + `main.py` when run from source.

## Packaging

`packaging/windows/`:
- **`EtherWaveServer.spec`** — PyInstaller spec mirroring
  `packaging/macos/EtherWaveClient.spec`'s `Analysis`/`excludes`/
  `hiddenimports` structure (same `UNUSED_QT_MODULES` exclude list). Onedir
  build (`EXE(exclude_binaries=True)` + `COLLECT`, no bundle/Info.plist step
  the way macOS needs) producing an `EtherWave Server/` folder containing
  `EtherWave Server.exe`.
- **`build.ps1`** — PowerShell equivalent of `packaging/macos/build.sh`:
  installs `requirements.txt` + `requirements-windows.txt` + `pyinstaller` +
  `Pillow`, regenerates `assets/icon.png`, converts it to `assets/icon.ico`
  via Pillow, runs PyInstaller. Uses the repo's `.venv` if present, falls
  back to `python` on `PATH` (for CI).
- CI: `.github/workflows/ci.yml`'s `compile-check-windows` job (`runs-on:
  windows-latest`) byte-compiles `server_windows/*.py` and headlessly
  constructs `ServerMainWindow` — see the note below on why teardown needs
  care. `.github/workflows/release.yml`'s `build-windows` job runs
  `build.ps1` and zips the output as a release asset alongside the macOS
  `.app` and Arch package.

**Headless-test teardown gotcha on Windows** (only matters for
CI/smoke-testing code, not the app itself): the CLAUDE.md-documented
"construct the main window and let the script end" pattern that works fine
for `server`/`client` on Linux/macOS **crashes the interpreter on Windows**
(`STATUS_STACK_BUFFER_OVERRUN`) if `ServerMainWindow`'s background QThreads
(`broadcaster`, `subscribers`) are still running when the process exits.
Fix: go through the real shutdown path instead of just letting the script
end — `window._quitting = True; window.close()` — exactly like
`compile-check-windows` does. Confirmed via direct bisection: the crash
consistently occurs strictly *after* successful construction, during
interpreter teardown, and disappears entirely once threads are stopped
cleanly first.

## Requirements

`requirements-windows.txt` (separate from the shared root
`requirements.txt`, since — unlike the Linux server — this one genuinely
needs `sounddevice`/PortAudio as a runtime dependency, not just something
the client uses): `PySide6-Essentials`, `numpy`, `sounddevice`, `comtypes`
(Windows-only, `sys_platform == "win32"` marker).

## Prerequisite: VB-Audio Virtual Cable

Install from VB-Audio's official site (the user installs it once, same
category of one-time setup as PipeWire already being present on CachyOS).
After installing, if more than stereo is wanted, set the channel count via
Windows Sound Settings' Advanced tab as described above — `server_windows`
detects and works with whatever's configured, it doesn't set it.
