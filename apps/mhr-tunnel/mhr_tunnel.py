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
APP_VERSION = "2.0.0"
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
#  MHR Tunnel v2 — animated GUI
#  Animated status orb • live traffic graph • FA/EN • Connection Doctor
# ═══════════════════════════════════════════════════════════════════════════

import math
from collections import deque

STRINGS = {
    "en": {
        "dashboard": "  📊 Dashboard  ", "profiles": "  👤 Profiles  ",
        "doctor": "  🩺 Doctor  ", "log": "  📝 Log  ",
        "connect": "Connect", "disconnect": "Disconnect",
        "disconnected": "Disconnected", "connecting": "Connecting…",
        "connected": "Connected",
        "requests": "Requests", "data": "Data via relay",
        "latency": "Avg latency", "uptime": "Uptime",
        "top_sites": "Top sites by traffic",
        "set_sysproxy": "Set system proxy on connect",
        "test_relay": "🧪 Test relay", "scan_ips": "📡 Scan Google IPs",
        "install_cert": "🔐 Install certificate",
        "create_profile_first": "Create a profile first (Profiles tab).",
        "profile_name": "Profile name", "deployment_id": "Deployment ID (Apps Script)",
        "auth_key": "AUTH_KEY", "worker_url": "Worker URL (https://…)",
        "local_port": "Local port",
        "add": "➕ Add", "update": "💾 Update", "delete": "🗑 Delete",
        "saved_profiles": "Saved profiles",
        "profile_tip": "Tip: get Deployment ID + AUTH_KEY from the MHR panel.\nKeys are stored in plain text in ~/.mhr-tunnel/profiles.json — keep it private.",
        "run_doctor": "🔍 Run connection diagnosis",
        "doctor_idle": "Press the button to check whether the relay path is usable right now.",
        "probing": "Probing…",
        "clear": "Clear", "autoscroll": "Auto-scroll",
        "update_available": "⬆ Update available",
        "up_to_date": "up to date",
        "relay_test_ok": "Relay path looks good ✅",
        "speed": "Relay speed",
    },
    "fa": {
        "dashboard": "  📊 داشبورد  ", "profiles": "  👤 پروفایل‌ها  ",
        "doctor": "  🩺 دکتر اتصال  ", "log": "  📝 لاگ  ",
        "connect": "اتصال", "disconnect": "قطع اتصال",
        "disconnected": "قطع", "connecting": "در حال اتصال…",
        "connected": "متصل",
        "requests": "درخواست‌ها", "data": "داده از رله",
        "latency": "میانگین تأخیر", "uptime": "آپ‌تایم",
        "top_sites": "پرترافیک‌ترین سایت‌ها",
        "set_sysproxy": "ست کردن پروکسی سیستم موقع اتصال",
        "test_relay": "🧪 تست رله", "scan_ips": "📡 اسکن IPهای گوگل",
        "install_cert": "🔐 نصب سرتیفیکیت",
        "create_profile_first": "اول یک پروفایل بساز (تب پروفایل‌ها).",
        "profile_name": "نام پروفایل", "deployment_id": "Deployment ID (اسکریپت گوگل)",
        "auth_key": "کلید امنیتی", "worker_url": "آدرس Worker (https://…)",
        "local_port": "پورت محلی",
        "add": "➕ افزودن", "update": "💾 به‌روزرسانی", "delete": "🗑 حذف",
        "saved_profiles": "پروفایل‌های ذخیره‌شده",
        "profile_tip": "نکته: Deployment ID و AUTH_KEY را از پنل MHR بگیر.\nکلیدها به‌صورت متن ساده در ~/.mhr-tunnel/profiles.json ذخیره می‌شوند — محرمانه نگهش دار.",
        "run_doctor": "🔍 اجرای عیب‌یابی اتصال",
        "doctor_idle": "دکمه را بزن تا بفهمی مسیر رله الان قابل استفاده است یا نه.",
        "probing": "در حال بررسی…",
        "clear": "پاک کردن", "autoscroll": "اسکرول خودکار",
        "update_available": "⬆ به‌روزرسانی موجود است",
        "up_to_date": "به‌روز است",
        "relay_test_ok": "مسیر رله سالم است ✅",
        "speed": "سرعت رله",
    },
}

