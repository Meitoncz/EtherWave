# 🌊 EtherWave

Low-latency, multichannel (stereo up to 7.1 surround) audio streaming over
your LAN — from a CachyOS/Arch Linux (PipeWire) machine to a macOS
(CoreAudio) machine, with zero-config auto-discovery. Two small desktop
apps, one for each side, that just find each other on the network and go.

Think: "I want the surround sound from my Linux gaming/media PC to come out
of the speakers/headphones plugged into my Mac" — and every existing tool
you tried only ever gave you stereo.

## ✨ What it does

- 🔊 **Real multichannel, not just stereo** — 2.0, 2.1, 4.0, 5.1, or 7.1,
  picked on the server side. Uncompressed 32-bit float audio the whole way
  through, no lossy compression.
- 📡 **Auto-discovery** — the client finds servers on the LAN by itself; no
  IP addresses to type in. Optional "connect to the first one found"
  auto-connect toggle for a fully hands-off setup.
- 🎚️ **Adjustable jitter buffer** (5–50 ms) to trade off latency vs.
  smoothness, plus an optional **adaptive mode** that grows the buffer
  automatically when it detects glitches and shrinks it back down once the
  connection's been clean for a while.
- 🎛️ **Per-channel volume trim** (±24 dB) on both the server and the client
  — handy if one speaker/channel in your setup is quieter than the others.
- 📊 **Live VU meters and stats** — see per-channel levels, buffered
  latency, packets and data transferred, underruns/resyncs, right in the
  window.
- 🗂️ **System tray / menu bar app** — runs quietly in the background with a
  tray icon (light/dark aware), autostart at login, and a menu to
  open/pause/quit without a window cluttering your screen.

## 📋 Requirements

- **Server**: CachyOS or another Arch-based Linux distro with PipeWire
  (the default on CachyOS). Python 3.10+.
- **Client**: macOS 11+.
- Both machines on the **same LAN/subnet**, wired Ethernet strongly
  recommended over Wi-Fi for a stable stream. Discovery uses UDP broadcast,
  which doesn't cross routers/VLANs — server and client need to be on the
  same network segment.
- UDP ports `51234` (discovery), `51235` (audio) and `51236` (client
  subscriptions) reachable between the two machines — see ⚠️ **Firewalls**
  below if you have one active.

## 📥 Installing the server (CachyOS / Arch Linux)

### Option A: PKGBUILD (recommended)

Installs a launcher, an app-menu entry, and a `systemd --user` service for
autostart.

```bash
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/packaging/arch
makepkg -si
```

Launch it from your app menu, or run `etherwave-server` from a terminal.

### Option B: run from source

```bash
sudo pacman -S python pyside6 python-numpy pipewire pipewire-pulse wireplumber
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/server
python main.py
```

Either way: pick a channel layout (2.0 up to 7.1) and hit **Start
Streaming**. EtherWave creates a virtual sink called `EtherWave_Sink`, sets
it as your system's default output, and starts broadcasting audio +
presence beacons. Route any app's sound to it — since it's now the default,
most apps pick it up automatically. **Stop Streaming** tears the sink down
and restores whatever your default output was before.

### Autostart at login

Only if you installed via the PKGBUILD:

```bash
systemctl --user enable --now etherwave-server.service
```

The app starts minimized to the tray at login. Disable with
`systemctl --user disable --now etherwave-server.service`.

## 📥 Installing the client (macOS)

### Option A: pre-built .app (recommended)

