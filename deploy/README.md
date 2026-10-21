# استقرار روی OpenShift Developer Sandbox

رایگان، بدون کارت اعتباری، با دیسک ماندگار و یک pod واقعی ۲۴/۷.

## چرا این گزینه

| نیاز ما | OpenShift Sandbox |
|---|---|
| پروسه‌ی همیشگی (برای Telethon) | ✅ |
| اتصال خروجی به تلگرام | ✅ |
| دیسک ماندگار برای SQLite | ✅ |
| بدون کارت اعتباری | ✅ |
| بدون root | ✅ (کد ما هم root نمی‌خواهد) |
| عمر | ۳۰ روز، تمدیدشدنی |

## ۱. پیش‌نیازها

1. حساب Red Hat (رایگان) و درخواست sandbox:
   <https://developers.redhat.com/developer-sandbox>
2. نصب `oc`:
   <https://developers.redhat.com/openshift/downloads>
3. لاگین با توکنی که در صفحه‌ی sandbox نشان می‌دهد:

```bash
oc login --token=sha256~... --server=https://api.sandbox-m2.ll9k.p1.openshiftapps.com:6443
```

## ۲. آماده‌سازی ریپو

ریپو باید **پابلیک** باشد، چون بیلد داخل کلاستر بدون اعتبارنامه به آن دسترسی می‌گیرد.

```bash
git remote add origin https://github.com/USERNAME/iran-airspace-watch.git
git push -u origin main
```

## ۳. تنظیمات

```bash
cp .env.example .env
notepad .env     # یا nano
```

حداقلِ لازم:

```env
TELEGRAM_BOT_TOKEN=...
LLM_API_KEY=...
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-2.0-flash
```

## ۴. استقرار

**ویندوز / PowerShell** (بدون نیاز به Git Bash):

```powershell
.\deploy\deploy.ps1
```

**لینوکس / macOS / Git Bash:**

```bash
chmod +x deploy/deploy.sh
REPO=https://github.com/USERNAME/iran-airspace-watch.git ./deploy/deploy.sh
```

اسکریپت این کارها را می‌کند:

1. سکرت `airwatch-secrets` را از `.env` می‌سازد
2. ImageStream + BuildConfig + PVC را اعمال می‌کند
3. ایمیج را داخل کلاستر بیلد می‌کند
4. Deployment را مستقر می‌کند و منتظر آماده‌شدن می‌ماند

بعد از آن، یک بار در تلگرام به بات `/start` بده.

## ۵. بررسی

```bash
oc get pods
oc logs -f deployment/airwatch
oc get pvc
```

سلامت سرویس:

```bash
POD=$(oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}')
oc exec "$POD" -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/healthz').read().decode())"
```

اگر جواب `"status": "ok"` بود، حلقه‌ی اصلی سالم است. اگر `stale` شد، k8s خودش pod را ری‌استارت می‌کند.

## ۶. تمدید ۳۰ روزه

sandbox بعد از ۳۰ روز منقضی می‌شود. قبل از انقضا:

```bash
./deploy/backup.sh                 # بکاپ دیتابیس روی سیستم خودت
```

بعد از گرفتن sandbox جدید:

```bash
oc login --token=... --server=...
REPO=https://github.com/USERNAME/iran-airspace-watch.git ./deploy/deploy.sh
./deploy/backup.sh restore backups/data-YYYY-MM-DD-HHMMSS.tgz
oc rollout restart deployment/airwatch
```

بدون بکاپ، تاریخچه‌ی دِداپ و پیام‌های ریشه از دست می‌رود (خبر جدید دوباره ارسال می‌شود، ولی چیزی نمی‌شکند).

## ۶.۵ فعال‌کردن NOTAM (رایگان)

بدون کلید FAA، کالکتور NOTAM خاموش می‌ماند و در لاگ این هشدار را می‌بینی:

```
NOTAM غیرفعال است: FAA_NOTAM_CLIENT_ID/SECRET تنظیم نشده
```

1. ثبت‌نام در پورتال API سازمان هوانوردی: <https://api.faa.gov>
2. در پورتال، API مربوط به NOTAM را جست‌وجو کن و برای نسخه‌ی Production درخواست اشتراک بده (approval لازم است)
3. بعد از تأیید، `client_id` و `client_secret` را برمی‌داری و در `.env` می‌گذاری:

```
FAA_NOTAM_CLIENT_ID=...
FAA_NOTAM_CLIENT_SECRET=...
```

4. فقط سکرت را به‌روز کن و پاد را ری‌استارت کن (بیلد لازم نیست):

> نکته: آدرس `https://external-api.faa.gov/notamapi` که در نسخه‌های قبلی گفته شده بود **صفحه‌ی ثبت‌نام نیست**؛ ریشه‌ی خود API است و در مرورگر خطای
> `No context-path matches the request URI.` می‌دهد. این خطا طبیعی است.
> آدرس واقعی که کد صدا می‌زند `https://external-api.faa.gov/notamapi/v1/notams` است و بدون کلید `401 Unauthorized` برمی‌گرداند.

```powershell
oc create secret generic airwatch-secrets --from-env-file=.env --dry-run=client -o yaml | oc apply -f -
oc rollout restart deployment/airwatch
```

## ۷. عیب‌یابی

| نشانه | علت | راه‌حل |
|---|---|---|
| `ImagePullBackOff` | بیلد تمام نشده | `oc start-build airwatch --follow` |
| `CrashLoopBackOff` | خطای پیکربندی | `oc logs deployment/airwatch --previous` |
| `403` در لاگ تلگرام | توکن غلط یا باطل | توکن را در BotFather چک کن |
| بیلد `denied` می‌دهد | ریپو پابلیک نیست | ریپو را پابلیک کن یا از `oc set build-secret` استفاده کن |
| `stale` در healthz | حلقه‌ی اصلی hang کرده | خودکار ری‌استارت می‌شود؛ اگر تکرار شد لاگ را ببین |
| پیام نمی‌آید ولی لاگ سالم است | `/start` نزده‌ای | یک بار به بات پیام بده |
| بعد از بیلد جدید کد عوض نشده | تگ `:latest` عوض نمی‌شود | `oc rollout restart deployment/airwatch` (اسکریپت خودش انجام می‌دهد) |

## ۸. محدودیت‌ها

- **یک replica** — SQLite و `getUpdates` بیش از یکی را پشتیبانی نمی‌کنند. `Recreate` هم برای همین است.
- **بدون root** — تصویر با UID تصادفی اجرا می‌شود؛ `Dockerfile` با `chmod -R g=u` آماده است.
- **منابع محدود** — ۵۰m CPU و ۱۲۸Mi رم درخواست کرده‌ایم که برای این بار کافی است. اگر sandbox سخت‌گیر بود، `requests` را کم کن.
- **بدون Route عمومی** — بات فقط خروجی می‌زند؛ لازم نیست از اینترنت به آن دسترسی باشد. probeها از داخل pod کار می‌کنند.
- **پس از هر تغییر کد** باید دوباره بیلد کنی: `oc start-build airwatch --follow && oc rollout restart deployment/airwatch`