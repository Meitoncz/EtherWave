# Windows 11 server port — planning doc

Status: **planned, ready to start**. Decided: capture via a **VB-Audio
Virtual Cable** virtual device rather than the real output device — the
user confirmed VB-Cable supports up to 8 channels, which resolves the
multichannel question that previously blocked this (there's no physical
Windows audio device on hand that supports more than stereo, which is why
the earlier "just loopback the current default device" MVP idea was a
dead end and isn't the plan anymore). A custom driver is still on the
table as a much later upgrade if VB-Cable's limitations turn out to
matter in practice, but is not part of this pass — see "Why not a custom
driver (yet)" below.

This is a technical plan for porting `server/` to Windows 11 while
preserving all current functionality, written before any Windows-side
implementation began. If you're picking this up, read this whole file
first — it captures the reasoning, not just the conclusions, so you can
tell when a "decided" point actually needs to be revisited.

The Linux server (this repo's `server/`) and the eventual Windows server
are expected to become two independently-deployed apps sharing the wire
protocol/discovery format but not code, the same way `server/` and
`client/` already don't share code with each other (see CLAUDE.md's
"Wire protocol is duplicated, not shared" section) — port, don't
abstract.

## Why PipeWire can't just be used

PipeWire is Linux-only (built on Linux-specific kernel/D-Bus
infrastructure). No equivalent runs on Windows. This means the
`PipeWireSinkManager` + `parec` subprocess capture approach in
`server/audio_engine.py` has no direct Windows translation — a genuinely
different capture backend is needed, not a compatibility shim.

## The capture backend: WASAPI loopback on the VB-Cable device

Windows' built-in mechanism for "capture whatever is playing into a given
device" is **WASAPI loopback capture**, reachable directly through
`sounddevice`/PortAudio — the same library the client already depends on.
No `parec`-equivalent subprocess hack is needed; PortAudio on Windows sees
loopback devices natively (unlike PortAudio on Linux, which is why the
Linux server avoids it entirely — see CLAUDE.md).

The plan: install VB-Audio Virtual Cable (the user does this once, same
category of system prerequisite as PipeWire already being present on
CachyOS), set it as the Windows default output device so system audio
routes into it the same way `EtherWave_Sink` becomes the PipeWire default,
and loopback-capture *that specific device* — not "whatever the current
default happens to be," since the default *is* the VB-Cable device once
set.

```python
import sounddevice as sd

# Find the VB-Cable device by name rather than hardcoding an index (device
# indices aren't stable across reboots/driver changes).
vb_cable_index = next(
    i for i, d in enumerate(sd.query_devices())
    if "CABLE" in d["name"] and d["max_output_channels"] > 0
)

wasapi_settings = sd.WasapiSettings(loopback=True)
with sd.InputStream(device=vb_cable_index, channels=N, samplerate=48000,
                     extra_settings=wasapi_settings, callback=...) as stream:
    ...
```

The trick: `device=` takes the index of an **output** (render) device even
though this opens an `InputStream` — that's what makes it "loopback."

This replaces `parec`'s stdout-reading loop with a `sounddevice` callback
(or blocking reads); the packet-pacing logic around `next_send_time` in
`AudioCaptureThread.run()` is portable Python and should carry over with
only the data-source swapped out.

## Decided: VB-Cable as the virtual device

The Linux server creates a `EtherWave_Sink` virtual device on demand, with
whatever channel count the user picked (2.0–7.1), and makes it the system
default — so *all* system audio routes into it, and local playback goes
silent (the sink has no real hardware behind it) while everything streams
to the client instead. Note this only changes *between* streaming
sessions in practice (pick a layout, click Start, which creates a fresh
sink) — it isn't reconfigured while a stream is actively running, which
matters for how ambitious the Windows equivalent actually needs to be.

An earlier version of this doc weighed three options (no virtual device
at all / a third-party virtual device / a custom driver) because it
wasn't known whether any existing virtual audio device actually supports
more than stereo. The user has since confirmed **VB-Audio Virtual Cable
supports up to 8 channels**, which settles it — that's the path. The
"no virtual device" MVP idea is dead (there's no physical device on hand
that supports more than stereo to loopback from), and a custom driver is
unnecessary complexity now that an existing free tool covers the channel
count this project needs.

### Why not a custom driver (yet)

The only way to fully match Linux's "create a sink with any channel count
on demand, no separate install step" ergonomics would be a real
kernel-mode Windows audio driver: a fundamentally different, much larger
undertaking than the rest of this port — C/C++ and the WDK rather than
Python, a Microsoft driver-signing process (an EV code-signing
certificate, ongoing cost, plus an attestation-signing submission for it
to load on a stock Windows 11 machine without the user disabling driver
signature enforcement), and real stability risk (a buggy kernel audio
driver can crash the whole machine, not just the app). VB-Cable already
covers the channel count requirement for free, so this isn't worth taking
on right now — revisit only if VB-Cable's limitations turn out to be a
real practical problem once this is actually running.

## What ports 1:1, no changes needed

- The entire wire protocol (`HEADER_FORMAT`, `MAGIC`, packet pacing) —
  pure Python `struct`/`socket`, platform-independent.
- `discovery.py` — pure socket/JSON code, no Linux-specific calls at all;
  can likely be copied close to verbatim.
- Most of `gui.py`: tray icon, About dialog, per-channel gain, VU meters,
  `QSettings` persistence, theme-aware monochrome tray icons — PySide6/Qt
  is cross-platform.
- The single-instance guard (`QLocalServer`/`QLocalSocket`, see
  `server/main.py`) — already platform-agnostic.

## What needs new implementation

1. **Capture backend** (see above) — the biggest piece of actual new code.
2. **Setting VB-Cable as the default output device programmatically** —
   the Windows equivalent of `PipeWireSinkManager.create_sink()`/
   `pactl set-default-sink`. Windows has no first-party command-line
   equivalent; the usual approaches are the undocumented-but-widely-used
   COM `IPolicyConfig` interface (via `comtypes`/`ctypes`), or shelling
   out to a third-party CLI tool (e.g. `nircmd`, `SoundVolumeView`) the
   way the Linux server shells out to `pactl`/`parec`. Needs real research
   and live testing — this is unexplored territory, not just a capture
   backend swap.
3. **VB-Cable's channel count** — need to determine, live, whether it's
   configured once via VB-Cable's own control panel/driver properties
   (in which case EtherWave can only detect and use whatever's currently
   set, similar to the earlier MVP idea's limitation) or something
   EtherWave can actually set per-session to match the chosen layout,
   the way `PipeWireSinkManager` creates a fresh sink at the chosen
   channel count on every Start Streaming click.
4. **Local playback behavior** — once VB-Cable is the default output,
   confirm it behaves like `EtherWave_Sink` (nothing physically connected,
   so local playback goes silent) rather than passing audio through to
   real speakers by default — some virtual-cable products have their own
   optional "listen to this device" passthrough that would need to be off.
5. **Autostart at login** — no systemd/LaunchAgent equivalent. Standard
   Windows approaches: a shortcut in `shell:startup`
   (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), or a
   registry `Run` key
   (`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`).
6. **Packaging** — PyInstaller can produce a `.exe` the same way it
   produces the macOS `.app` (see `packaging/macos/EtherWaveClient.spec`
   for the pattern to mirror). Recommend shipping a working `.exe` from
   source first and deferring a proper installer (Inno Setup / NSIS) to a
   later pass — the macOS packaging also only reached its current state
   through several iterations, not on the first attempt. Should probably
   also check/document VB-Cable as an install prerequisite, the same way
   the README tells Linux users PipeWire needs to already be present.
7. **New server dependency**: unlike Linux (where the server needs no
   `sounddevice`/PortAudio at all), the Windows server needs it as its
   core capture mechanism — a real split from the current
   `requirements.txt` structure, which assumes the server is
   dependency-light. Give the Windows server its own requirements file
   rather than overloading the shared one with platform markers for a
   dependency the Linux server never needed in the first place.

## Open questions that need live testing on Windows (can't be resolved by reasoning alone)

1. How VB-Cable's channel count is actually configured — statically via
   its own control panel/driver properties, or something EtherWave can
   set per-session. Directly decides whether the channel-layout picker in
   the GUI can work the way it does on Linux (see item 3 in "What needs
   new implementation" above).
2. The concrete mechanism for setting VB-Cable as the Windows default
   output device from Python (`IPolicyConfig` via `comtypes` vs. shelling
   out to a CLI tool) — unresearched, see item 2 above.
3. Whether local playback actually goes silent once VB-Cable is default
   (matching `EtherWave_Sink`), or needs an explicit step to prevent
   passthrough to real speakers.
4. What actually happens if `channels=N` requested on `InputStream`
   doesn't match VB-Cable's current configured format — hard failure,
   silent downmix/upmix by PortAudio, or something else.
5. Windows taskbar/notification-area icon behavior — KDE Plasma and macOS
   each had their own unexpected quirks this project already had to
   live-debug (dock icon reopen, tray-click-opens-both-menu-and-window,
   monochrome icon theming). Assume Windows has its own set; don't assume
   parity with either existing platform without checking.

## Suggested directory structure

A new `server_windows/` directory mirroring `server/`'s files (`main.py`,
`gui.py`, `audio_engine.py` equivalent, `discovery.py`) — consistent with
this project's established preference for duplicating code across
independently-deployed apps rather than sharing a module with platform
branches threaded through it (see CLAUDE.md). `discovery.py` in
particular should need close to no changes from the Linux version.
