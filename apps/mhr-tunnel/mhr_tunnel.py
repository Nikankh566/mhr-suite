#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MHR Tunnel — advanced GUI client for the MHR domain-fronted relay.

One-click connect that tunnels the whole computer:
  local proxy (127.0.0.1:PORT) → www.google.com (TLS SNI) →
  Google Apps Script → Cloudflare Worker → target site.

Relay core (mhrcore/) is vendored from denuitt1/mhr-cfw (MIT).
GUI, profiles, system-proxy control and dashboard are original to MHR Suite.

Requires: Python 3.10+, pip install -r requirements.txt
Run:  python mhr_tunnel.py
Headless: python mhr_tunnel.py --no-gui --profile "My Profile"
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import timedelta

# ── Make the vendored core importable (flat imports, like upstream) ──────────
_CORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mhrcore")
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

APP_NAME = "MHR Tunnel"
APP_VERSION = "1.0.0"
CORE_VERSION = "2.0.1"  # vendored mhr-cfw core

try:
    from proxy_server import ProxyServer  # noqa: E402
except ImportError as e:  # pragma: no cover
    sys.exit(f"Cannot import relay core (mhrcore/): {e}\n"
             f"Did you run: pip install -r requirements.txt ?")

try:
    from mitm import CA_CERT_FILE, MITMCertManager  # noqa: E402
    from cert_installer import install_ca, is_ca_trusted  # noqa: E402
    _MITM_AVAILABLE = True
except ImportError:  # cryptography not installed
    _MITM_AVAILABLE = False
    CA_CERT_FILE = None

# ── Paths ───────────────────────────────────────────────────────────────────
def _app_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".mhr-tunnel")
    os.makedirs(d, exist_ok=True)
    return d

PROFILES_PATH = os.path.join(_app_dir(), "profiles.json")

_PLACEHOLDER_KEYS = {"", "CHANGE_ME_TO_A_STRONG_SECRET", "your-secret-password-here",
                     "YOUR_APPS_SCRIPT_DEPLOYMENT_ID", "AKfyc..."}

# ── Logging → GUI queue ─────────────────────────────────────────────────────
log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_queue.put((record.levelname, self.format(record)))
        except Exception:
            pass

def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    qh = _QueueHandler()
    qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                      "%H:%M:%S"))
    root.addHandler(qh)

log = logging.getLogger("MHR-Tunnel")

# ── Client config (mirrors the panel's config generator) ────────────────────
def build_config(profile: dict) -> dict:
    return {
        "mode": "apps_script",
        "google_ip": profile.get("google_ip", "216.239.38.120"),
        "front_domain": "www.google.com",
        "script_id": profile["script_id"],
        "auth_key": profile["auth_key"],
        "listen_host": "127.0.0.1",
        "socks5_enabled": True,
        "listen_port": int(profile.get("port", 8085)),
        "socks5_port": 1080,
        "log_level": "INFO",
        "verify_ssl": True,
        "lan_sharing": False,
        "relay_timeout": 25,
        "tls_connect_timeout": 15,
        "tcp_connect_timeout": 10,
        "max_response_body_bytes": 209715200,
        "parallel_relay": 1,
        "chunked_download_extensions": [".bin", ".zip", ".tar", ".gz", ".bz2",
            ".xz", ".7z", ".rar", ".exe", ".msi", ".dmg", ".deb", ".rpm",
            ".apk", ".iso", ".img", ".mp4", ".mkv", ".avi", ".mov", ".webm",
            ".mp3", ".flac", ".wav", ".aac", ".pdf", ".doc", ".docx",
            ".ppt", ".pptx", ".wasm"],
        "chunked_download_min_size": 5242880,
        "chunked_download_chunk_size": 524288,
        "chunked_download_max_parallel": 8,
        "chunked_download_max_chunks": 256,
        "block_hosts": [],
        "bypass_hosts": ["localhost", ".local", ".lan", ".home.arpa"],
        "forwarder_hosts": [],
        "direct_google_exclude": ["gemini.google.com", "aistudio.google.com",
            "notebooklm.google.com", "labs.google.com", "meet.google.com",
            "accounts.google.com", "ogs.google.com", "mail.google.com",
            "calendar.google.com", "drive.google.com", "docs.google.com",
            "chat.google.com", "maps.google.com", "play.google.com",
            "translate.google.com", "assistant.google.com", "lens.google.com"],
        "direct_google_allow": ["www.google.com", "safebrowsing.google.com"],
        "youtube_via_relay": False,
        "hosts": {},
    }

