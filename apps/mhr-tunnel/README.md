# 🛰️ MHR Tunnel v2 — Advanced Python Client

A one-click desktop app with a **beautiful animated interface** that tunnels your
**whole computer** through the MHR domain-fronted relay
(`www.google.com` → Google Apps Script → Cloudflare Worker).

Relay core (`mhrcore/`) is vendored from
[denuitt1/mhr-cfw](https://github.com/denuitt1/mhr-cfw) (MIT).
The GUI, profile manager, system-proxy control, dashboard and Connection Doctor
are original to MHR Suite.

## ✨ v2 highlights

- 🌀 **Animated status orb** — breathing/pulsing rings, color-coded state
- 📈 **Live traffic graph** — real-time KB/s area chart
- 🔢 **Animated counters** — requests, data, latency ease toward live values
- 🌐 **FA / EN interface** — one-click language toggle
- 🩺 **Connection Doctor** — probes DNS → google.com:443 → Apps Script → Worker
  and tells you honestly whether the relay path is usable *right now*
- ⬆️ **Auto-update check** — notifies when a new release is out
- 📊 **Session history** — per-profile totals saved to `~/.mhr-tunnel/stats.json`
- ⌨️ **Headless mode** — `python mhr_tunnel.py --no-gui --profile "My Relay"`

Plus everything from v1: one-click connect with automatic **system-wide** proxy
(Windows/macOS/Linux), multiple profiles, relay health test, Google IP scanner,
color-coded log viewer.

## 🚀 Quick start

```bash
cd apps/mhr-tunnel
pip install -r requirements.txt
# Linux: sudo apt install python3-tk   (for the GUI)
python mhr_tunnel.py
```

1. **Profiles** tab → enter **Deployment ID** + **AUTH_KEY**
   (from the [MHR panel](https://github.com/Nikankh566/mhr-suite#step-4--configure-the-panel)) → Add
2. Press **Connect** — the orb turns green, the system proxy is set automatically
3. Browse 🌍 — **Disconnect** restores your normal network settings

First connect may ask for admin/sudo to install the local MITM certificate
(generated on your machine, never leaves it) — needed for HTTPS interception.

## 🩺 Connection Doctor

Not sure the relay can work right now? The Doctor tab checks each hop:

1. DNS resolves `www.google.com`
2. TCP port 443 to google.com connects
3. Your Google Apps Script answers
4. Your Cloudflare Worker answers

Verdict tells you plainly: relay path working / partially broken / **no
international path** (in a full national-intranet blackout no software can help).

## 🖥️ Platforms

- **Windows** — proxy via registry + settings broadcast; run `run-windows.bat`
  or build a `.exe` (see below)
- **macOS** — proxy via `networksetup` on all network services
- **Linux** — proxy via GNOME `gsettings`; other desktops set
  `http_proxy`/`https_proxy` to `http://127.0.0.1:8085/` manually

### Build a Windows .exe

On a Windows PC with Python installed:

```bat
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --windowed --name MHR-Tunnel mhr_tunnel.py
```

The `.exe` appears in `dist/`. (`mhr_tunnel_v1.py` is the previous
non-animated version, kept as backup.)

## 📁 Layout

```
apps/mhr-tunnel/
├── mhr_tunnel.py        # v2 app — animated GUI + engine + system proxy
├── mhr_tunnel_v1.py     # v1 backup (simple GUI)
├── mhrcore/             # vendored relay core (mhr-cfw, MIT)
├── requirements.txt
├── profiles.json.example
└── README.md
```

Profiles → `~/.mhr-tunnel/profiles.json` (plain text — keep it private).
