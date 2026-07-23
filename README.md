# EtherWave

Ultra-low-latency, multi-channel (2.0 up to 7.1 surround) audio streaming
over LAN, from a CachyOS/Arch Linux PipeWire server to a macOS/CoreAudio
client, with automatic peer discovery. Both sides are PySide6 (Qt6) desktop
apps.

```
/server/
  main.py            Entry point
  gui.py             PySide6 interface, VU meters & system tray
  audio_engine.py    PipeWire virtual sink management + parec capture
  discovery.py       UDP broadcast beacon ("EtherWave Server" presence)
/client/
  main.py            Entry point
  gui.py             PySide6 interface, output device picker & system tray
  audio_player.py    Jitter buffer & multi-channel sounddevice playback
  discovery.py       UDP listener for auto-discovering active servers
/assets/
  generate_icon.py   Regenerates icon.png (Pillow, no other runtime use)
  icon.png           App/tray icon, used by both apps
/packaging/
  macos/             PyInstaller spec, build/install scripts, LaunchAgent
  arch/              PKGBUILD, systemd --user service, .desktop entry
/.github/workflows/
  ci.yml             Compile + headless GUI construction check, every push
  release.yml        Builds the macOS .app and Arch package on version tags
requirements.txt
```

## How it works

1. **Server** creates a PipeWire null-sink named `EtherWave_Sink` via `pactl`
   (with a channel map matching the chosen layout) and sets it as the
   default output, so any app's audio routes into it.
2. Server captures the sink's `.monitor` source via `parec` and streams
   uncompressed 32-bit float PCM @ 48,000 Hz over UDP broadcast, in small
   sequenced packets (configurable packet size, default 240 frames ≈ 5 ms).
3. Server also broadcasts a small JSON presence beacon every 2 seconds on a
   separate UDP port, **starting as soon as the server app launches** (not
   just while streaming) — carrying a `streaming` flag — so clients can find
   and optionally pre-connect to it with zero configuration, before any
   audio flows.
