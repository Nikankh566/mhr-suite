<div align="center">

# 🛰️ مجموعه MHR

### کیت رلهٔ دامنه‌محور — Cloudflare Worker + Google Apps Script + پنل مدیریت + اپ دسکتاپ

**English:** full English guide in [README.md](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](apps/mhr-tunnel/)
[![Release](https://img.shields.io/badge/release-v1.0.0-green.svg)](https://github.com/Nikankh566/mhr-suite/releases)

*ترافیک شما داخل اتصال‌های TLS می‌رود که برای شبکه فقط `www.google.com`
دیده می‌شود — بعد از Google Apps Scriptِ خودتان و Cloudflare Workerِ خودتان
عبور می‌کند و به اینترنت واقعی می‌رسد.*

[🚀 شروع سریع](#-نصب--صفر-تا-صد) •
[💻 اپ MHR Tunnel](#-mhr-tunnel--اپ-دسکتاپ) •
[🛠️ پنل مدیریت](#%EF%B8%8F-پنل-مدیریت) •
[❓ عیب‌یابی](#-عیبیابی)

</div>

---

## ✨ چه چیزهایی می‌گیرید

| جزء | توضیح |
|---|---|
| 🛰️ **خروجی رله** | `deploy/cloudflare-worker/worker.js` — ورکر کلادفلر که سایت مقصد را فچ می‌کند |
| 🔀 **فرانت گوگل** | `deploy/gas/Code.gs` — اسکریپت شما؛ شبکه فقط `www.google.com` را می‌بیند |
| 🛠️ **پنل مدیریت** | `deploy/panel/panel.js` — پنل وب: تست رله، سازندهٔ کانفیگ، کانفیگ Xray، چرخش کلید |
| 💻 **MHR Tunnel** | `apps/mhr-tunnel/` — اپ پیشرفتهٔ پایتون: اتصال تک‌کلیک، پروکسی سیستمی، داشبورد زنده |

## 🔁 طرز کار

```
دستگاه شما
    │
    │  MHR Tunnel (پروکسی محلی 127.0.0.1:8085)
    ▼
www.google.com   ◄── ISP فقط همین را می‌بیند (TLS رمزشده)
    │
    │  Google Apps Script (حساب گوگل خودتان)
    ▼
Cloudflare Worker (حساب کلادفلر خودتان)  ◄── سایت واقعی را فچ می‌کند
    │
    ▼
سایت مقصد 🌍
```

**قبل از شروع بدانید:**
- ✅ برای **اختلال جزئی** ساخته شده (فیلترینگ/کندی وقتی اینترنت بین‌الملل وصل است).
- ⚠️ در **قطعی کامل** هیچ رلهٔ نرم‌افزاری‌ای کار نمی‌کند.
- 📊 سهمیهٔ رایگان Apps Script حدود ۲۰٬۰۰۰ فچ در روز است — برای مصرف شخصی کافی است.

---

## 🚀 نصب — صفر تا صد

<details>
<summary><b>پیش‌نیازها</b></summary>

- حساب **Cloudflare** (رایگان کافی است)
- حساب **Google** (هر Gmail)
- **Python 3.10+** روی کامپیوتری که کلاینت را اجرا می‌کند

</details>

### قدم ۱ — دیپلوی Worker رله ☁️

۱. وارد [داشبورد کلادفلر](https://dash.cloudflare.com) شوید
۲. **Compute → Workers & Pages → Create → Create Worker**
۳. اسم `mhr-relay` → Deploy
۴. **Edit code** → همه را پاک کنید → محتوای `deploy/cloudflare-worker/worker.js` را پیست کنید
۵. بالای فایل هاست خودتان را بگذارید:
   ```js
   const WORKER_URL = "mhr-relay.<your-subdomain>.workers.dev";
   ```
۶. **Deploy** ✅ — آدرس را باز کنید، باید `{"e":"Relay is Active."}` ببینید

### قدم ۲ — دیپلوی پنل مدیریت 🛠️

مثل قدم ۱، با فایل `deploy/panel/panel.js` در ورکری به اسم `mhr-panel`.
**رمز پنل را عوض کنید:**

```js
const PANEL_PASSWORD = "CHANGE_ME_TO_A_STRONG_PANEL_PASSWORD";
```

### قدم ۳ — دیپلوی Google Apps Script 📜

۱. [script.google.com](https://script.google.com) → **New project**
۲. کد پیش‌فرض را پاک کنید → محتوای `deploy/gas/Code.gs` را پیست کنید
۳. مقادیر خودتان:
   ```js
   const AUTH_KEY   = "CHANGE_ME_TO_A_STRONG_SECRET";
   const WORKER_URL = "https://mhr-relay.<your-subdomain>.workers.dev";
   ```
۴. **Deploy → New deployment → ⚙ Web app** → *Execute as:* Me، *Who has access:* Anyone
۵. Authorize کنید و **Deployment ID** را کپی کنید

> برای به‌روزرسانی: **Deploy → Manage deployments → ✏️ → New version**

### قدم ۴ — تنظیم پنل ⚙️

وارد پنل شوید → **تنظیمات اتصال** → آدرس Worker، Deployment ID و AUTH_KEY →
**ذخیره**. بعد:

- **🧪 تست Worker** — آیا رله روی کلادفلر زنده است؟
- **🧪 تست کامل زنجیره** — درخواست واقعی از مسیر `google.com ← Apps Script ← Worker`

### قدم ۵ — اجرای MHR Tunnel 💻

```bash
cd apps/mhr-tunnel
pip install -r requirements.txt
# لینوکس: sudo apt install python3-tk
python mhr_tunnel.py
```

۱. تب **Profiles** → Deployment ID و AUTH_KEY را وارد کنید
۲. **Connect** را بزنید — اپ خودش **پروکسی سیستم** را ست می‌کند
۳. وب‌گردی کنید 🌍 — با **Disconnect** همه‌چیز به حالت عادی برمی‌گردد

---

## 💻 MHR Tunnel — اپ دسکتاپ

اپ تک‌کلیکی که **کل کامپیوتر** را تانل می‌کند (نه فقط مرورگر):

- 🖱️ **اتصال تک‌کلیک** — موقع وصل شدن پروکسی سیستم را ست می‌کند، موقع قطع شدن برمی‌گرداند
- 👤 **پروفایل‌ها** — چند رله، جابه‌جایی با یک کلیک
- 📊 **داشبورد زنده** — تعداد درخواست‌ها، حجم داده، تأخیر، آپ‌تایم، پرترافیک‌ترین سایت‌ها
- 🧪 **تست رله** — چک سلامت زنجیره بدون خروج از اپ
- 📡 **اسکنر IP گوگل** — پیدا کردن سریع‌ترین IP فرانت
- 📝 **لاگ رنگی داخل اپ**
- ⌨️ **حالت هدلس** — `python mhr_tunnel.py --no-gui --profile "My Relay"`

روی **ویندوز**، **macOS** و **لینوکس** کار می‌کند.

## 🛠️ پنل مدیریت

| تب | کار |
|---|---|
| 🧪 تست | چک سلامت Worker + تست کامل زنجیره |
| 📦 ساخت کانفیگ | تولید `config.json` آماده برای کلاینت |
| 🔀 کانفیگ Xray | تولید `xray-config.json` (ایمپورت در NekoBox / Hiddify) |
| 🔑 چرخش کلید | ساخت AUTH_KEY جدید + راهنمای دیپلوی مجدد |

> ⚠️ کانفیگ Xray هم به کلاینت محلی MHR نیاز دارد — Xray فقط ترافیک را به رله می‌دهد.

---

## 🔑 چرخش کلید امنیتی

۱. پنل → **چرخش کلید** → ساخت کلید جدید
۲. `AUTH_KEY` را در Apps Script عوض کنید → **Deploy → Manage deployments → New version**
۳. `config.json` تازه بسازید (یا پروفایل را در MHR Tunnel به‌روز کنید)

## ❓ عیب‌یابی

| علامت | راه‌حل |
|---|---|
| آدرس Worker عبارت Relay is Active را نشان نمی‌دهد | کد پیست/دیپلوی نشده — قدم ۱ را تکرار کنید |
| تست زنجیره: `unauthorized` | AUTH_KEY با Apps Script یکی نیست |
| تست زنجیره: timeout | `script.google.com` در دسترس نیست یا Apps Script با دسترسی Anyone دیپلوی نشده |
| چیزی لود نمی‌شود | کلاینت روشن نیست / پروکسی سیستم ست نشده |
| یوتیوب لود می‌شود ولی ویدیو نه | محدودیت شناخته‌شدهٔ Apps Script |
| لوپ CAPTCHA | خروجی Worker از IPهای چرخشی می‌آید — طبیعی است |

## 🔒 نکته‌های امنیتی

- `AUTH_KEY` و رمز پنل مثل رمز عبورند — ریپو فقط `CHANGE_ME` دارد،
  **هیچ‌وقت** مقادیر واقعی را کامیت نکنید.
- تنظیمات پنل فقط در مرورگر شما (`localStorage`) ذخیره می‌شود.

## 🙏 سپاس

پروتکل رله: [denuitt1/mhr-cfw](https://github.com/denuitt1/mhr-cfw) (MIT).
پنل مدیریت، اپ MHR Tunnel، راهنماها و بسته‌بندی: همین ریپو (MIT).