Download `EtherWave-Client-macOS-vX.Y.Z.zip` from the
[latest release](https://github.com/Meitoncz/EtherWave/releases), unzip it,
then from a terminal:

```bash
cd packaging/macos   # from a full repo checkout, next to install.sh
./install.sh
```

This copies `EtherWave Client.app` to `/Applications` and sets up autostart
at login. (`install.sh` expects the app in `packaging/macos/dist/` — either
from the release zip placed there, or from running `./build.sh` yourself.)

### Option B: build the .app yourself

```bash
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/packaging/macos
./build.sh      # builds EtherWave Client.app via PyInstaller
./install.sh    # copies it to /Applications, sets up autostart at login
```

### Option C: run from source (no packaging)

```bash
brew install portaudio
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd client
python main.py
```

The first time it starts, macOS will prompt for a local network permission
(needed for UDP discovery) — allow it, or the client will never see any
servers. Select a discovered server from the list, choose your output
device, and hit **Connect**.

### Autostart at login

Set up automatically by `install.sh` (Options A/B). Disable with:
```bash
launchctl unload ~/Library/LaunchAgents/com.etherwave.client.plist
rm ~/Library/LaunchAgents/com.etherwave.client.plist
```

Enable **"Auto-connect to first detected server"** in the client to skip
the manual Connect step entirely — it'll connect to whichever server it
sees first, even if idle, and just start playing once that server starts
streaming.

## 🎚️ Tuning

- **Jitter buffer** (client, 5–50 ms): lower = less delay but more
  vulnerable to network hiccups (audible glitches); higher = smoother but
  more delay. 20 ms is a solid starting point on a decent wired LAN; 5 ms
  is genuinely tight and can produce audible distortion on its own even
  with a perfectly healthy network — that's not a bug, it's just very
  little margin for error. Turn on **Adaptive** if you'd rather not tune it
  by hand: it grows the buffer automatically when it detects trouble and
  shrinks it back down once things have been clean for a while.
- **Per-channel gain** (both sides, ±24 dB): use this to balance channels
  that are naturally louder or quieter in your specific speaker setup,
  rather than fighting it with your AV receiver or OS mixer.
- **Packet size** (server): smaller packets mean lower latency but more of
  them per second. The default (240 frames, ≈5 ms) is a good balance on a
  gigabit LAN.

## ⚠️ Things to watch out for

- **`EtherWave_Sink` becomes your system's default audio output** while
  streaming — *everything* making sound on the server machine routes
  through it, not just one specific app. That's intentional (so you don't
  have to reconfigure every app individually), but it means, e.g., a
  notification sound will also travel over to the client.
- **EasyEffects (or similar global PipeWire effects tools) can silently
  downmix surround to stereo** before it ever reaches EtherWave, even if
  the source app is sending real 5.1/7.1 — the symptom is audio only
  arriving on the front-left channel. Fix: exclude `EtherWave_Sink` in
  EasyEffects' output settings, or stop it while streaming:
  ```bash
  systemctl --user stop app-com.github.wwmm.easyeffects@autostart.service
  ```
- **Firewalls will silently eat the stream.** If you run `ufw` on the
  server, open the ports first — a default-deny policy drops the packets
  with no error message, which looks exactly like a bug:
  ```bash
  sudo ufw allow 51234/udp
  sudo ufw allow 51235/udp
  sudo ufw allow 51236/udp
  ```
  Port `51236` is the one clients use to say "stream to me", which is what
  lets the server send the audio straight to them instead of broadcasting
  it at the whole network. If it stays closed nothing breaks — the server
  simply falls back to broadcasting, exactly as older versions always did —
  so this is worth opening but not required.
  On macOS, make sure you accepted the local network permission prompt the
  first time the client ran.
- **Wi-Fi works but Ethernet is much more reliable** for a smooth,
  glitch-free stream — Wi-Fi's inherent extra jitter tends to need a larger
  (or adaptive) jitter buffer to stay clean.
- **This is a LAN-only tool**, by design — it doesn't route over the
  internet or across separate network segments/VLANs.

## 🗂️ System tray / menu bar

Closing the window (the ✕ button) just hides it — the app keeps running.
Use the tray/menu bar icon to actually control it:

- **Open EtherWave** — brings the window back.
- **Pause stream** / **Resume stream** — server: Start/Stop Streaming.
  Client: Connect/Disconnect.
- **About** — version and project link.
- **Close** — actually quits (stops streaming/disconnects, and on the
  server, tears down the virtual sink first).

## 🛠️ For developers

Architecture notes, the wire protocol layout, and the debugging history
behind some of the trickier fixes (jitter buffer edge cases, PipeWire
quirks, etc.) live in [`CLAUDE.md`](CLAUDE.md) — written for AI-assisted
development but equally useful as a technical deep-dive for a human
contributor.

- Every push to `main` runs `.github/workflows/ci.yml`: compiles both apps
  and headlessly constructs both main windows to catch import/init-time
  bugs.
- Pushing a version tag (`git tag v1.0.0 && git push --tags`) runs
  `.github/workflows/release.yml`, which builds the macOS `.app` and the
  Arch package and attaches both to a GitHub Release.

## 📝 Changelog

### v1.0.4

- 🐛 **Fixed the dock/taskbar icon opening a second copy of the app**
  instead of bringing the existing (hidden-to-tray) window back. Both apps
  now guard against a second instance and just raise the running one.

### v1.0.3

- 📡 **The audio stream is now sent straight to the clients that ask for
  it**, instead of being broadcast at every device on the LAN. A broadcast
  stream has to be received and discarded by every machine on the network —
  around 1.15 MB/s at 5.1/48kHz — which is wasteful everywhere and
  genuinely disruptive over Wi-Fi. Clients now announce themselves on UDP
  `51236`; if nothing does (an older client, or that port firewalled off)
  the server keeps broadcasting exactly as before, so nothing can break.
  Discovery is unchanged, so auto-connect works the same as it always did.
