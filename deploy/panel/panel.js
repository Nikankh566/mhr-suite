// MHR-CFW Management Panel — Cloudflare Worker
// Features: relay health test, end-to-end relay test, client config generator, auth-key rotation

const PANEL_PASSWORD = "CHANGE_ME_TO_A_STRONG_PANEL_PASSWORD";
const DEFAULT_WORKER_URL = "https://mhr-relay.nikankh56-d72.workers.dev";

function configTemplate(scriptId, authKey, listenPort) {
  return {
    mode: "apps_script",
    google_ip: "216.239.38.120",
    front_domain: "www.google.com",
    script_id: scriptId,
    auth_key: authKey,
    listen_host: "127.0.0.1",
    socks5_enabled: true,
    listen_port: listenPort || 8085,
    socks5_port: 1080,
    log_level: "INFO",
    verify_ssl: true,
    lan_sharing: true,
    relay_timeout: 25,
    tls_connect_timeout: 15,
    tcp_connect_timeout: 10,
    max_response_body_bytes: 209715200,
    parallel_relay: 1,
    chunked_download_extensions: [".bin", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk", ".iso", ".img", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".flac", ".wav", ".aac", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".wasm"],
    chunked_download_min_size: 5242880,
    chunked_download_chunk_size: 524288,
    chunked_download_max_parallel: 8,
    chunked_download_max_chunks: 256,
    block_hosts: [],
    bypass_hosts: ["localhost", ".local", ".lan", ".home.arpa"],
    forwarder_hosts: [],
    direct_google_exclude: ["gemini.google.com", "aistudio.google.com", "notebooklm.google.com", "labs.google.com", "meet.google.com", "accounts.google.com", "ogs.google.com", "mail.google.com", "calendar.google.com", "drive.google.com", "docs.google.com", "chat.google.com", "maps.google.com", "play.google.com", "translate.google.com", "assistant.google.com", "lens.google.com"],
    direct_google_allow: ["www.google.com", "safebrowsing.google.com"],
    youtube_via_relay: false,
    hosts: {}
  };
}

function xrayTemplate(relayPort, socksPort) {
  return {
    log: { loglevel: "warning" },
    inbounds: [
      { port: socksPort || 10808, listen: "127.0.0.1", protocol: "socks", settings: { auth: "noauth", udp: true } }
    ],
    outbounds: [
      { protocol: "http", settings: { servers: [{ address: "127.0.0.1", port: relayPort || 8085 }] }, tag: "mhr-relay" },
      { protocol: "freedom", tag: "direct" }
    ],
    routing: { rules: [] }
  };
}

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { "content-type": "application/json; charset=utf-8" }
  });
}

function checkAuth(body) {
  return body && body.password === PANEL_PASSWORD;
}

function randomKey(n) {
  const bytes = new Uint8Array(n || 32);
  crypto.getRandomValues(bytes);
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += chars[bytes[i] % 64];
  return s;
}

