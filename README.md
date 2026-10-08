<div align="center">

# 🛰️ MHR Suite

### Domain-fronted relay kit — Cloudflare Worker + Google Apps Script + Management Panel + Desktop App

**فارسی:** آموزش کامل فارسی در [README_FA.md](README_FA.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](apps/mhr-tunnel/)
[![Release](https://img.shields.io/badge/release-v1.0.0-green.svg)](https://github.com/Nikankh566/mhr-suite/releases)

*Your traffic travels inside TLS connections that only show `www.google.com`
to the network — then hops through **your own** Google Apps Script and
**your own** Cloudflare Worker to reach the real internet.*

[🚀 Quick start](#-installation--0-to-100) •
[📱 MHR Tunnel app](#-mhr-tunnel--desktop-app) •
[🛠️ Management panel](#%EF%B8%8F-management-panel) •
[❓ Troubleshooting](#-troubleshooting)

</div>

---

## ✨ What you get

| Component | Description |
|---|---|
| 🛰️ **Relay exit** | `deploy/cloudflare-worker/worker.js` — Cloudflare Worker that fetches target sites |
| 🔀 **Google front** | `deploy/gas/Code.gs` — your Apps Script; the network only sees `www.google.com` |
| 🛠️ **Management panel** | `deploy/panel/panel.js` — web panel: relay tests, config generator, Xray configs, key rotation |
| 💻 **MHR Tunnel** | `apps/mhr-tunnel/` — advanced Python desktop app: one-click connect, system-wide proxy, live dashboard |

## 🔁 How it works

```
Your device
    │
    │  MHR Tunnel (local proxy 127.0.0.1:8085)
    ▼
www.google.com   ◄── ISP / DPI sees ONLY this (encrypted TLS, SNI = www.google.com)
    │
    │  Google Apps Script (your own Google account)
    ▼
Cloudflare Worker (your own Cloudflare account)  ◄── fetches the real site
    │
    ▼
Target website 🌍
```

**Good to know before you start:**
- ✅ Built for **partial disruptions** (filtering / throttling while the
  international internet is reachable).
- ⚠️ During a **full blackout** (only the national intranet works) no software
  relay can work — only satellite links, one-way satellite data-casting, local
  mesh or privileged access do.
- 📊 Apps Script free tier ≈ 20,000 fetches/day — plenty for personal use.

---

## 🚀 Installation — 0 to 100

<details>
<summary><b>Prerequisites</b> (click to expand)</summary>

- A **Cloudflare account** (free plan is enough)
- A **Google account** (any Gmail)
- **Python 3.10+** on the computer that runs the client

</details>

### Step 1 — Deploy the relay Worker ☁️

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com/)
2. **Compute → Workers & Pages → Create → Create Worker** (Hello World template)
3. Name it `mhr-relay` → Deploy
4. **Edit code** → delete everything → paste `deploy/cloudflare-worker/worker.js`
5. Set your own hostname at the top:
   ```js
   const WORKER_URL = "mhr-relay.<your-subdomain>.workers.dev";
   ```
6. **Deploy** ✅ — open the URL, you should see `{"e":"Relay is Active."}`

### Step 2 — Deploy the management panel 🛠️

Same as Step 1, but with `deploy/panel/panel.js` in a Worker named `mhr-panel`.
**Change the panel password** at the top of the file:

```js
const PANEL_PASSWORD = "CHANGE_ME_TO_A_STRONG_PANEL_PASSWORD";
```

Open the panel URL → you should see the login screen.

### Step 3 — Deploy the Google Apps Script 📜

1. [script.google.com](https://script.google.com) → **New project**
2. Delete the default code → paste `deploy/gas/Code.gs`
3. Set your values at the top:
   ```js
   const AUTH_KEY   = "CHANGE_ME_TO_A_STRONG_SECRET";
   const WORKER_URL = "https://mhr-relay.<your-subdomain>.workers.dev";
   ```
4. **Deploy → New deployment → ⚙ Web app** → *Execute as:* Me, *Who has access:* Anyone → **Deploy**
5. Authorize, then copy the **Deployment ID**

> To update later: **Deploy → Manage deployments → ✏️ → New version** (ID stays the same).

### Step 4 — Configure the panel ⚙️

Log in to the panel → **Connection settings** → enter Worker URL, Deployment ID,
AUTH_KEY → **Save**. Then run:

- **🧪 Test Worker** — is the relay live on Cloudflare?
- **🧪 Full chain test** — real request through `google.com → Apps Script → Worker`

### Step 5 — Run MHR Tunnel 💻

```bash
cd apps/mhr-tunnel
pip install -r requirements.txt
# Linux: sudo apt install python3-tk
python mhr_tunnel.py
```

1. **Profiles** tab → enter Deployment ID + AUTH_KEY → Add
2. Press **Connect** — the app sets your **system proxy** automatically
3. Browse 🌍 — press **Disconnect** to restore normal networking

<details>
<summary><b>Manual client (no GUI)</b></summary>

```bash
git clone https://github.com/denuitt1/mhr-cfw.git
cd mhr-cfw && pip install -r requirements.txt
```

In the panel: **Build config** tab → **Build config.json** → download →
place next to `main.py` → `python main.py` → set browser proxy to
`127.0.0.1:8085`.

</details>

---

## 💻 MHR Tunnel — desktop app

One-click app that tunnels the **whole computer** (not just the browser):

- 🖱️ **One-click connect** — sets the OS proxy on connect, restores it on disconnect
- 👤 **Profiles** — multiple relays, switch in one click
- 📊 **Live dashboard** — requests, data volume, latency, uptime, top sites
- 🧪 **Relay test** — full-chain health check without leaving the app
- 📡 **Google IP scanner** — finds the fastest front IP
- 📝 **Color-coded log viewer**
- ⌨️ **Headless mode** — `python mhr_tunnel.py --no-gui --profile "My Relay"`

Works on **Windows** (registry), **macOS** (`networksetup`) and **Linux** (GNOME).

## 🛠️ Management panel

After logging in, the panel gives you:

| Tab | What it does |
|---|---|
| 🧪 Test | Worker health check + full `google.com → GAS → Worker` chain test |
| 📦 Build config | Generates ready-to-use `config.json` for the local client |
| 🔀 Xray config | Generates `xray-config.json` (import into NekoBox / Hiddify) |
| 🔑 Rotate key | Generates a new AUTH_KEY (+ redeploy instructions) |

> ⚠️ Xray configs still need the local MHR client running — Xray forwards
> traffic into the relay; the Google fronting happens in the local client.

---

## 🔑 Rotating the secret key

1. Panel → **Rotate key** → generate
2. Replace `AUTH_KEY` in your Apps Script → **Deploy → Manage deployments → New version**
3. Build a fresh `config.json` (or update the profile in MHR Tunnel)

## ❓ Troubleshooting

| Symptom | Fix |
|---|---|
| Worker URL ≠ `Relay is Active` | Code wasn't pasted/deployed — redo Step 1 |
| Chain test: `unauthorized` | AUTH_KEY mismatch between Apps Script and panel/profile |
| Chain test: timeout | `script.google.com` unreachable, or Apps Script not deployed as "Anyone" |
| Nothing loads | Client not running / system proxy not set |
| YouTube pages load, videos don't | Known Apps Script limit (`googlevideo.com` unreachable from Apps Script) |
| CAPTCHA loops | Worker exits via rotating IPs — expected behavior |

## 🔒 Security notes

- Treat `AUTH_KEY` and the panel password like passwords — the repo only
  contains `CHANGE_ME` placeholders, **never** commit real values.
- Panel settings live only in your browser (`localStorage`).
- Anyone with your Apps Script URL + AUTH_KEY can spend your relay quota.

## 🙏 Credits

Relay protocol: [denuitt1/mhr-cfw](https://github.com/denuitt1/mhr-cfw) (MIT),
inspired by `masterking32/MasterHttpRelayVPN`.
Management panel, MHR Tunnel app, guides & packaging: this repo (MIT).
