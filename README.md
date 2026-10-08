# MHR Suite — Domain-Fronted Relay + Management Panel

> **فارسی:** برای آموزش کامل فارسی، [README_FA.md](README_FA.md) را بخوانید.

A complete, self-hosted censorship-circumvention kit: your traffic travels inside
TLS connections that **only show `www.google.com`** to the network, hops through
a Google Apps Script you own, and exits through a Cloudflare Worker you own.
A web management panel lets you test the relay, generate ready-to-use client
configs (including Xray/v2ray format), and rotate your secret key.

```
Your device
    │
    │  local client (HTTP proxy on 127.0.0.1:8085)
    ▼
www.google.com   ◄── ISP / DPI sees ONLY this (encrypted TLS, SNI = www.google.com)
    │
    │  Google Apps Script (your own Google account)
    ▼
Cloudflare Worker (your own Cloudflare account)  ◄── fetches the real site
    │
    ▼
Target website
```

**Components**

| Path | What it is |
|---|---|
| `deploy/cloudflare-worker/worker.js` | Relay exit: receives requests from the Apps Script, fetches target sites, returns them |
| `deploy/gas/Code.gs` | Google Apps Script front: authenticates requests, forwards them to the Worker |
| `deploy/panel/panel.js` | Management panel (Worker): relay tests, `config.json` generator, Xray config generator, key rotation |
| `client/config.example.json` | Template for the local client configuration |

**How it works (short version):** the local client on your computer takes your
browser's traffic and sends it — disguised as ordinary Google traffic — to your
Apps Script. The script forwards it to your Cloudflare Worker, which fetches the
real website and sends the answer back along the same path. Anyone watching the
network only sees encrypted traffic to `www.google.com`.

**Limitations (read before you start):**
- This helps during **partial disruptions** (filtering/throttling while the
  international internet is reachable). During a **full blackout** (only the
  national intranet works, `google.com` unreachable) no software relay can work —
  only satellite links, one-way satellite data-casting, local mesh networks, or
  privileged access work then.
- Google sees the sites fetched through your Apps Script (same as any hosted proxy).
- Apps Script free tier: ~20,000 URL fetches/day — plenty for one person.

---

## Prerequisites

- A **Cloudflare account** (free plan is enough)
- A **Google account** (any Gmail)
- **Python 3.8+** on the computer that will run the local client
- The local client itself: [`mhr-cfw`](https://github.com/denuitt1/mhr-cfw)
  (`main.py` + `requirements.txt` from that repository)

---

## Installation — 0 to 100

### Step 0 — Get the files

Clone this repository (or download it as ZIP):

```bash
git clone https://github.com/<your-username>/mhr-suite.git
cd mhr-suite
```

Also clone the local client (you only need `main.py`, `requirements.txt`,
`setup.py`, `run.sh`/`run.bat` and the `src/` folder):

```bash
git clone https://github.com/denuitt1/mhr-cfw.git
```

### Step 1 — Deploy the relay Worker (Cloudflare)

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com/).
2. Sidebar: **Compute → Workers & Pages** → **Create** → **Create Worker**
   (start with "Hello World").
3. Name it, e.g. `mhr-relay`, and deploy the starter.
4. Click **Edit code**, delete ALL the default code.
5. Open `deploy/cloudflare-worker/worker.js` from this repo, copy everything,
   paste it into the editor.
6. Near the top, set `WORKER_URL` to your worker's own hostname, e.g.:
   ```js
   const WORKER_URL = "mhr-relay.<your-subdomain>.workers.dev";
   ```
   (This only blocks accidental self-fetch loops.)
7. Click **Deploy**. Your relay URL is
   `https://<worker-name>.<your-subdomain>.workers.dev`.
8. Quick check: open that URL in a browser — you should see
   `{"e":"Relay is Active."}`.

### Step 2 — Deploy the management panel (Cloudflare)

Same process as Step 1, but:

1. Create another Worker named e.g. `mhr-panel`.
2. Paste the contents of `deploy/panel/panel.js`.
3. **Important:** change the panel password near the top:
   ```js
   const PANEL_PASSWORD = "CHANGE_ME_TO_A_STRONG_PANEL_PASSWORD";
   ```
   Pick a long random string. Anyone with the URL + password can manage the
   relay — keep it secret.
4. Deploy and open the panel URL. You should see the login screen.

### Step 3 — Deploy the Google Apps Script