const PAGE = `<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>پنل مدیریت MHR</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1420;color:#e8ecf4;font-family:Tahoma,Arial,sans-serif;min-height:100vh;padding:20px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:20px}
.card{background:#182030;border:1px solid #263049;border-radius:12px;padding:18px;margin-bottom:14px}
label{display:block;font-size:13px;color:#aab3c8;margin:10px 0 4px}
input,select{width:100%;background:#0f1420;border:1px solid #2c3a55;color:#e8ecf4;border-radius:8px;padding:10px;font-size:14px;direction:ltr;text-align:left}
input[type=password]{}
button{background:#2f6fed;border:none;color:#fff;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer;margin-top:12px;font-family:inherit}
button:hover{background:#3d7dff}
button.ghost{background:#263049}
button:disabled{opacity:.5;cursor:default}
.tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.tab{background:#182030;border:1px solid #263049;color:#aab3c8;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:14px;font-family:inherit}
.tab.active{background:#2f6fed;color:#fff;border-color:#2f6fed}
.hidden{display:none}
.result{margin-top:12px;padding:12px;border-radius:8px;font-size:13px;line-height:1.9;white-space:pre-wrap;word-break:break-all;direction:ltr;text-align:left}
.ok{background:#0f2e1f;border:1px solid #1f6b3f;color:#9fe8b8}
.err{background:#33141a;border:1px solid #8a2b3a;color:#f5a8b5}
pre.code{background:#0b0f18;border:1px solid #263049;border-radius:8px;padding:12px;overflow:auto;direction:ltr;text-align:left;font-size:12px;max-height:320px;margin-top:10px}
.row{display:flex;gap:8px}
.row>*{flex:1}
.note{font-size:12px;color:#8b93a7;line-height:2;margin-top:10px}
#gate{max-width:380px;margin:80px auto}
h2{font-size:17px;margin-bottom:10px}
</style>
</head>
<body>
<div class="wrap" id="gate">
  <div class="card">
    <h2>🔐 ورود به پنل مدیریت MHR</h2>
    <label>رمز پنل</label>
    <input type="password" id="pw" placeholder="panel password">
    <button onclick="login()">ورود</button>
    <div class="result err hidden" id="pwerr">رمز اشتباه است.</div>
  </div>
</div>
<div class="wrap hidden" id="app">
  <h1>🛰️ پنل مدیریت MHR</h1>
  <div class="sub">تست رله • ساخت کانفیگ • چرخش کلید</div>
  <div class="card">
    <h2>⚙️ تنظیمات اتصال</h2>
    <label>آدرس Worker کلادفلر</label>
    <input id="workerUrl" placeholder="https://mhr-relay.xxxxx.workers.dev">
    <label>Deployment ID اسکریپت گوگل</label>
    <input id="scriptId" placeholder="AKfyc...">
    <label>کلید امنیتی (AUTH_KEY)</label>
    <input id="authKey" placeholder="auth key">
    <button onclick="saveSettings()">💾 ذخیره تنظیمات</button>
    <div class="note">این مقادیر فقط در مرورگر خودت (localStorage) ذخیره می‌شن و به هیچ سروری ارسال نمی‌شن.</div>
  </div>
  <div class="tabs">
    <button class="tab active" onclick="showTab('test',this)">🧪 تست رله</button>
    <button class="tab" onclick="showTab('config',this)">📦 ساخت کانفیگ</button>
    <button class="tab" onclick="showTab('xray',this)">🔀 کانفیگ Xray</button>
    <button class="tab" onclick="showTab('rotate',this)">🔑 چرخش کلید</button>
  </div>
  <div class="card" id="tab-test">
    <h2>🧪 تست رله</h2>
    <div class="row">
      <button onclick="testWorker()">تست Worker</button>
      <button class="ghost" onclick="testRelay()">تست کامل زنجیره (تا گوگل)</button>
    </div>
    <div id="testOut"></div>
    <div class="note">تست Worker: بررسی می‌کند کد رله روی کلادفلر فعال است.<br>تست کامل: یک درخواست واقعی از مسیر google.com ← Apps Script ← Worker می‌فرستد.</div>
  </div>
  <div class="card hidden" id="tab-config">
    <h2>📦 ساخت کانفیگ آماده</h2>
    <label>پورت پروکسی (پیش‌فرض 8085)</label>
    <input id="cfgPort" value="8085">
    <button onclick="genConfig()">ساخت config.json</button>
    <button class="ghost" onclick="downloadConfig()">⬇️ دانلود فایل</button>
    <pre class="code hidden" id="cfgOut"></pre>
    <div class="note">فایل را کنار main.py بگذار و با <span dir="ltr">python main.py</span> اجرا کن.</div>
  </div>
  <div class="card hidden" id="tab-xray">
    <h2>🔀 کانفیگ Xray (فرمت v2ray)</h2>
    <label>پورت رله محلی MHR (پیش‌فرض 8085)</label>
    <input id="xrayRelayPort" value="8085">
    <label>پورت SOCKS ورودی Xray (پیش‌فرض 10808)</label>
    <input id="xraySocksPort" value="10808">
    <button onclick="genXray()">ساخت کانفیگ Xray</button>
    <button class="ghost" onclick="downloadXray()">⬇️ دانلود فایل</button>
    <pre class="code hidden" id="xrayOut"></pre>
    <div class="note">این کانفیگ را می‌توانی در NekoBox / Hiddify (کانفیگ دستی) یا xray مستقیم ایمپورت کنی.<br>⚠️ اول باید کلاینت MHR (main.py) روشن باشد تا رله روی پورت 8085 جواب بدهد؛ Xray ترافیک را از آن رد می‌کند.</div>
  </div>
  <div class="card hidden" id="tab-rotate">
    <h2>🔑 چرخش کلید امنیتی</h2>
    <button onclick="rotateKey()">ساخت کلید جدید</button>
    <pre class="code hidden" id="rotOut"></pre>
    <div class="note">بعد از ساخت کلید جدید:<br>۱. خط AUTH_KEY را در Code.gs عوض کن<br>۲. در Apps Script: Deploy ← Manage deployments ← New version ← Deploy<br>۳. با کلید جدید یک کانفیگ تازه بساز</div>
  </div>
</div>
<script>
let PW="";
function login(){
  PW=document.getElementById('pw').value;
  api('/api/ping',{},{ok:r=>{document.getElementById('gate').classList.add('hidden');document.getElementById('app').classList.remove('hidden');loadSettings();},err:()=>{document.getElementById('pwerr').classList.remove('hidden');}});
}
function api(path,data,cb){
  fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(Object.assign({password:PW},data))})
  .then(r=>r.json().then(j=>({s:r.status,j})))
  .then(({s,j})=>{ if(s===401){(cb.err||(()=>{}))();return;} cb.ok(j); })
  .catch(e=>{(cb.err||(()=>{}))(e);});
}
function showTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  ['test','config','xray','rotate'].forEach(t=>document.getElementById('tab-'+t).classList.add('hidden'));
  document.getElementById('tab-'+name).classList.remove('hidden');
}
function saveSettings(){
  localStorage.setItem('mhr_workerUrl',document.getElementById('workerUrl').value.trim());
  localStorage.setItem('mhr_scriptId',document.getElementById('scriptId').value.trim());
  localStorage.setItem('mhr_authKey',document.getElementById('authKey').value.trim());
  alert('ذخیره شد ✅');
}
function loadSettings(){
  document.getElementById('workerUrl').value=localStorage.getItem('mhr_workerUrl')||'${DEFAULT_WORKER_URL}';
  document.getElementById('scriptId').value=localStorage.getItem('mhr_scriptId')||'';
  document.getElementById('authKey').value=localStorage.getItem('mhr_authKey')||'';
}
function settings(){return{workerUrl:document.getElementById('workerUrl').value.trim(),scriptId:document.getElementById('scriptId').value.trim(),authKey:document.getElementById('authKey').value.trim()};}
function out(html,ok){
  const d=document.getElementById('testOut');
  d.className='result '+(ok?'ok':'err');d.textContent=html;
}
function testWorker(){
  const s=settings();out('در حال تست...',true);
  api('/api/test-worker',{workerUrl:s.workerUrl},{ok:r=>out((r.ok?'✅ ':'❌ ')+r.detail,r.ok)});
}
function testRelay(){
  const s=settings();out('در حال تست زنجیره کامل...',true);
  api('/api/test-relay',{scriptId:s.scriptId,authKey:s.authKey},{ok:r=>out((r.ok?'✅ ':'❌ ')+r.detail,r.ok)});
}
let lastCfg="";
function genConfig(){
  const s=settings();const port=parseInt(document.getElementById('cfgPort').value)||8085;
  api('/api/gen-config',{scriptId:s.scriptId,authKey:s.authKey,port},{ok:r=>{
    lastCfg=r.config;const p=document.getElementById('cfgOut');
    p.textContent=r.config;p.classList.remove('hidden');
  }});
}
function downloadConfig(){
  if(!lastCfg){alert('اول «ساخت config.json» را بزن');return;}
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lastCfg],{type:'application/json'}));
  a.download='config.json';a.click();
}
let lastXray="";
function genXray(){
  const rp=parseInt(document.getElementById('xrayRelayPort').value)||8085;
  const sp=parseInt(document.getElementById('xraySocksPort').value)||10808;
  api('/api/gen-xray',{relayPort:rp,socksPort:sp},{ok:r=>{
    lastXray=r.config;const p=document.getElementById('xrayOut');
    p.textContent=r.config;p.classList.remove('hidden');
  }});
}
function downloadXray(){
  if(!lastXray){alert('اول «ساخت کانفیگ Xray» را بزن');return;}
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lastXray],{type:'application/json'}));
  a.download='xray-config.json';a.click();
}
function rotateKey(){
  api('/api/rotate-key',{},{ok:r=>{
    const p=document.getElementById('rotOut');p.classList.remove('hidden');
    p.textContent='کلید جدید:\\n'+r.newKey+'\\n\\nدر Code.gs جایگزین کن:\\nconst AUTH_KEY = "'+r.newKey+'";\\n\\nبعد Deploy ← Manage deployments ← New version';
    document.getElementById('authKey').value=r.newKey;saveSettings();
  }});
}
</script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    if (request.method === "POST" && url.pathname.startsWith("/api/")) {
      let body = {};
      try { body = await request.json(); } catch (e) { return json({ e: "bad json" }, 400); }
      if (!checkAuth(body)) return json({ e: "unauthorized" }, 401);

      if (url.pathname === "/api/ping") return json({ ok: true });

      if (url.pathname === "/api/test-worker") {
        const wu = String(body.workerUrl || "").trim();
        if (!/^https:\/\//.test(wu)) return json({ ok: false, detail: "worker URL invalid" });
        try {
          const r = await fetch(wu, { method: "GET", signal: AbortSignal.timeout(15000) });
          const t = await r.text();
          const good = t.includes("Relay is Active");
          return json({ ok: good, detail: "HTTP " + r.status + " :: " + t.slice(0, 120) });
        } catch (e) { return json({ ok: false, detail: "fetch failed: " + (e.message || e) }); }
      }

      if (url.pathname === "/api/test-relay") {
        const sid = String(body.scriptId || "").trim();
        const key = String(body.authKey || "").trim();
        if (!sid || !key) return json({ ok: false, detail: "scriptId/authKey missing — fill settings first" });
        const execUrl = "https://script.google.com/macros/s/" + sid + "/exec";
        try {
          const r = await fetch(execUrl, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ k: key, u: "https://example.com/", m: "GET", h: {}, r: true }),
            signal: AbortSignal.timeout(30000)
          });
          const t = await r.text();
          let j = null;
          try { j = JSON.parse(t); } catch (e) {}
          if (j && j.s === 200) return json({ ok: true, detail: "relay OK — example.com fetched with HTTP 200 through google.com → GAS → worker" });
          if (j && j.e) return json({ ok: false, detail: "relay error: " + j.e });
          return json({ ok: false, detail: "unexpected response: " + t.slice(0, 160) });
        } catch (e) { return json({ ok: false, detail: "fetch failed: " + (e.message || e) }); }
      }

      if (url.pathname === "/api/gen-config") {
        const sid = String(body.scriptId || "").trim();
        const key = String(body.authKey || "").trim();
        if (!sid || !key) return json({ e: "scriptId/authKey missing" }, 400);
        const port = parseInt(body.port) || 8085;
        const cfg = configTemplate(sid, key, port);
        return json({ config: JSON.stringify(cfg, null, 2) });
      }

      if (url.pathname === "/api/rotate-key") {
        return json({ newKey: randomKey(32) });
      }

      if (url.pathname === "/api/gen-xray") {
        const rp = parseInt(body.relayPort) || 8085;
        const sp = parseInt(body.socksPort) || 10808;
        return json({ config: JSON.stringify(xrayTemplate(rp, sp), null, 2) });
      }

      return json({ e: "not found" }, 404);
    }
    return new Response("not found", { status: 404 });
  }
};
