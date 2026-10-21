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

## ۶.۵ وضعیت NOTAM و خطوط هوایی باز

کالکتور NOTAM **کنار گذاشته شده** و در `settings.yaml` با `notam.enabled: false` خاموش است
(نیاز به کلید FAA داشت و کاربر آن را نخواست). کد باقی مانده تا در آینده با یک منبع
بی‌کلید جایگزین شود؛ هیچ سکرتی لازم نیست و هیچ هشداری در لاگ نمی‌آید.

پوشش خبری روی کشورهایی تنظیم شده که خط هوایی‌شان با ایران باز است (مهر ۱۴۰۵):

| وضعیت | کشورها / مقاصد |
|---|---|
| باز | چین (پکن، شانگهای، گوانگژو)، روسیه (مسکو)، ترکیه (ایران‌ایر)، افغانستان (کابل)، عراق (نجف)، تایلند (بانکوک، پوکت)، ویتنام، پاکستان |
| بسته | امارات، عمان، آذربایجان، گرجستان، قطر (تجدید ۲۹ نوامبر)، آلمان/اتریش (۲۴ اکتبر) |
| حریم هوایی | بخش شرقی ایران برای عبور باز، بخش غربی بسته |

فهرست منابع در `config/sources.yaml` و با کامنت «کشورهایی که خط هوایی‌شان با ایران باز مانده»
نگه‌داری می‌شود؛ با تغییر وضعیت، همان بلوک را به‌روز کن. تست: `python run.py --check-sources`

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