- 🐛 **Fixed the jitter buffer slowly starving itself.** The capture and
  playback clocks are independent crystals (~26 ppm apart on the hardware
  this was measured on) and nothing held the buffer at its configured
  depth, so it drifted down to a single packet of headroom and underran
  roughly once a second for ~30 seconds at a time before a resync snapped
  it back — then drifted straight back down, about once a minute. Buffer
  depth is now held continuously, correcting by a single inaudible frame
  at a time.
- 🐛 **Fixed a brief interruption being treated as a full stream restart.**
  A ~200 ms hiccup (network or CPU) made the client wipe its buffer and
  rebuild from scratch, heard as a burst of distortion — far more damage
  than the hiccup itself. A restart is now recognised properly, by the
  packet numbering starting over, so an ordinary interruption just resolves
  itself.
- 🐛 **A sudden loss of buffer now recovers in ~2 seconds instead of ~15**,
  during which playback previously sat one packet away from underrunning.
- 🐛 **Running two copies of the client at once now fails with a clear
  message** instead of silently splitting the incoming stream between them
  and making both stutter.
- ⚡ Discovery no longer wakes the UI once a second regardless of whether
  anything changed.

### v1.0.2

- 🐛 Fixed the server's packet pacing occasionally falling behind by
  tens of milliseconds under heavy concurrent CPU load (e.g. a demanding
  game running alongside the streamed audio) — the capture thread now
  requests real-time (`SCHED_RR`) scheduling instead of relying on a
  process-priority hint alone, which measured as the difference between
  frequent multi-ms send bursts and a rock-steady 5.00ms cadence.
  Falls back silently if the OS denies it.
- 🐛 Fixed the client repeating the last fraction of a second of audio in
  a loop instead of going silent when the server stops streaming (e.g.
  clicking "Stop Streaming", or a brief disconnect) — the jitter buffer's
  resync guard now recognizes a genuinely stalled stream (no packets
  arriving) instead of treating it the same as ordinary clock drift
  during active playback, and stream restarts are detected from a real
  time gap rather than from packet sequence-number arithmetic that could
  miss a restart if the previous stream's run was very short.
- 🐛 Fixed a file descriptor leak in the server's `parec` launch retry
  path that could accumulate across many Stop/Start Streaming cycles in
  a single run of the app.

### v1.0.1

- 🐛 Fixed a server-side bug where latency could start around ~370ms and
  keep climbing the longer a stream ran (up to ~900ms after about 80
  minutes) — a stale backlog in how the server drained audio from
  PipeWire, not the client's jitter buffer.
- 🐛 Fixed macOS output latency being much higher than it should on
  aggregated/virtual audio devices (e.g. Rogue Amoeba's Loopback) — the
  client now explicitly requests a small output buffer instead of trusting
  the device's own (sometimes conservative) default.
- 📊 Added a real output-latency reading to the client's live stats,
  alongside the existing buffered-latency figure, for a full picture of
  where the delay actually is.
- 🎨 Cleaner, fixed-width stats layout on both apps.

### v1.0.0 — Initial public release

- 🎉 First public release.
- 🔊 Multichannel streaming (2.0 up to 7.1), uncompressed, with zero-config
  LAN auto-discovery.
- 🎚️ Adjustable jitter buffer with optional adaptive sizing.
- 🎛️ Per-channel volume trim on both server and client.
- 🗂️ System tray/menu bar apps with theme-aware icons, autostart at login,
  and an About dialog.
- 🐛 Fixed a chronic jitter-buffer desync bug that could cause occasional
  tinny/distorted or choppy audio with no self-correction.
- 📦 Automated macOS `.app` and Arch package builds via GitHub releases.

## 💬 Disclaimer

I built EtherWave primarily for myself: I'd spent years searching online
for something like it, and every similar tool I found only supported
stereo — nothing did real multichannel (5.1/7.1) LAN streaming the way I
wanted.

EtherWave was built with the help of AI, but I'm a technically grounded
person with years of professional experience in the IT industry, including
hands-on frontend development among other areas — every part of this
project has been directed, reviewed, and tested with that background, not
just prompted and shipped blind. Getting it from "technically working" to
actually solid took real, hands-on troubleshooting — live packet captures,
PipeWire routing traces, before/after latency measurements, and more than
one round of chasing audio bugs that only ever showed up under real
playback conditions, not by inspecting the code. See the Changelog above
for the specifics; this wasn't a one-shot generate-and-forget project.