# ── System proxy control ────────────────────────────────────────────────────
class SystemProxy:
    """Enable/disable the OS-level HTTP(S) proxy. Saves previous state."""

    def __init__(self):
        self.platform = sys.platform
        self._saved = None

    # — Windows —
    def _win_enable(self, host: str, port: int):
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            def _get(name, default=0):
                try:
                    v, _ = winreg.QueryValueEx(key, name)
                    return v
                except FileNotFoundError:
                    return default
            self._saved = {
                "ProxyEnable": _get("ProxyEnable", 0),
                "ProxyServer": _get("ProxyServer", ""),
                "ProxyOverride": _get("ProxyOverride", ""),
            }
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ,
                              f"{host}:{port}")
            override = self._saved["ProxyOverride"] or ""
            if "<local>" not in override:
                override = (override + ";<local>").strip(";")
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
        self._win_broadcast()

    def _win_disable(self):
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_WRITE) as key:
            s = self._saved or {}
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD,
                              int(s.get("ProxyEnable", 0)))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ,
                              str(s.get("ProxyServer", "")))
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                              str(s.get("ProxyOverride", "")))
        self._win_broadcast()
        self._saved = None

    def _win_broadcast(self):
        try:
            HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                "Environment", SMTO_ABORTIFHUNG, 5000, None)
        except Exception:
            pass

    # — macOS —
    def _mac_services(self) -> list[str]:
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True, timeout=10)
        services = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("An asterisk") and line != "*":
                services.append(line.lstrip("*"))
        return services

    def _mac_enable(self, host: str, port: int):
        self._saved = {"services": []}
        for svc in self._mac_services():
            try:
                cur = subprocess.run(["networksetup", "-getwebproxy", svc],
                                     capture_output=True, text=True, timeout=10).stdout
                enabled = "Yes" in cur.splitlines()[0] if cur else False
                self._saved["services"].append((svc, enabled))
                subprocess.run(["networksetup", "-setwebproxy", svc, host, str(port)],
                               check=False, timeout=10)
                subprocess.run(["networksetup", "-setsecurewebproxy", svc, host, str(port)],
                               check=False, timeout=10)
            except Exception as e:
                log.warning("macOS proxy setup skipped for %s: %s", svc, e)

    def _mac_disable(self):
        for svc, was_enabled in (self._saved or {}).get("services", []):
            try:
                subprocess.run(["networksetup", "-setwebproxystate", svc,
                                "on" if was_enabled else "off"],
                               check=False, timeout=10)
                subprocess.run(["networksetup", "-setsecurewebproxystate", svc,
                                "on" if was_enabled else "off"],
                               check=False, timeout=10)
            except Exception:
                pass
        self._saved = None

    # — Linux (GNOME) —
    def _gsettings(self, *args):
        return subprocess.run(["gsettings", *args], capture_output=True,
                              text=True, timeout=10)

    def _linux_enable(self, host: str, port: int):
        if not shutil.which("gsettings"):
            raise RuntimeError("gsettings not found — set the proxy manually:\n"
                               f"http_proxy=http://{host}:{port}/ "
                               f"https_proxy=http://{host}:{port}/")
        mode = self._gsettings("get", "org.gnome.system.proxy", "mode").stdout.strip()
        http_host = self._gsettings("get", "org.gnome.system.proxy.http", "host").stdout.strip()
        http_port = self._gsettings("get", "org.gnome.system.proxy.http", "port").stdout.strip()
        https_host = self._gsettings("get", "org.gnome.system.proxy.https", "host").stdout.strip()
        https_port = self._gsettings("get", "org.gnome.system.proxy.https", "port").stdout.strip()
        self._saved = {"mode": mode, "http_host": http_host, "http_port": http_port,
                       "https_host": https_host, "https_port": https_port}
        self._gsettings("set", "org.gnome.system.proxy", "mode", "'manual'")
        self._gsettings("set", "org.gnome.system.proxy.http", "host", f"'{host}'")
        self._gsettings("set", "org.gnome.system.proxy.http", "port", str(port))
        self._gsettings("set", "org.gnome.system.proxy.https", "host", f"'{host}'")
        self._gsettings("set", "org.gnome.system.proxy.https", "port", str(port))

    def _linux_disable(self):
        s = self._saved or {}
        if shutil.which("gsettings") and s:
            self._gsettings("set", "org.gnome.system.proxy", "mode", s.get("mode", "'none'"))
            self._gsettings("set", "org.gnome.system.proxy.http", "host", s.get("http_host", "''"))
            self._gsettings("set", "org.gnome.system.proxy.http", "port", s.get("http_port", "0"))
            self._gsettings("set", "org.gnome.system.proxy.https", "host", s.get("https_host", "''"))
            self._gsettings("set", "org.gnome.system.proxy.https", "port", s.get("https_port", "0"))
        self._saved = None

    # — public —
    def enable(self, host: str, port: int):
        if self.platform == "win32":
            self._win_enable(host, port)
        elif self.platform == "darwin":
            self._mac_enable(host, port)
        elif self.platform.startswith("linux"):
            self._linux_enable(host, port)
        else:
            raise RuntimeError(f"Unsupported platform: {self.platform}")

    def disable(self):
        try:
            if self.platform == "win32":
                self._win_disable()
            elif self.platform == "darwin":
                self._mac_disable()
            elif self.platform.startswith("linux"):
                self._linux_disable()
        except Exception as e:
            log.warning("Could not restore system proxy: %s", e)