1. Go to [script.google.com](https://script.google.com), sign in with your
   Google account, click **New project**.
2. Delete the default code in the editor.
3. Open `deploy/gas/Code.gs` from this repo, copy everything, paste it in.
4. Near the top, set your own values:
   ```js
   const AUTH_KEY = "CHANGE_ME_TO_A_STRONG_SECRET";   // long random string, keep it secret
   const WORKER_URL = "https://mhr-relay.<your-subdomain>.workers.dev";  // from Step 1
   ```
5. Save (Ctrl/Cmd+S).
6. **Deploy → New deployment** → gear icon ⚙ → **Web app**.
   - **Execute as:** Me
   - **Who has access:** Anyone
7. Click **Deploy**, authorize access when Google asks.
8. Copy the **Deployment ID** (long random string) — you need it next.

> Tip: to update `Code.gs` later, don't create a new deployment — go to
> **Deploy → Manage deployments → ✏️ → Version: New version → Deploy**.
> The Deployment ID stays the same.

### Step 4 — Configure the panel

1. Open your panel URL, log in with the panel password.
2. In **Connection settings** fill in:
   - Worker URL (from Step 1)
   - Deployment ID (from Step 3)
   - AUTH_KEY (from Step 3)
3. Click **Save settings** (stored only in your browser's localStorage).

### Step 5 — Test everything from the panel

- **Test Worker** — checks the relay code is live on Cloudflare.
- **Full chain test** — sends a real request through
  `google.com → Apps Script → Worker` and fetches `example.com`.
  If it reports success, the whole chain works end to end.

### Step 6 — Run the local client

1. Install dependencies:
   ```bash
   cd mhr-cfw
   pip install -r requirements.txt
   ```
2. In the panel, open the **Build config** tab and click **Build config.json**,
   then **Download file**. (It fills in your Deployment ID and AUTH_KEY
   automatically.)
3. Put `config.json` next to `main.py` and run:
   ```bash
   python main.py
   # Windows: run.bat | Linux: ./run.sh
   ```
   The first run may ask for your system password to install a local
   certificate (generated on your machine, never leaves it).
4. You should see the HTTP proxy running on `127.0.0.1:8085`.

### Step 7 — Point your browser at the proxy

**Firefox (recommended):** Settings → search "proxy" → Network Settings →
Manual proxy configuration → HTTP Proxy `127.0.0.1`, port `8085` →
check "Also use this proxy for HTTPS".

**Chrome/Edge:** use the FoxyProxy / Proxy SwitchyOmega extension with
`127.0.0.1:8085`.

**Whole system (example):** set HTTP/HTTPS proxy to `127.0.0.1:8085`.

Open a blocked site — it should load. The panel's full-chain test is the
fastest way to check whether a failure is in your setup or the network.

### Step 8 — (Optional) Xray / v2ray-format config

The panel's **Xray config** tab generates an `xray-config.json` (SOCKS inbound
on `127.0.0.1:10808`, HTTP outbound to the local relay on `127.0.0.1:8085`).
Import it into NekoBox / Hiddify (manual config) or run it with `xray`
directly. **The local MHR client must be running** — Xray only forwards
traffic into the relay; the Google fronting happens in the local client.

### Step 9 — Rotate the secret key

If your AUTH_KEY ever leaks:

1. Panel → **Rotate key** tab → **Generate new key**.
2. Replace the `AUTH_KEY` line in your Apps Script project.
3. **Deploy → Manage deployments → ✏️ → New version → Deploy.**
4. Build a fresh `config.json` in the panel (it uses the new key automatically).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Worker URL doesn't show "Relay is Active" | Code wasn't pasted/deployed — redo Step 1 |
| Full-chain test: `unauthorized` | AUTH_KEY in Apps Script ≠ key in panel settings |
| Full-chain test: timeout | `script.google.com` unreachable from your network, or Apps Script not deployed as "Anyone" |
| Browser loads nothing via proxy | Local client not running, or browser proxy not set to `127.0.0.1:8085` |
| YouTube videos don't play | Known Apps Script limitation (`googlevideo.com` unreachable from Apps Script). Page loads; video doesn't |
| CAPTCHA loops on some sites | Cloudflare Worker exits via rotating IPs — expected; use the optional upstream forwarder in upstream `mhr-cfw` if you have a VPS |

## Security notes

- Treat `AUTH_KEY` and the panel password like passwords. Never commit the real
  values to git — the files in this repo contain placeholders on purpose.
- The panel stores your settings only in the browser (localStorage).
- Anyone with your Apps Script exec URL + AUTH_KEY can use your relay quota.

## Credits

Relay concept and protocol: [`denuitt1/mhr-cfw`](https://github.com/denuitt1/mhr-cfw)
(MIT), itself inspired by `masterking32/MasterHttpRelayVPN`. Management panel,
guides and packaging: this repository (MIT).