4. **Client** listens for beacons, lists live servers (idle or streaming),
   and on "Connect" (manual, or automatic via the "Auto-connect to first
   detected server" toggle) opens a UDP socket filtered to the chosen
   server's IP. Incoming packets are written into a ring-buffer **jitter
   buffer** at the position implied by their sequence number — this
   reorders out-of-order packets for free and drops stale/duplicate ones.
   Playback reads lag the newest write by an adjustable 5–50 ms. Connecting
   to an idle server is safe and simply plays silence until it starts
   streaming.
5. Before playback, audio is remapped from the server's channel count to
   whatever the selected output device supports (e.g. 5.1 → stereo) using
   standard downmix coefficients.

Everything audio- or network-related runs on a `QThread` (or, for playback,
`sounddevice`'s own realtime callback thread) and only ever talks to the GUI
through Qt signals, so the UI never blocks.

## System tray

Both apps run as tray/menu-bar apps: closing the window (the X button) just
hides it — the app keeps running in the background — and the tray icon's
menu is the way to actually control or quit it:

- **Open EtherWave** — shows the main window again.
- **Pause stream** / **Resume stream** — server: same as the Start/Stop
  Streaming button. Client: same as Connect/Disconnect. Label reflects
  current state.
- **Close** — actually quits (stops streaming/disconnects, tears down the
  PipeWire sink on the server, and exits).

Left-clicking (or double-clicking, platform-dependent) the tray icon also
reopens the window. This is what makes autostart-at-login (below) usable —
the app is there in the tray ready to go, without a window cluttering your
screen at boot.

## Wire protocol

**Audio packets** (UDP, port `51235`, broadcast from server):

| Field | Type | Bytes | Notes |
|---|---|---|---|
| magic | `4s` | 4 | `b"EWv1"` |
| sequence_num | `I` | 4 | Big-endian uint32, wraps at 2^32 |
| timestamp | `d` | 8 | `time.time()` at capture, for latency display |
| channels | `B` | 1 | 2, 3, 4, 6, or 8 |
| frame_count | `H` | 2 | Frames in this packet |
| payload | float32[] | frame_count × channels × 4 | Interleaved PCM |

Header is packed with `struct.pack("!4sIdBH", ...)` (19 bytes). This layout
is duplicated (not shared as a module) in `server/audio_engine.py` and
`client/audio_player.py` — keep them in sync if you change it.

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

## Requirements

- Python 3.10+
- LAN with UDP broadcast allowed between server and client (same subnet;
  broadcast doesn't cross routers). Ensure ports `51234` (discovery) and
  `51235` (audio) are open in any firewall on both machines.
- On the server, `parec` must be available (it ships alongside `pactl`, in
  the same `libpulse` package — see below). Capture uses `parec`, not
  PortAudio/sounddevice: PortAudio's Linux backend only enumerates ALSA
  hardware devices, so it can never see a PipeWire null-sink's `.monitor`
  source, no matter how many times its device list is rescanned. `parec`
  speaks the same protocol `pactl` already uses, so it finds it reliably.

If your CachyOS install has **ufw** active (`systemctl is-active ufw`), open
both UDP ports before testing — a default-deny outgoing/incoming policy will
silently swallow the broadcast packets and look exactly like a code bug:

```bash
sudo ufw allow 51234/udp
sudo ufw allow 51235/udp
```

macOS also prompts for a local network / firewall permission the first time
the client runs — allow it, or discovery/audio packets never arrive.

## Installation — Server (CachyOS / Arch Linux)

PipeWire + WirePlumber should already be your active audio server (default
on CachyOS). The server does **not** need `sounddevice`/PortAudio — it
captures via `parec` (see Requirements above), so `python-pyside6` and
`python-numpy` are the only Python-side deps.

### Option A: PKGBUILD (recommended)

Installs a launcher, `.desktop` entry, and a systemd autostart service.

```bash
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/packaging/arch
makepkg -si
```

This installs `etherwave-server` to `/usr/bin`, an app-menu entry, and a
`systemd --user` service (not enabled by default — see Autostart below).
Launch it from your app menu, or just run `etherwave-server`.

### Option B: run from source

```bash
sudo pacman -S python python-pyside6 python-numpy pipewire pipewire-pulse wireplumber
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/server
python main.py
```

Either way: pick a channel layout, hit **Start Streaming**. The server
creates `EtherWave_Sink`, sets it as your default output, and starts
broadcasting audio + presence beacons. Route any app's sound to
`EtherWave_Sink` (it's the default now, so most apps pick it up
automatically). **Stop Streaming** tears the sink down and restores your
previous default output.

### Autostart at login (systemd)

Only relevant if you installed via the PKGBUILD (Option A):

```bash
systemctl --user enable --now etherwave-server.service
```

The app starts minimized to the tray at login; use the tray menu to Start
Streaming or leave it idle/discoverable. Disable with
`systemctl --user disable --now etherwave-server.service`.

### Surround/multichannel audio only reaching the front-left channel

If you run **EasyEffects** (or a similar global PipeWire effects processor),
it commonly auto-intercepts every sink's audio through its own internal
effects chain — which is stereo-only. That silently collapses any 5.1/7.1
source down to 2 real channels *before* it ever reaches `EtherWave_Sink`, no
matter what the source app declares. This was confirmed directly: with
EasyEffects running, `pw-link -l` showed every tested app (including a real
7.1-capable game) landing only 2 real ports on the sink; with EasyEffects
stopped, all 6/8 ports linked correctly. Fix by either excluding
`EtherWave_Sink` in EasyEffects' output settings, or stopping it while
streaming:

```bash
systemctl --user stop app-com.github.wwmm.easyeffects@autostart.service
```

## Installation — Client (macOS)

### Option A: pre-built .app (recommended)

Installs to /Applications and sets up autostart at login. Download
`EtherWave-Client-macOS.zip` from the
[latest release](https://github.com/Meitoncz/EtherWave/releases), unzip it,
then from a terminal:

```bash
cd packaging/macos   # from a full repo checkout, next to install.sh
./install.sh
```

(`install.sh` expects the built `EtherWave Client.app` in
`packaging/macos/dist/` — either from the release zip placed there, or from
running `./build.sh` yourself, see below.)

### Option B: build the .app yourself

```bash
git clone https://github.com/Meitoncz/EtherWave.git
cd EtherWave/packaging/macos
./build.sh      # generates the icon, builds EtherWave Client.app via PyInstaller
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

The first time you start it, macOS will prompt for local network
permission (needed for UDP broadcast discovery) — allow it. Select a
discovered server from the list, choose your output device, adjust the
jitter buffer if needed, and hit **Connect**.

### Autostart at login (LaunchAgent)

Set up automatically by `install.sh` (Options A/B above). It loads a
`LaunchAgent` that starts the app minimized to the menu bar at login. To
disable: `launchctl unload ~/Library/LaunchAgents/com.etherwave.client.plist
&& rm ~/Library/LaunchAgents/com.etherwave.client.plist`.

Enable **"Auto-connect to first detected server"** to skip that manual step
entirely: the client connects automatically to whichever server it detects
first, even if that server is only idle (not streaming yet) — it'll simply
play silence until the server starts streaming, at which point audio starts
flowing with no further action needed. It won't switch servers once
connected, even if others appear; disabling the toggle only stops future
auto-connects, it doesn't disconnect an existing session.

## Releasing / CI

- Every push to `main` runs `.github/workflows/ci.yml`: byte-compiles both
  apps, headlessly constructs both main windows (catches import/init-time
  bugs), regenerates the icon, and validates the packaging metadata
  (`.desktop`, PyInstaller spec, plist, shell script syntax).
- Pushing a version tag (`git tag v1.0.0 && git push --tags`) runs
  `.github/workflows/release.yml`: builds the macOS `.app` and the Arch
  package, then attaches both to a GitHub Release.

## Tuning

- **Packet size** (server): smaller = lower latency, more packets/sec.
  Default 240 frames (~5 ms) is a good balance on gigabit LAN. Go lower
  (e.g. 128 frames, ~2.7 ms) for tighter latency if your network handles it
  cleanly; raise it if you see dropouts on a busier/Wi-Fi network.
- **Jitter buffer** (client): 5–50 ms. Lower = less latency but more
  vulnerable to network jitter (audible dropouts); higher = smoother but
  more delay. Start around 20 ms and reduce if the connection is stable.

## Settings persistence

Both apps save their settings via `QSettings` (Qt's key/value store, backed
here by a plain `.ini` file) and reload them on next launch:

- **Server** (`~/.config/EtherWave/Server.ini`): channel layout, packet size.
- **Client** (`~/.config/EtherWave/Client.ini`): jitter buffer (ms),
  auto-connect toggle, output device (matched back up by name on next
  launch, since device index order isn't stable across runs — falls back to
  the first available device with a log message if it's no longer present).

Settings save immediately on change (not just on close), and each app's
`_load_settings()` / `_save_settings()` methods are the extension point for
future settings — add a widget, then one line in each to wire it up.

## Notes

- Discovery uses plain UDP broadcast rather than Zeroconf/mDNS to avoid an
  extra dependency and keep the beacon format trivially inspectable
  (`tcpdump -A udp port 51234`); it fulfills the same auto-discovery goal on
  a single LAN segment.
- Audio format is fixed at 32-bit float / 48,000 Hz throughout the pipeline
  for consistent, low-latency, distortion-free transport.