# ── Relay engine (runs the asyncio proxy in a background thread) ─────────────
class RelayEngine:
    def __init__(self):
        self.server: ProxyServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.running = False
        self.connect_time: float | None = None
        self._stop_event = threading.Event()

    @property
    def stats(self) -> dict:
        if self.server and self.server.fronter:
            try:
                return self.server.fronter.stats_snapshot()
            except Exception:
                pass
        return {"per_site": []}

    def start(self, config: dict):
        if self.running:
            return
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(config,),
                                       daemon=True, name="relay-engine")
        self.thread.start()
        # Wait until the server socket is up (or fail fast).
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.running:
                break
            if not self.thread.is_alive():
                raise RuntimeError("Relay thread died during startup — see log.")
            time.sleep(0.2)
        if not self.running:
            raise RuntimeError("Relay did not start in time — see log.")
        self.connect_time = time.time()

    def _run(self, config: dict):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = ProxyServer(config)
        serve_task = None
        try:
            # start() blocks inside serve_forever(), so run it as a task and
            # wait until the listening sockets are actually up.
            serve_task = self.loop.create_task(self.server.start(),
                                               name="proxy-serve")

            async def _wait_up() -> bool:
                for _ in range(150):
                    if getattr(self.server, "_servers", None):
                        return True
                    await asyncio.sleep(0.1)
                return False

            if not self.loop.run_until_complete(_wait_up()):
                raise RuntimeError("proxy listeners did not come up")
            self.running = True
            log.info("Relay engine running — proxy on %s:%s",
                     config.get("listen_host"), config.get("listen_port"))
            while not self._stop_event.is_set():
                if serve_task.done():
                    exc = serve_task.exception()
                    raise RuntimeError(f"proxy server exited unexpectedly: {exc}")
                self.loop.run_until_complete(asyncio.sleep(0.5))
        except Exception as e:
            log.error("Relay engine error: %s", e)
        finally:
            try:
                if serve_task and not serve_task.done():
                    serve_task.cancel()
                    try:
                        self.loop.run_until_complete(serve_task)
                    except (asyncio.CancelledError, Exception):
                        pass
                if self.server:
                    self.loop.run_until_complete(self.server.stop())
            except Exception:
                pass
            self.running = False
            try:
                self.loop.close()
            except Exception:
                pass

    def stop(self):
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        self.running = False
        self.server = None
        self.connect_time = None

