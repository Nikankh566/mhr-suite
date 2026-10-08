# 🛰️ MHR Tunnel — Advanced Python Client

A one-click desktop app that tunnels your **whole computer** through the MHR
domain-fronted relay (`www.google.com` → Google Apps Script → Cloudflare Worker).

Relay core (`mhrcore/`) is vendored from
[denuitt1/mhr-cfw](https://github.com/denuitt1/mhr-cfw) (MIT).
The GUI, profile manager, system-proxy control and dashboard are original to
MHR Suite.

## ✨ What makes it "advanced"

| Feature | Upstream `main.py` | MHR Tunnel |
|---|---|---|
| GUI | ❌ CLI only | ✅ Dark-theme desktop app |
| One-click connect | ❌ Manual browser proxy setup | ✅ Auto-sets **system** proxy on connect, restores on disconnect |
| Profiles | ❌ Single `config.json` | ✅ Multiple named profiles |
| Live dashboard | ❌ | ✅ Requests, data, latency, uptime, top sites |
| Relay health test | ❌ | ✅ Built-in full-chain test button |
| Google IP scanner | CLI flag | ✅ One-click button |
| Log viewer | Terminal | ✅ In-app, color-coded |
| Headless mode | ✅ | ✅ `--no-gui --profile NAME` |

## 🚀 Quick start

```bash
cd apps/mhr-tunnel
pip install -r requirements.txt
# Linux: sudo apt install python3-tk   (for the GUI)
python mhr_tunnel.py
```

1. Open the **Profiles** tab → fill in **Deployment ID** + **AUTH_KEY**
   (get them from the [MHR panel](https://github.com/Nikankh566/mhr-suite#step-4--configure-the-panel)).
2. Press **Connect**. The app sets your OS proxy to `127.0.0.1:8085` automatically.
3. Browse — everything goes through the relay. Press **Disconnect** to restore
   your normal network settings.

First connect may ask for admin/sudo to install the local MITM certificate
(generated on your machine, never leaves it) — needed for HTTPS interception.

## 🖥️ Platforms

- **Windows** — proxy via registry, settings broadcast to apps
- **macOS** — proxy via `networksetup` on all network services
- **Linux** — proxy via GNOME `gsettings`; other desktops: set
  `http_proxy`/`https_proxy` to `http://127.0.0.1:8085/` manually

## ⌨️ Headless mode

```bash
python mhr_tunnel.py --no-gui --profile "My Relay"
```

Ctrl+C stops and restores the system proxy.

## 📁 Layout

```
apps/mhr-tunnel/
├── mhr_tunnel.py        # the app (GUI + engine + system proxy)
├── mhrcore/             # vendored relay core (mhr-cfw, MIT)
├── requirements.txt
├── profiles.json.example
└── README.md
```

Profiles are stored in `~/.mhr-tunnel/profiles.json` (plain text — keep it private).
