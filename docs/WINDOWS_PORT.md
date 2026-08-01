# Windows 11 server port — planning doc

Status: **deferred, backlog** — not started, no current timeline. The user
has no physical Windows audio device supporting more than stereo, which
rules out the Option 1 MVP below as-is; the realistic path needs a
multichannel-capable virtual audio device (Option 2, unverified which
product actually supports 5.1/7.1 — VAC is the current lead, not
confirmed) or, longer-term, a custom driver (Option 3). None of that has
been investigated yet — this is parked until there's actual appetite to
pick it back up, not actively planned right now.

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

## The capture backend: WASAPI loopback via `sounddevice`

Windows' built-in equivalent for "capture whatever is currently playing"
is **WASAPI loopback capture**, and it's reachable directly through
`sounddevice`/PortAudio — the same library the client already depends on.
No `parec`-equivalent subprocess hack is needed; PortAudio on Windows sees
loopback devices natively (unlike PortAudio on Linux, which is why the
Linux server avoids it entirely — see CLAUDE.md).

```python
import sounddevice as sd

wasapi_settings = sd.WasapiSettings(loopback=True)
with sd.InputStream(device=<output_device_index>, channels=N,
                     samplerate=48000, extra_settings=wasapi_settings,
                     callback=...) as stream:
    ...
```

The trick: `device=` takes the index of an **output** (render) device even
though this opens an `InputStream` — that's what makes it "loopback."

This replaces `parec`'s stdout-reading loop with a `sounddevice` callback
(or blocking reads); the packet-pacing logic around `next_send_time` in
`AudioCaptureThread.run()` is portable Python and should carry over with
only the data-source swapped out.

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
2. **Autostart at login** — no systemd/LaunchAgent equivalent. Standard
   Windows approaches: a shortcut in `shell:startup`
   (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), or a
   registry `Run` key
   (`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`).
3. **Packaging** — PyInstaller can produce a `.exe` the same way it
   produces the macOS `.app` (see `packaging/macos/EtherWaveClient.spec`
   for the pattern to mirror). Recommend shipping a working `.exe` from
   source first and deferring a proper installer (Inno Setup / NSIS) to a
   later pass — the macOS packaging also only reached its current state
   through several iterations, not on the first attempt.
4. **New server dependency**: unlike Linux (where the server needs no
   `sounddevice`/PortAudio at all), the Windows server needs it as its
   core capture mechanism — a real split from the current
   `requirements.txt` structure, which assumes the server is
   dependency-light. Give the Windows server its own requirements file
   rather than overloading the shared one with platform markers for a
   dependency the Linux server never needed in the first place.

## The open question: our own virtual audio device?

The Linux server creates a `EtherWave_Sink` virtual device on demand, with
whatever channel count the user picked (2.0–7.1), and makes it the system
default — so *all* system audio routes into it, and local playback goes
silent (the sink has no real hardware behind it) while everything streams
to the client instead. Note this only changes *between* streaming
sessions in practice (pick a layout, click Start, which creates a fresh
sink) — it isn't reconfigured while a stream is actively running, which
matters for how ambitious the Windows equivalent actually needs to be.

Reproducing this exactly on Windows means creating a **virtual audio
device** Windows will list as a real output choice. Three ways to get
there, in increasing order of effort and decreasing order of how soon
this port could actually ship:

**Option 1 — MVP, no virtual device at all.** WASAPI loopback captures
whatever the *current default output device* is already rendering, at
whatever channel count that device is presently configured for (via
Windows' own Sound Settings → Speaker Setup). Simplest by far, ships
fastest, needs zero driver work — but two real behavior differences from
Linux: (a) local playback keeps working normally alongside the stream,
it isn't silenced automatically, and (b) channel layout isn't something
EtherWave's own UI picks — the user sets it in Windows' Sound Settings
first, and EtherWave detects and streams whatever that currently is.

**Option 2 — capture a third-party virtual audio device instead of real
hardware.** Point WASAPI loopback at an existing virtual audio device
(e.g. VB-Audio Virtual Cable, free, widely used) instead of the physical
output — closer to the Linux null-sink *pattern* (audio routes to a
virtual endpoint that isn't real hardware), without writing a driver.
Caveats to verify before committing to this path: standard VB-Cable is
stereo-only as far as is known here — genuine 5.1/7.1 support would need
checking a specific product/config (e.g. Voicemeeter Potato or similar),
and this adds a separate manual install step for the user before
EtherWave even runs, which is new installation friction the Linux/macOS
sides don't have.

**Option 3 — build a custom virtual audio driver.** The only way to fully
match Linux's "create a sink with any channel count on demand" ergonomics.
This is a fundamentally different, much larger undertaking: real
kernel-mode Windows driver development (C/C++, the WDK, not Python),
Microsoft driver-signing requirements (an EV code-signing certificate,
ongoing cost, plus a Microsoft attestation-signing submission process for
it to load on a stock Windows 11 machine without the user disabling driver
signature enforcement — not something to ask general users to do), and
real stability risk (a buggy kernel audio driver can crash the whole
machine, not just the app). This is realistically a separate project, not
a task inside this port.

**Recommendation**: ship Option 1 first. It's a complete, working port
with two known, clearly-explainable behavior differences from Linux
(local playback stays audible; layout follows Windows' current speaker
config instead of being freely chosen in-app) — not a broken or
half-finished one. Treat Option 2 as a real v2 candidate once Option 1 is
live and the actual gap is felt in practice, and Option 3 as something to
consider only if Option 2 turns out to be genuinely insufficient, not a
default assumption.

## Open questions that need live testing on Windows (can't be resolved by reasoning alone)

1. Does WASAPI loopback via `sounddevice` behave differently when the
   local device is muted at the OS level, vs. actively playing? (Relevant
   if Option 1's "local playback stays audible" turns out to be
   unwanted and muting-while-capturing is attempted as a middle ground.)
2. What actually happens if `channels=N` requested on `InputStream` doesn't
   match the loopback device's current mix format — hard failure, silent
   downmix/upmix by PortAudio, or something else? This directly decides
   how much (if any) channel-layout choice Option 1 can realistically
   offer in the GUI.
3. Windows taskbar/notification-area icon behavior — KDE Plasma and macOS
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