# ── Relay chain test (same check the panel does) ─────────────────────────────
def test_relay_chain(script_id: str, auth_key: str, timeout: int = 30) -> tuple[bool, str]:
    url = f"https://script.google.com/macros/s/{script_id}/exec"
    payload = json.dumps({"k": auth_key, "u": "https://example.com/",
                          "m": "GET", "h": {}, "r": True}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"request failed: {e}"
    try:
        data = json.loads(body)
    except Exception:
        return False, f"unexpected response: {body[:120]}"
    if data.get("s") == 200:
        return True, "relay OK — example.com fetched (HTTP 200) via google.com → Apps Script → Worker"
    if data.get("e"):
        return False, f"relay error: {data['e']}"
    return False, f"unexpected response: {body[:120]}"

# ═══════════════════════════════════════════════════════════════════════════
#  GUI (tkinter, dark theme, zero extra dependencies)
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"

def _fmt_uptime(sec: float) -> str:
    return str(timedelta(seconds=int(sec)))


class TunnelGUI:
    # — palette —
    BG = "#0f1420"; CARD = "#182030"; BORDER = "#263049"; FG = "#e8ecf4"
    MUTED = "#8b93a7"; ACCENT = "#2f6fed"; ACCENT_H = "#3d7dff"
    GREEN = "#3ddc84"; RED = "#ff5d5d"; AMBER = "#ffb020"

    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("920x660")
        root.minsize(760, 540)
        root.configure(bg=self.BG)

        self.engine = RelayEngine()
        self.sysproxy = SystemProxy()
        self.profiles: list[dict] = self._load_profiles()
        self.active_profile: dict | None = None
        self.proxy_was_set = False
        self._busy = False

        self._build_style()
        self._build_topbar()
        self._build_tabs()
        self._refresh_profile_list()
        self.root.after(500, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # — styling —
    def _build_style(self):
        import tkinter.ttk as ttk
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=self.BG)
        s.configure("Card.TFrame", background=self.CARD)
        s.configure("TLabel", background=self.BG, foreground=self.FG, font=("Segoe UI", 10))
        s.configure("Card.TLabel", background=self.CARD, foreground=self.FG)
        s.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 9))
        s.configure("CardMuted.TLabel", background=self.CARD, foreground=self.MUTED, font=("Segoe UI", 9))
        s.configure("Title.TLabel", background=self.BG, foreground=self.FG,
                    font=("Segoe UI", 13, "bold"))
        s.configure("Stat.TLabel", background=self.CARD, foreground=self.FG,
                    font=("Segoe UI", 16, "bold"))
        s.configure("TButton", background=self.ACCENT, foreground="white",
                    font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        s.map("TButton", background=[("active", self.ACCENT_H), ("disabled", "#3a4358")])
        s.configure("Ghost.TButton", background=self.BORDER, foreground=self.FG,
                    font=("Segoe UI", 10), padding=6, borderwidth=0)
        s.map("Ghost.TButton", background=[("active", "#33405c")])
        s.configure("Danger.TButton", background="#8a2b3a", foreground="white",
                    font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        s.configure("TNotebook", background=self.BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=self.CARD, foreground=self.MUTED,
                    padding=(16, 8), font=("Segoe UI", 10))
        s.map("TNotebook.Tab", background=[("selected", self.ACCENT)],
              foreground=[("selected", "white")])
        s.configure("TCombobox", fieldbackground=self.CARD, background=self.CARD,
                    foreground=self.FG, arrowcolor=self.FG)
        s.configure("Treeview", background=self.CARD, fieldbackground=self.CARD,
                    foreground=self.FG, borderwidth=0, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background=self.BORDER, foreground=self.FG,
                    font=("Segoe UI", 9, "bold"))
        s.configure("TCheckbutton", background=self.BG, foreground=self.FG,
                    font=("Segoe UI", 10))
        self._ttk = ttk

    # — top bar —
    def _build_topbar(self):
        import tkinter as tk
        from tkinter import ttk
        bar = ttk.Frame(self.root, padding=(16, 12))
        bar.pack(fill="x")
        ttk.Label(bar, text="🛰️", font=("Segoe UI", 20)).pack(side="left")
        box = ttk.Frame(bar); box.pack(side="left", padx=10)
        ttk.Label(box, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, text=f"v{APP_VERSION}  •  relay core v{CORE_VERSION}",
                  style="Muted.TLabel").pack(anchor="w")

        right = ttk.Frame(bar); right.pack(side="right")
        self.status_dot = tk.Label(right, text="●", fg=self.RED, bg=self.BG,
                                   font=("Segoe UI", 18))
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = ttk.Label(right, text="Disconnected")
        self.status_label.pack(side="left", padx=(0, 12))

        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(right, textvariable=self.profile_var,
                                         width=26, state="readonly")
        self.profile_combo.pack(side="left", padx=(0, 8))

        self.connect_btn = ttk.Button(right, text="Connect",
                                      command=self._on_connect_toggle)
        self.connect_btn.pack(side="left")

    # — tabs —
    def _build_tabs(self):
        import tkinter as tk
        from tkinter import ttk
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        # Dashboard
        dash = ttk.Frame(nb, padding=12)
        nb.add(dash, text="  📊 Dashboard  ")
        cards = ttk.Frame(dash); cards.pack(fill="x", pady=(0, 10))
        self.stat_vars = {}
        for key, label in (("req", "Requests"), ("data", "Data via relay"),
                           ("lat", "Avg latency"), ("up", "Uptime")):
            card = ttk.Frame(cards, style="Card.TFrame", padding=12)
            card.pack(side="left", fill="x", expand=True, padx=4)
            ttk.Label(card, text=label, style="CardMuted.TLabel").pack(anchor="w")
            var = tk.StringVar(value="—")
            ttk.Label(card, textvariable=var, style="Stat.TLabel").pack(anchor="w")
            self.stat_vars[key] = var

        ttk.Label(dash, text="Top sites by traffic",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(4, 4))
        cols = ("host", "requests", "errors", "data", "latency")
        self.hosts_tree = ttk.Treeview(dash, columns=cols, show="headings", height=9)
        for c, w, t in (("host", 320, "Host"), ("requests", 90, "Requests"),
                        ("errors", 70, "Errors"), ("data", 110, "Data"),
                        ("latency", 90, "Avg ms")):
            self.hosts_tree.heading(c, text=t)
            self.hosts_tree.column(c, width=w, anchor="w" if c == "host" else "center")
        self.hosts_tree.pack(fill="both", expand=True)

        tools = ttk.Frame(dash); tools.pack(fill="x", pady=(10, 0))
        ttk.Button(tools, text="🧪 Test relay", style="Ghost.TButton",
                   command=self._on_test_relay).pack(side="left", padx=3)
        ttk.Button(tools, text="📡 Scan Google IPs", style="Ghost.TButton",
                   command=self._on_scan).pack(side="left", padx=3)
        ttk.Button(tools, text="🔐 Install certificate", style="Ghost.TButton",
                   command=self._on_install_cert).pack(side="left", padx=3)
        self.sysproxy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="Set system proxy on connect",
                        variable=self.sysproxy_var).pack(side="left", padx=12)

        # Profiles
        prof = ttk.Frame(nb, padding=12)
        nb.add(prof, text="  👤 Profiles  ")
        left = ttk.Frame(prof); left.pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(left, text="Saved profiles").pack(anchor="w", pady=(0, 4))
        self.profile_list = tk.Listbox(left, width=28, height=14, bg=self.CARD,
                                       fg=self.FG, selectbackground=self.ACCENT,
                                       borderwidth=0, highlightthickness=0,
                                       font=("Segoe UI", 10))
        self.profile_list.pack()
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_select)

        form = ttk.Frame(prof); form.pack(side="left", fill="both", expand=True)
        self.form_vars = {}
        for key, label, show in (("name", "Profile name", None),
                                 ("script_id", "Deployment ID (Apps Script)", None),
                                 ("auth_key", "AUTH_KEY", "*"),
                                 ("worker_url", "Worker URL (https://…)", None),
                                 ("port", "Local port", None)):
            ttk.Label(form, text=label, style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
            e = tk.Entry(form, bg=self.CARD, fg=self.FG, insertbackground=self.FG,
                         relief="flat", font=("Segoe UI", 10), show=show or "")
            e.pack(fill="x", ipady=6)
            self.form_vars[key] = e
        self.form_vars["port"].insert(0, "8085")
        btns = ttk.Frame(form); btns.pack(fill="x", pady=12)
        ttk.Button(btns, text="➕ Add", command=self._profile_add).pack(side="left", padx=3)
        ttk.Button(btns, text="💾 Update", style="Ghost.TButton",
                   command=self._profile_update).pack(side="left", padx=3)
        ttk.Button(btns, text="🗑 Delete", style="Danger.TButton",
                   command=self._profile_delete).pack(side="left", padx=3)
        ttk.Label(form, text="Tip: get Deployment ID + AUTH_KEY from the MHR panel.\n"
                             "Keys are stored in plain text in ~/.mhr-tunnel/profiles.json —\n"
                             "keep that file private.",
                  style="Muted.TLabel").pack(anchor="w", pady=6)

        # Log
        logf = ttk.Frame(nb, padding=12)
        nb.add(logf, text="  📝 Log  ")
        self.log_text = tk.Text(logf, bg="#0b0f18", fg="#c9d4e8", relief="flat",
                                font=("Consolas", 9), wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("ERROR", foreground="#ff8a8a")
        self.log_text.tag_config("WARNING", foreground="#ffcf7a")
        self.log_text.tag_config("INFO", foreground="#c9d4e8")
        self.log_text.tag_config("DEBUG", foreground="#6b7690")
        brow = ttk.Frame(logf); brow.pack(fill="x", pady=(8, 0))
        ttk.Button(brow, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="left")
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(brow, text="Auto-scroll",
                        variable=self.autoscroll_var).pack(side="left", padx=10)

    # — profiles storage —
    def _load_profiles(self) -> list[dict]:
        try:
            with open(PROFILES_PATH, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_profiles(self):
        with open(PROFILES_PATH, "w", encoding="utf-8") as f:
            json.dump(self.profiles, f, ensure_ascii=False, indent=2)

    def _refresh_profile_list(self):
        names = [p.get("name", "?") for p in self.profiles]
        self.profile_list.delete(0, "end")
        for n in names:
            self.profile_list.insert("end", n)
        self.profile_combo["values"] = names
        if names and not self.profile_var.get():
            self.profile_var.set(names[0])

    def _selected_profile(self) -> dict | None:
        name = self.profile_var.get()
        for p in self.profiles:
            if p.get("name") == name:
                return p
        return None

    def _on_profile_select(self, _evt=None):
        sel = self.profile_list.curselection()
        if not sel:
            return
        p = self.profiles[sel[0]]
        for k, e in self.form_vars.items():
            e.delete(0, "end")
            e.insert(0, str(p.get(k, "")))
        self.profile_var.set(p.get("name", ""))

    def _form_to_profile(self) -> dict | None:
        vals = {k: e.get().strip() for k, e in self.form_vars.items()}
        if not vals["name"]:
            self._say("Profile name is required.", error=True); return None
        if not vals["script_id"] or vals["script_id"] in _PLACEHOLDER_KEYS:
            self._say("Enter a real Apps Script Deployment ID.", error=True); return None
        if not vals["auth_key"] or vals["auth_key"] in _PLACEHOLDER_KEYS:
            self._say("Enter your AUTH_KEY.", error=True); return None
        try:
            vals["port"] = int(vals.get("port") or 8085)
        except ValueError:
            vals["port"] = 8085
        return vals

    def _profile_add(self):
        p = self._form_to_profile()
        if not p:
            return
        if any(x["name"] == p["name"] for x in self.profiles):
            self._say("A profile with this name already exists.", error=True); return
        self.profiles.append(p)
        self._save_profiles(); self._refresh_profile_list()
        self._say(f"Profile '{p['name']}' added.")

    def _profile_update(self):
        sel = self.profile_list.curselection()
        if not sel:
            self._say("Select a profile first.", error=True); return
        p = self._form_to_profile()
        if not p:
            return
        self.profiles[sel[0]] = p
        self._save_profiles(); self._refresh_profile_list()
        self._say(f"Profile '{p['name']}' updated.")

    def _profile_delete(self):
        sel = self.profile_list.curselection()
        if not sel:
            return
        name = self.profiles.pop(sel[0])["name"]
        self._save_profiles(); self._refresh_profile_list()
        self._say(f"Profile '{name}' deleted.")

    # — connect / disconnect —
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.connect_btn.state(["disabled"] if busy else ["!disabled"])

    def _on_connect_toggle(self):
        if self.engine.running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        p = self._selected_profile()
        if not p:
            self._say("Create a profile first (Profiles tab).", error=True); return
        self.active_profile = p
        self._set_busy(True)
        threading.Thread(target=self._connect_worker, args=(p,), daemon=True).start()

    def _connect_worker(self, profile: dict):
        try:
            config = build_config(profile)
            # Ensure MITM CA exists (HTTPS interception needs it)
            if _MITM_AVAILABLE:
                if not os.path.exists(CA_CERT_FILE):
                    log.info("Generating local MITM CA certificate…")
                    MITMCertManager()
                if not is_ca_trusted(CA_CERT_FILE):
                    log.warning("MITM CA not trusted — installing (may need admin)…")
                    if install_ca(CA_CERT_FILE):
                        log.info("MITM CA installed. Restart your browser if pages warn.")
                    else:
                        log.warning("CA install failed — HTTPS sites may show warnings. "
                                    "Use the 'Install certificate' button as admin.")
            self.engine.start(config)
            if self.sysproxy_var.get():
                try:
                    self.sysproxy.enable("127.0.0.1", config["listen_port"])
                    self.proxy_was_set = True
                    log.info("System proxy → 127.0.0.1:%s", config["listen_port"])
                except Exception as e:
                    log.warning("Could not set system proxy: %s", e)
                    self._say(f"Relay is up, but system proxy failed: {e}\n"
                              f"Set your browser proxy to 127.0.0.1:{config['listen_port']} manually.",
                              error=True)
            self.root.after(0, self._ui_connected, profile)
            log.info("✅ Connected via profile '%s' — traffic: device → google.com → "
                     "Apps Script → Worker", profile["name"])
        except Exception as e:
            log.error("Connect failed: %s", e)
            self.root.after(0, self._say, f"Connect failed: {e}", True)
        finally:
            self.root.after(0, self._set_busy, False)

    def _ui_connected(self, profile: dict):
        self.status_dot.config(fg=self.GREEN)
        self.status_label.config(text=f"Connected — {profile['name']}")
        self.connect_btn.config(text="Disconnect")

    def _disconnect(self):
        self._set_busy(True)
        threading.Thread(target=self._disconnect_worker, daemon=True).start()

    def _disconnect_worker(self):
        try:
            if self.proxy_was_set:
                self.sysproxy.disable()
                self.proxy_was_set = False
                log.info("System proxy restored.")
            self.engine.stop()
            log.info("Disconnected.")
        finally:
            self.root.after(0, self._ui_disconnected)
            self.root.after(0, self._set_busy, False)

    def _ui_disconnected(self):
        self.status_dot.config(fg=self.RED)
        self.status_label.config(text="Disconnected")
        self.connect_btn.config(text="Connect")
        for v in self.stat_vars.values():
            v.set("—")
        for row in self.hosts_tree.get_children():
            self.hosts_tree.delete(row)

    # — tools —
    def _on_test_relay(self):
        p = self._selected_profile()
        if not p:
            self._say("Select a profile first.", error=True); return
        self._say("Testing relay chain…")
        threading.Thread(target=self._test_worker,
                         args=(p["script_id"], p["auth_key"]), daemon=True).start()

    def _test_worker(self, sid: str, key: str):
        ok, detail = test_relay_chain(sid, key)
        log.info("Relay test: %s", detail)
        self.root.after(0, self._say, ("✅ " if ok else "❌ ") + detail, not ok)

    def _on_scan(self):
        self._say("Scanning Google IPs for the fastest front… (takes ~30s)")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            from google_ip_scanner import scan_sync
            ok = scan_sync("www.google.com")
            self.root.after(0, self._say,
                            "Scan finished — check the Log tab for the best IP." if ok
                            else "Scan found no reachable Google IP.", not ok)
        except Exception as e:
            self.root.after(0, self._say, f"Scan failed: {e}", True)

    def _on_install_cert(self):
        if not _MITM_AVAILABLE:
            self._say("cryptography package missing — pip install -r requirements.txt",
                      error=True); return
        threading.Thread(target=self._cert_worker, daemon=True).start()

    def _cert_worker(self):
        try:
            if not os.path.exists(CA_CERT_FILE):
                MITMCertManager()
            ok = install_ca(CA_CERT_FILE)
            self.root.after(0, self._say,
                            "Certificate installed ✅" if ok
                            else "Install failed — try running as administrator.", not ok)
        except Exception as e:
            self.root.after(0, self._say, f"Install failed: {e}", True)

    # — status + log helpers —
    def _say(self, msg: str, error: bool = False):
        self.status_label.config(text=(msg[:90]))
        log.info(msg) if not error else log.error(msg)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _drain_logs(self):
        lines = []
        try:
            while True:
                lines.append(log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.config(state="normal")
            for level, text in lines[-300:]:
                tag = level if level in ("INFO", "WARNING", "ERROR", "DEBUG") else "INFO"
                self.log_text.insert("end", text + "\n", tag)
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > 800:
                self.log_text.delete("1.0", f"{total - 800}.0")
            if self.autoscroll_var.get():
                self.log_text.see("end")
            self.log_text.config(state="disabled")

    # — periodic tick —
    def _tick(self):
        self._drain_logs()
        if self.engine.running:
            snap = self.engine.stats
            sites = snap.get("per_site", [])
            total_req = sum(s["requests"] for s in sites)
            total_bytes = sum(s["bytes"] for s in sites)
            avg = (sum(s["avg_ms"] * s["requests"] for s in sites) / total_req
                   if total_req else 0)
            self.stat_vars["req"].set(f"{total_req:,}")
            self.stat_vars["data"].set(_fmt_bytes(total_bytes))
            self.stat_vars["lat"].set(f"{avg:.0f} ms" if total_req else "—")
            if self.engine.connect_time:
                self.stat_vars["up"].set(_fmt_uptime(time.time() - self.engine.connect_time))
            # top hosts table
            for row in self.hosts_tree.get_children():
                self.hosts_tree.delete(row)
            for s in sites[:25]:
                self.hosts_tree.insert("", "end", values=(
                    s["host"], f"{s['requests']:,}", s["errors"],
                    _fmt_bytes(s["bytes"]), s["avg_ms"]))
        self.root.after(1000, self._tick)

    def _on_close(self):
        if self.engine.running:
            if self.proxy_was_set:
                try:
                    self.sysproxy.disable()
                except Exception:
                    pass
            self.engine.stop()
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def _run_headless(profile_name: str):
    """Connect without GUI (servers / scripts). Ctrl+C to stop + restore."""
    profiles = []
    try:
        with open(PROFILES_PATH, encoding="utf-8") as f:
            profiles = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prof = next((p for p in profiles if p.get("name") == profile_name), None)
    if not prof:
        sys.exit(f'Profile "{profile_name}" not found in {PROFILES_PATH}')
    setup_logging()
    stream = logging.StreamHandler(sys.stdout)
    logging.getLogger().addHandler(stream)
    engine = RelayEngine()
    proxy = SystemProxy()
    proxy_set = False
    try:
        if _MITM_AVAILABLE and os.path.exists(CA_CERT_FILE or ""):
            pass
        engine.start(build_config(prof))
        try:
            proxy.enable("127.0.0.1", int(prof.get("port", 8085)))
            proxy_set = True
            log.info("System proxy enabled.")
        except Exception as e:
            log.warning("System proxy not set: %s", e)
        log.info("Connected — press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if proxy_set:
            proxy.disable()
        engine.stop()
        log.info("Stopped.")


def main():
    ap = argparse.ArgumentParser(prog="mhr-tunnel",
                                 description="MHR Tunnel — advanced GUI client "
                                             "for the MHR domain-fronted relay.")
    ap.add_argument("--no-gui", action="store_true", help="run headless")
    ap.add_argument("--profile", default=None, help="profile name (with --no-gui)")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {APP_VERSION} (core {CORE_VERSION})")
    args = ap.parse_args()

    if args.no_gui:
        if not args.profile:
            ap.error("--no-gui requires --profile NAME")
        _run_headless(args.profile)
        return

    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("tkinter is required for the GUI.\n"
                 "Linux: sudo apt install python3-tk\n"
                 "Or run headless: python mhr_tunnel.py --no-gui --profile NAME")

    setup_logging()
    import tkinter as tk
    root = tk.Tk()
    TunnelGUI(root)
    log.info("%s v%s started (relay core v%s).", APP_NAME, APP_VERSION, CORE_VERSION)
    log.info("Create a profile (Profiles tab) with your Deployment ID + AUTH_KEY, "
             "then press Connect.")
    root.mainloop()


if __name__ == "__main__":
    main()