GITHUB_RELEASES_API = "https://api.github.com/repos/Nikankh566/mhr-suite/releases/latest"


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_uptime(sec: float) -> str:
    s = int(sec)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TunnelGUI:
    BG = "#0b0e17"; CARD = "#141b2e"; CARD2 = "#1a2338"; BORDER = "#2a3a5c"
    FG = "#eef2ff"; MUTED = "#8b93a7"; ACCENT = "#4f7cff"; ACCENT_H = "#6b93ff"
    GREEN = "#3ddc84"; RED = "#ff5d6c"; AMBER = "#ffb020"

    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("1020x700")
        root.minsize(860, 600)
        root.configure(bg=self.BG)

        self.lang = "fa"
        self.engine = RelayEngine()
        self.sysproxy = SystemProxy()
        self.profiles = self._load_profiles()
        self.active_profile = None
        self.proxy_was_set = False
        self._busy = False
        self.orb_state = "idle"          # idle | connecting | connected
        self._disp = {"req": 0.0, "data": 0.0, "lat": 0.0}
        self._graph = deque([0.0] * 60, maxlen=60)
        self._last_bytes = 0
        self._btn_hover = False

        self._build_style()
        self._build_header()
        self._build_tabs()
        self._refresh_profile_list()
        self._check_update()
        self.root.after(50, self._animate)
        self.root.after(500, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def t(self, key: str) -> str:
        return STRINGS[self.lang].get(key, key)

    # ── styling ──────────────────────────────────────────────────────────
    def _build_style(self):
        from tkinter import ttk
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=self.BG)
        s.configure("Card.TFrame", background=self.CARD)
        s.configure("TLabel", background=self.BG, foreground=self.FG,
                    font=("Segoe UI", 10))
        s.configure("Card.TLabel", background=self.CARD, foreground=self.FG)
        s.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED,
                    font=("Segoe UI", 9))
        s.configure("CardMuted.TLabel", background=self.CARD, foreground=self.MUTED,
                    font=("Segoe UI", 9))
        s.configure("Title.TLabel", background=self.BG, foreground=self.FG,
                    font=("Segoe UI", 14, "bold"))
        s.configure("Stat.TLabel", background=self.CARD, foreground=self.FG,
                    font=("Segoe UI", 17, "bold"))
        s.configure("Big.TButton", background=self.ACCENT, foreground="white",
                    font=("Segoe UI", 12, "bold"), padding=12, borderwidth=0)
        s.map("Big.TButton", background=[("active", self.ACCENT_H),
                                         ("disabled", "#3a4358")])
        s.configure("Ghost.TButton", background=self.CARD2, foreground=self.FG,
                    font=("Segoe UI", 10), padding=7, borderwidth=0)
        s.map("Ghost.TButton", background=[("active", "#243154")])
        s.configure("Danger.TButton", background="#8a2b3a", foreground="white",
                    font=("Segoe UI", 10, "bold"), padding=7, borderwidth=0)
        s.configure("TNotebook", background=self.BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=self.CARD, foreground=self.MUTED,
                    padding=(18, 9), font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab", background=[("selected", self.ACCENT)],
              foreground=[("selected", "white")])
        s.configure("TCombobox", fieldbackground=self.CARD2, background=self.CARD2,
                    foreground=self.FG, arrowcolor=self.FG)
        s.configure("Treeview", background=self.CARD, fieldbackground=self.CARD,
                    foreground=self.FG, borderwidth=0, font=("Segoe UI", 9),
                    rowheight=24)
        s.configure("Treeview.Heading", background=self.CARD2, foreground=self.FG,
                    font=("Segoe UI", 9, "bold"))
        s.configure("TCheckbutton", background=self.BG, foreground=self.FG,
                    font=("Segoe UI", 10))
        self._ttk = s

    def _card(self, parent, **kw):
        from tkinter import ttk
        f = ttk.Frame(parent, style="Card.TFrame", padding=14, **kw)
        return f

    # ── header ───────────────────────────────────────────────────────────
    def _build_header(self):
        import tkinter as tk
        from tkinter import ttk
        bar = ttk.Frame(self.root, padding=(18, 12, 18, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="🛰️", font=("Segoe UI", 24)).pack(side="left")
        box = ttk.Frame(bar)
        box.pack(side="left", padx=12)
        ttk.Label(box, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        self.ver_label = ttk.Label(box, text=f"v{APP_VERSION} • core v{CORE_VERSION}",
                                   style="Muted.TLabel")
        self.ver_label.pack(anchor="w")
        right = ttk.Frame(bar)
        right.pack(side="right")
        self.update_label = ttk.Label(right, text="", style="Muted.TLabel",
                                      foreground=self.AMBER)
        self.update_label.pack(side="left", padx=(0, 12))
        self.lang_btn = ttk.Button(right, text="EN", style="Ghost.TButton",
                                   command=self._toggle_lang, width=5)
        self.lang_btn.pack(side="left")

    def _toggle_lang(self):
        self.lang = "en" if self.lang == "fa" else "fa"
        self.lang_btn.config(text="فا" if self.lang == "en" else "EN")
        self._retranslate()

    def _retranslate(self):
        self.nb.tab(0, text=self.t("dashboard"))
        self.nb.tab(1, text=self.t("profiles"))
        self.nb.tab(2, text=self.t("doctor"))
        self.nb.tab(3, text=self.t("log"))
        self.connect_btn.config(text=self.t("disconnect") if self.engine.running
                                else self.t("connect"))
        if not self.engine.running:
            self.status_label.config(text=self.t("disconnected"))
        self.sysproxy_chk.config(text=self.t("set_sysproxy"))
        for k, lbl in self._stat_labels.items():
            lbl.config(text=self.t(k))
        self.test_btn.config(text=self.t("test_relay"))
        self.scan_btn.config(text=self.t("scan_ips"))
        self.cert_btn.config(text=self.t("install_cert"))
        self.doctor_btn.config(text=self.t("run_doctor"))
        self.clear_btn.config(text=self.t("clear"))
        self.autoscroll_chk.config(text=self.t("autoscroll"))

    # ── tabs ─────────────────────────────────────────────────────────────
    def _build_tabs(self):
        import tkinter as tk
        from tkinter import ttk
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        self._build_dashboard(self.nb)
        self._build_profiles_tab(self.nb)
        self._build_doctor_tab(self.nb)
        self._build_log_tab(self.nb)

    # ── dashboard ────────────────────────────────────────────────────────
    def _build_dashboard(self, nb):
        import tkinter as tk
        from tkinter import ttk
        dash = ttk.Frame(nb, padding=10)
        nb.add(dash, text=self.t("dashboard"))

        left = ttk.Frame(dash, width=300)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        orb_card = self._card(left)
        orb_card.pack(fill="x", pady=(0, 10))
        self.orb_canvas = tk.Canvas(orb_card, width=180, height=180, bg=self.CARD,
                                    highlightthickness=0)
        self.orb_canvas.pack()
        self.status_label = ttk.Label(orb_card, text=self.t("disconnected"),
                                      style="Card.TLabel",
                                      font=("Segoe UI", 11, "bold"))
        self.status_label.pack(pady=(2, 4))

        conn_card = self._card(left)
        conn_card.pack(fill="x", pady=(0, 10))
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(conn_card, textvariable=self.profile_var,
                                         state="readonly")
        self.profile_combo.pack(fill="x", pady=(0, 8))
        self.connect_btn = ttk.Button(conn_card, text=self.t("connect"),
                                      style="Big.TButton",
                                      command=self._on_connect_toggle)
        self.connect_btn.pack(fill="x")
        self.connect_btn.bind("<Enter>", lambda e: self._btn_glow(True))
        self.connect_btn.bind("<Leave>", lambda e: self._btn_glow(False))
        self.sysproxy_var = tk.BooleanVar(value=True)
        self.sysproxy_chk = ttk.Checkbutton(conn_card, text=self.t("set_sysproxy"),
                                            variable=self.sysproxy_var)
        self.sysproxy_chk.pack(anchor="w", pady=(8, 0))

        tools_card = self._card(left)
        tools_card.pack(fill="x")
        self.test_btn = ttk.Button(tools_card, text=self.t("test_relay"),
                                   style="Ghost.TButton", command=self._on_test_relay)
        self.test_btn.pack(fill="x", pady=2)
        self.scan_btn = ttk.Button(tools_card, text=self.t("scan_ips"),
                                   style="Ghost.TButton", command=self._on_scan)
        self.scan_btn.pack(fill="x", pady=2)
        self.cert_btn = ttk.Button(tools_card, text=self.t("install_cert"),
                                   style="Ghost.TButton",
                                   command=self._on_install_cert)
        self.cert_btn.pack(fill="x", pady=2)

        right = ttk.Frame(dash)
        right.pack(side="left", fill="both", expand=True)

        stats = ttk.Frame(right)
        stats.pack(fill="x", pady=(0, 10))
        self.stat_vars = {}
        self._stat_labels = {}
        for key in ("requests", "data", "latency", "uptime"):
            card = self._card(stats)
            card.pack(side="left", fill="x", expand=True, padx=4)
            lbl = ttk.Label(card, text=self.t(key), style="CardMuted.TLabel")
            lbl.pack(anchor="w")
            self._stat_labels[key] = lbl
            var = tk.StringVar(value="—")
            ttk.Label(card, textvariable=var, style="Stat.TLabel").pack(anchor="w")
            self.stat_vars[key] = var

        graph_card = self._card(right)
        graph_card.pack(fill="x", pady=(0, 10))
        ttk.Label(graph_card, text="📈 KB/s", style="CardMuted.TLabel").pack(anchor="w")
        self.graph_canvas = tk.Canvas(graph_card, height=110, bg=self.CARD,
                                      highlightthickness=0)
        self.graph_canvas.pack(fill="x", pady=(4, 0))
        self.graph_canvas.bind("<Configure>", lambda e: self._draw_graph())

        sites_card = self._card(right)
        sites_card.pack(fill="both", expand=True)
        ttk.Label(sites_card, text=self.t("top_sites"), style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        cols = ("host", "requests", "data", "latency")
        self.hosts_tree = ttk.Treeview(sites_card, columns=cols, show="headings",
                                       height=6)
        for c, w, hd in (("host", 300, "Host"), ("requests", 80, "Req"),
                         ("data", 100, "Data"), ("latency", 80, "ms")):
            self.hosts_tree.heading(c, text=hd)
            self.hosts_tree.column(c, width=w,
                                  anchor="w" if c == "host" else "center")
        self.hosts_tree.pack(fill="both", expand=True)

    def _btn_glow(self, on: bool):
        if self._busy:
            return
        self.connect_btn.config(
            style="Big.TButton")
        # subtle scale feedback via padding is not available; use cursor
        self.connect_btn.config(cursor="hand2" if on else "")

    def _rgb(self, rgb):
        return "#%02x%02x%02x" % rgb

    def _blend(self, fg, bg, t):
        return self._rgb(tuple(int(f + (b - f) * t) for f, b in zip(fg, bg)))

    def _draw_orb(self):
        c = self.orb_canvas
        c.delete("all")
        t = time.time()
        cx, cy = 90, 90
        if self.orb_state == "connected":
            base, speed = (61, 220, 132), 1.4
        elif self.orb_state == "connecting":
            base, speed = (255, 176, 32), 4.5
        else:
            base, speed = (255, 93, 108), 0.9
        for i in range(3):
            phase = (t * speed * 0.35 + i / 3) % 1.0
            r = 30 + phase * 58
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          outline=self._blend(base, (20, 27, 46), phase), width=2)
        pulse = 26 + 4 * math.sin(t * speed * 2)
        c.create_oval(cx - pulse, cy - pulse, cx + pulse, cy + pulse,
                      fill=self._rgb(base), outline="")
        c.create_oval(cx - pulse + 7, cy - pulse + 4,
                      cx - pulse + 20, cy - pulse + 17,
                      fill="white", outline="", stipple="gray25")
        if self.orb_state == "connected":
            a0 = (t * 120) % 360
            c.create_arc(cx - 42, cy - 42, cx + 42, cy + 42, start=a0,
                         extent=80, outline="white", width=2, style="arc")

    def _draw_graph(self):
        c = self.graph_canvas
        c.delete("all")
        w = c.winfo_width() or 400
        h = 110
        data = list(self._graph)
        mx = max(data) if max(data) > 0 else 1.0
        n = len(data)
        pts = []
        for i, v in enumerate(data):
            x = i / max(1, n - 1) * w
            y = h - 8 - (v / mx) * (h - 20)
            pts.append((x, y))
        if len(pts) > 1:
            flat = [(x, y) for x, y in pts]
            c.create_polygon(flat + [(w, h), (0, h)], fill="#1d2c52", outline="")
            c.create_line(flat, fill="#4f7cff", width=2, smooth=True)
        c.create_text(8, 8, anchor="nw", fill="#8b93a7",
                      font=("Segoe UI", 8),
                      text=f"max {_fmt_bytes(mx*1024)}/s")

    def _animate(self):
        self._draw_orb()
        # ease displayed counters toward targets
        for k in ("req", "data", "lat"):
            pass  # handled in _tick
        self.root.after(50, self._animate)

    # ── profiles tab ─────────────────────────────────────────────────────
    def _build_profiles_tab(self, nb):
        import tkinter as tk
        from tkinter import ttk
        prof = ttk.Frame(nb, padding=12)
        nb.add(prof, text=self.t("profiles"))
        left = ttk.Frame(prof)
        left.pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(left, text=self.t("saved_profiles")).pack(anchor="w", pady=(0, 4))
        self.profile_list = tk.Listbox(left, width=30, height=16, bg=self.CARD,
                                       fg=self.FG, selectbackground=self.ACCENT,
                                       borderwidth=0, highlightthickness=0,
                                       font=("Segoe UI", 10))
        self.profile_list.pack()
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_select)

        form = ttk.Frame(prof)
        form.pack(side="left", fill="both", expand=True)
        self.form_vars = {}
        for key, label_key, show in (("name", "profile_name", None),
                                    ("script_id", "deployment_id", None),
                                    ("auth_key", "auth_key", "*"),
                                    ("worker_url", "worker_url", None),
                                    ("port", "local_port", None)):
            ttk.Label(form, text=self.t(label_key),
                      style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
            e = tk.Entry(form, bg=self.CARD2, fg=self.FG, insertbackground=self.FG,
                         relief="flat", font=("Segoe UI", 10), show=show or "")
            e.pack(fill="x", ipady=7)
            self.form_vars[key] = e
        self.form_vars["port"].insert(0, "8085")
        btns = ttk.Frame(form)
        btns.pack(fill="x", pady=12)
        ttk.Button(btns, text=self.t("add"),
                   command=self._profile_add).pack(side="left", padx=3)
        ttk.Button(btns, text=self.t("update"), style="Ghost.TButton",
                   command=self._profile_update).pack(side="left", padx=3)
        ttk.Button(btns, text=self.t("delete"), style="Danger.TButton",
                   command=self._profile_delete).pack(side="left", padx=3)
        ttk.Label(form, text=self.t("profile_tip"),
                  style="Muted.TLabel").pack(anchor="w", pady=6)

    # ── doctor tab ───────────────────────────────────────────────────────
    def _build_doctor_tab(self, nb):
        import tkinter as tk
        from tkinter import ttk
        doc = ttk.Frame(nb, padding=12)
        nb.add(doc, text=self.t("doctor"))
        ttk.Label(doc, text=self.t("doctor_idle"), style="Muted.TLabel",
                  wraplength=700).pack(anchor="w", pady=(0, 10))
        self.doctor_btn = ttk.Button(doc, text=self.t("run_doctor"),
                                     command=self._on_doctor)
        self.doctor_btn.pack(anchor="w", pady=(0, 12))
        self.doctor_rows = {}
        for key, label in (("dns", "DNS: www.google.com"),
                           ("tcp", "TCP 443 → google.com"),
                           ("gas", "Google Apps Script reachable"),
                           ("worker", "Cloudflare Worker reachable")):
            row = ttk.Frame(doc, style="Card.TFrame", padding=10)
            row.pack(fill="x", pady=4)
            dot = tk.Label(row, text="●", fg=self.MUTED, bg=self.CARD,
                           font=("Segoe UI", 14))
            dot.pack(side="left", padx=(0, 10))
            ttk.Label(row, text=label, style="Card.TLabel").pack(side="left")
            detail = ttk.Label(row, text="—", style="CardMuted.TLabel")
            detail.pack(side="right")
            self.doctor_rows[key] = (dot, detail)
        self.verdict_label = ttk.Label(doc, text="", font=("Segoe UI", 11, "bold"),
                                       wraplength=700, justify="left")
        self.verdict_label.pack(anchor="w", pady=12)

    def _on_doctor(self):
        p = self._selected_profile()
        sid = p["script_id"] if p else ""
        key = p["auth_key"] if p else ""
        worker = (p.get("worker_url") or DEFAULT_WORKER_URL_FALLBACK) if p else DEFAULT_WORKER_URL_FALLBACK
        for dot, detail in self.doctor_rows.values():
            dot.config(fg=self.AMBER)
            detail.config(text=self.t("probing"))
        self.verdict_label.config(text="")
        threading.Thread(target=self._doctor_worker,
                         args=(sid, key, worker), daemon=True).start()

    def _doctor_worker(self, sid, key, worker_url):
        results = {}
        # 1. DNS
        try:
            ip = __import__("socket").gethostbyname("www.google.com")
            results["dns"] = (True, ip)
        except Exception as e:
            results["dns"] = (False, str(e)[:60])
        # 2. TCP 443
        try:
            s = __import__("socket").create_connection(("www.google.com", 443), timeout=8)
            s.close()
            results["tcp"] = (True, "connected")
        except Exception as e:
            results["tcp"] = (False, str(e)[:60])
        # 3. GAS reachable (bad key → still an HTTP answer means reachable)
        try:
            payload = json.dumps({"k": "wrong", "u": "https://example.com/",
                                  "m": "GET", "h": {}, "r": True}).encode()
            req = urllib.request.Request(
                f"https://script.google.com/macros/s/{sid}/exec", data=payload,
                headers={"content-type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20)
                results["gas"] = (True, "HTTP 200")
            except urllib.error.HTTPError as e:
                results["gas"] = (True, f"HTTP {e.code}")
        except Exception as e:
            results["gas"] = (False, str(e)[:60])
        # 4. Worker
        try:
            with urllib.request.urlopen(worker_url, timeout=15) as r:
                body = r.read(200).decode("utf-8", "replace")
                ok = "Relay is Active" in body
                results["worker"] = (ok, f"HTTP {r.status}")
        except Exception as e:
            results["worker"] = (False, str(e)[:60])
        self.root.after(0, self._doctor_done, results)

    def _doctor_done(self, results):
        all_ok = True
        for key, (dot, detail) in self.doctor_rows.items():
            ok, info = results.get(key, (False, "?"))
            dot.config(fg=self.GREEN if ok else self.RED)
            detail.config(text=info)
            all_ok = all_ok and ok
        if all_ok:
            msg = ("✅ " + ("مسیر رله کاملاً سالم است — می‌توانی وصل شوی."
                             if self.lang == "fa" else
                             "Relay path is fully working — you can connect."))
            col = self.GREEN
        elif results.get("dns", (False,))[0] or results.get("tcp", (False,))[0]:
            msg = ("⚠️ " + ("گوگل در دسترس است ولی زنجیره کامل نیست — مشکل از اسکریپت/ورکر است."
                            if self.lang == "fa" else
                            "Google is reachable but the chain is broken — check script/worker."))
            col = self.AMBER
        else:
            msg = ("❌ " + ("هیچ مسیر بین‌المللی‌ای نیست — در نت ملی کامل هیچ نرم‌افزاری کار نمی‌کند."
                            if self.lang == "fa" else
                            "No international path — no software can work on a full national intranet."))
            col = self.RED
        self.verdict_label.config(text=msg, foreground=col)

    # ── log tab ──────────────────────────────────────────────────────────
    def _build_log_tab(self, nb):
        import tkinter as tk
        from tkinter import ttk
        logf = ttk.Frame(nb, padding=12)
        nb.add(logf, text=self.t("log"))
        self.log_text = tk.Text(logf, bg="#080b13", fg="#c9d4e8", relief="flat",
                                font=("Consolas", 9), wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        for tag, col in (("ERROR", "#ff8a8a"), ("WARNING", "#ffcf7a"),
                         ("INFO", "#c9d4e8"), ("DEBUG", "#6b7690")):
            self.log_text.tag_config(tag, foreground=col)
        brow = ttk.Frame(logf)
        brow.pack(fill="x", pady=(8, 0))
        self.clear_btn = ttk.Button(brow, text=self.t("clear"), style="Ghost.TButton",
                                    command=self._clear_log)
        self.clear_btn.pack(side="left")
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.autoscroll_chk = ttk.Checkbutton(brow, text=self.t("autoscroll"),
                                              variable=self.autoscroll_var)
        self.autoscroll_chk.pack(side="left", padx=10)

    # ── profiles storage ─────────────────────────────────────────────────
    def _load_profiles(self):
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

    def _selected_profile(self):
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

    def _form_to_profile(self):
        vals = {k: e.get().strip() for k, e in self.form_vars.items()}
        if not vals["name"]:
            self._say("Profile name is required." if self.lang == "en"
                      else "نام پروفایل لازم است.", error=True)
            return None
        if not vals["script_id"] or vals["script_id"] in _PLACEHOLDER_KEYS:
            self._say("Enter a real Deployment ID." if self.lang == "en"
                      else "یک Deployment ID واقعی وارد کن.", error=True)
            return None
        if not vals["auth_key"] or vals["auth_key"] in _PLACEHOLDER_KEYS:
            self._say("Enter your AUTH_KEY." if self.lang == "en"
                      else "کلید امنیتی را وارد کن.", error=True)
            return None
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
            self._say("Name exists." if self.lang == "en" else "این نام تکراری است.",
                      error=True)
            return
        self.profiles.append(p)
        self._save_profiles()
        self._refresh_profile_list()
        self._say(f"Profile '{p['name']}' added.")

    def _profile_update(self):
        sel = self.profile_list.curselection()
        if not sel:
            return
        p = self._form_to_profile()
        if not p:
            return
        self.profiles[sel[0]] = p
        self._save_profiles()
        self._refresh_profile_list()

    def _profile_delete(self):
        sel = self.profile_list.curselection()
        if not sel:
            return
        self.profiles.pop(sel[0])
        self._save_profiles()
        self._refresh_profile_list()

    # ── connect / disconnect ─────────────────────────────────────────────
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
            self._say(self.t("create_profile_first"), error=True)
            return
        self.active_profile = p
        self.orb_state = "connecting"
        self.status_label.config(text=self.t("connecting"), foreground=self.AMBER)
        self._set_busy(True)
        threading.Thread(target=self._connect_worker, args=(p,), daemon=True).start()

    def _connect_worker(self, profile):
        try:
            config = build_config(profile)
            if _MITM_AVAILABLE:
                if not os.path.exists(CA_CERT_FILE):
                    log.info("Generating local MITM CA certificate…")
                    MITMCertManager()
                if not is_ca_trusted(CA_CERT_FILE):
                    log.warning("MITM CA not trusted — installing (may need admin)…")
                    install_ca(CA_CERT_FILE)
            self.engine.start(config)
            if self.sysproxy_var.get():
                try:
                    self.sysproxy.enable("127.0.0.1", config["listen_port"])
                    self.proxy_was_set = True
                    log.info("System proxy → 127.0.0.1:%s", config["listen_port"])
                except Exception as e:
                    log.warning("Could not set system proxy: %s", e)
            self.root.after(0, self._ui_connected, profile)
            log.info("Connected via '%s'", profile["name"])
        except Exception as e:
            log.error("Connect failed: %s", e)
            self.root.after(0, self._ui_connect_failed, str(e))
        finally:
            self.root.after(0, self._set_busy, False)

    def _ui_connected(self, profile):
        self.orb_state = "connected"
        self.status_label.config(
            text=f"{self.t('connected')} — {profile['name']}", foreground=self.GREEN)
        self.connect_btn.config(text=self.t("disconnect"))

    def _ui_connect_failed(self, err):
        self.orb_state = "idle"
        self.status_label.config(text=self.t("disconnected"), foreground=self.RED)
        self._say(f"Connect failed: {err}", error=True)

    def _disconnect(self):
        self._set_busy(True)
        threading.Thread(target=self._disconnect_worker, daemon=True).start()

    def _disconnect_worker(self):
        try:
            if self.proxy_was_set:
                self.sysproxy.disable()
                self.proxy_was_set = False
                log.info("System proxy restored.")
            # save session stats to history
            try:
                self._save_session_stats()
            except Exception as e:
                log.debug("stats history: %s", e)
            self.engine.stop()
            log.info("Disconnected.")
        finally:
            self.root.after(0, self._ui_disconnected)
            self.root.after(0, self._set_busy, False)

    def _ui_disconnected(self):
        self.orb_state = "idle"
        self.status_label.config(text=self.t("disconnected"), foreground=self.RED)
        self.connect_btn.config(text=self.t("connect"))
        for v in self.stat_vars.values():
            v.set("—")
        for row in self.hosts_tree.get_children():
            self.hosts_tree.delete(row)
        self._graph = deque([0.0] * 60, maxlen=60)
        self._last_bytes = 0
        self._disp = {"req": 0.0, "data": 0.0, "lat": 0.0}

    def _save_session_stats(self):
        if not self.active_profile:
            return
        snap = self.engine.stats
        sites = snap.get("per_site", [])
        total_req = sum(s["requests"] for s in sites)
        total_bytes = sum(s["bytes"] for s in sites)
        if total_req == 0:
            return
        path = os.path.join(os.path.dirname(PROFILES_PATH), "stats.json")
        try:
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            hist = {}
        name = self.active_profile.get("name", "?")
        h = hist.get(name, {"requests": 0, "bytes": 0, "sessions": 0})
        h["requests"] += total_req
        h["bytes"] += total_bytes
        h["sessions"] += 1
        hist[name] = h
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log.info("Session saved: %s requests, %s", f"{total_req:,}",
                 _fmt_bytes(total_bytes))

    # ── tools ────────────────────────────────────────────────────────────
    def _on_test_relay(self):
        p = self._selected_profile()
        if not p:
            self._say(self.t("create_profile_first"), error=True)
            return
        self._say("🧪 …")
        threading.Thread(target=self._test_worker,
                         args=(p["script_id"], p["auth_key"]), daemon=True).start()

    def _test_worker(self, sid, key):
        t0 = time.time()
        ok, detail = test_relay_chain(sid, key)
        ms = int((time.time() - t0) * 1000)
        msg = f"{'✅' if ok else '❌'} {detail} (⏱ {ms} ms)"
        log.info("Relay test: %s", msg)
        self.root.after(0, self._say, msg, not ok)

    def _on_scan(self):
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            from google_ip_scanner import scan_sync
            ok = scan_sync("www.google.com")
            self.root.after(0, self._say,
                            "Scan done — see Log." if ok else "Scan: no IP reachable.",
                            not ok)
        except Exception as e:
            self.root.after(0, self._say, f"Scan failed: {e}", True)

    def _on_install_cert(self):
        if not _MITM_AVAILABLE:
            self._say("Install requirements first: pip install -r requirements.txt",
                      error=True)
            return
        threading.Thread(target=self._cert_worker, daemon=True).start()

    def _cert_worker(self):
        try:
            if not os.path.exists(CA_CERT_FILE):
                MITMCertManager()
            ok = install_ca(CA_CERT_FILE)
            self.root.after(0, self._say,
                            "Certificate installed ✅" if ok
                            else "Failed — run as administrator.", not ok)
        except Exception as e:
            self.root.after(0, self._say, f"Failed: {e}", True)

    def _check_update(self):
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            req = urllib.request.Request(GITHUB_RELEASES_API,
                                         headers={"User-Agent": "MHR-Tunnel"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = str(data.get("tag_name", "")).lstrip("v")
            if tag and tag != APP_VERSION:
                self.root.after(0, self.update_label.config,
                                {"text": f"{self.t('update_available')}: v{tag}"})
        except Exception:
            pass

    # ── log helpers ──────────────────────────────────────────────────────
    def _say(self, msg, error=False):
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

    # ── periodic tick ────────────────────────────────────────────────────
    def _tick(self):
        self._drain_logs()
        if self.engine.running:
            snap = self.engine.stats
            sites = snap.get("per_site", [])
            total_req = sum(s["requests"] for s in sites)
            total_bytes = sum(s["bytes"] for s in sites)
            avg = (sum(s["avg_ms"] * s["requests"] for s in sites) / total_req
                   if total_req else 0)
            # graph sample: KB/s over last second
            kb_s = max(0.0, (total_bytes - self._last_bytes) / 1024.0)
            self._last_bytes = total_bytes
            self._graph.append(kb_s)
            self._draw_graph()
            # eased counters
            self._disp["req"] += (total_req - self._disp["req"]) * 0.25
            self._disp["data"] += (total_bytes - self._disp["data"]) * 0.25
            self._disp["lat"] += (avg - self._disp["lat"]) * 0.25
            self.stat_vars["requests"].set(f"{int(self._disp['req']):,}")
            self.stat_vars["data"].set(_fmt_bytes(int(self._disp["data"])))
            self.stat_vars["latency"].set(
                f"{self._disp['lat']:.0f} ms" if total_req else "—")
            if self.engine.connect_time:
                self.stat_vars["uptime"].set(
                    _fmt_uptime(time.time() - self.engine.connect_time))
            for row in self.hosts_tree.get_children():
                self.hosts_tree.delete(row)
            for s in sites[:12]:
                self.hosts_tree.insert("", "end", values=(
                    s["host"], f"{s['requests']:,}",
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


DEFAULT_WORKER_URL_FALLBACK = "https://mhr-relay.nikankh56-d72.workers.dev"


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def _run_headless(profile_name: str):
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
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    engine = RelayEngine()
    proxy = SystemProxy()
    proxy_set = False
    try:
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
                                 description="MHR Tunnel v2 — animated client "
                                             "for the MHR domain-fronted relay.")
    ap.add_argument("--no-gui", action="store_true", help="run headless")
    ap.add_argument("--profile", default=None, help="profile name (with --no-gui)")
    ap.add_argument("--lang", choices=["en", "fa"], default="fa",
                    help="interface language")
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
    gui = TunnelGUI(root)
    gui.lang = args.lang
    gui._retranslate()
    log.info("%s v%s started (relay core v%s).", APP_NAME, APP_VERSION, CORE_VERSION)
    root.mainloop()


if __name__ == "__main__":
    main()
