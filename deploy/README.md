# استقرار روی OpenShift Developer Sandbox

رایگان، بدون کارت اعتباری، با دیسک ماندگار و **بیدارنگه‌دارنده‌ی خودکار داخلی (`CronJob`)** در برابر خاموشی ۱۲ساعته‌ی Sandbox.

## چرا این معماری؟

OpenShift Developer Sandbox یک کنترلر سیستمی (`Idler`) دارد که هر Pod با عمر بیش از **۱۲ ساعت** (یا بیش از ۵۰ ری‌استارت) را خاموش می‌کند و مقدار `replicas` را روی `0` می‌گذارد، اما روی **`CronJob`** اعمال نمی‌شود.  
به همین دلیل در `deploy/app.yaml` علاوه بر `Deployment/airwatch`، یک `CronJob/airwatch-keepalive` (هر ۵ دقیقه، با همان ایمیج داخلی و بدون کشیدن ایمیج خارجی) مستقر می‌شود که:

1. **لایه پیشگیرانه:** وقتی عمر Pod فعلی به **۱۱ ساعت** برسد (یک ساعت زودتر از سقف ۱۲ ساعت Sandbox)، یک Rollout Restart نرم می‌زند تا Pod با خروج ایمن `SIGTERM` جایگزین شود و تایمرش صفر شود.
2. **لایه خودترمیمی:** اگر به هر دلیلی `Deployment/airwatch` روی `replicas: 0` رفته باشد، در کمتر از ۵ دقیقه خودکار آن را به `replicas: 1` برمی‌گرداند.

| نیاز ما | وضعیت در OpenShift Sandbox |
|---|---|
| اجرای پیوسته‌ی ۲۴/۷ | ✅ با `Deployment` + `CronJob` بیدارنگه‌دارنده (`airwatch-keepalive`) |
| اتصال خروجی به تلگرام | ✅ |
| دیسک ماندگار برای SQLite | ✅ (`PersistentVolumeClaim` یک گیگابایتی) |
| سلامت‌سنج واقعی (`/healthz`) | ✅ نظارت بر حلقه‌ی اصلی، کالکتور RSS و نخ تلگرام |
| بدون کارت اعتباری / بدون root | ✅ |
| عمر اکانت Sandbox | ۳۰ روز، قابل تمدید رایگان با بکاپ اتمی |

## ۱. پیش‌نیازها

1. حساب Red Hat (رایگان) و درخواست sandbox:
   <https://developers.redhat.com/developer-sandbox>
2. نصب `oc`:
   <https://developers.redhat.com/openshift/downloads>
3. لاگین با توکنی که در صفحه‌ی sandbox نشان می‌دهد (در Web Console گزینه `Copy login command`):

```bash
oc login --token=sha256~... --server=https://api.sandbox-m2.ll9k.p1.openshiftapps.com:6443
```

## ۲. آماده‌سازی ریپو

ریپو باید **پابلیک** باشد، چون بیلد داخل کلاستر بدون اعتبارنامه به آن دسترسی می‌گیرد.

```bash
git push origin main
```

## ۳. تنظیمات

```bash
cp .env.example .env
notepad .env     # یا nano
```

توصیه می‌شود علاوه‌بر توکن بات، **`TELEGRAM_CHAT_ID` خودتان را هم در `.env` بنویسید** تا حتی بدون `/start` هم گیرنده همیشه مشخص باشد:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# ترجمه‌ی رایگان با زنجیره‌ی خودکار Gemini ← Groq ← OpenRouter؛
# هر کلیدی را که داری پر کن، بقیه خالی بمانند:
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
```

## ۴. استقرار یا به‌روزرسانی کامل

**ویندوز / PowerShell:**

```powershell
.\deploy\deploy.ps1
```

**لینوکس / macOS / Git Bash:**

```bash
chmod +x deploy/deploy.sh
REPO=https://github.com/USERNAME/iran-airspace-watch.git ./deploy/deploy.sh
```

اسکریپت این کارها را خودکار انجام می‌دهد:

1. سکرت `airwatch-secrets` را از `.env` می‌سازد/به‌روز می‌کند
2. `ImageStream` + `BuildConfig` + `PVC` را اعمال می‌کند
3. ایمیج جدید را داخل کلاستر بیلد می‌کند
4. `Deployment/airwatch` به همراه `ServiceAccount`، `Role`، `RoleBinding` و **`CronJob/airwatch-keepalive`** را مستقر و ری‌استارت می‌کند

## ۵. بررسی وضعیت

```bash
oc get deploy,cronjob,pods
oc logs -f deployment/airwatch
```

تست دستی بیدارنگه‌دارنده در هر لحظه:

```bash
oc create job --from=cronjob/airwatch-keepalive test-keepalive
oc logs job/test-keepalive
oc delete job test-keepalive
```

بررسی سلامت داخلی سرویس:

```bash
POD=$(oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}')
oc exec "$POD" -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/healthz').read().decode())"
```

## ۶. تمدید ۳۰ روزه (بکاپ اتمی)

خودِ اکانت Sandbox بعد از ۳۰ روز منقضی می‌شود. قبل از انقضا:

```bash
./deploy/backup.sh                 # یا در ویندوز: .\deploy\backup.ps1
```

بعد از گرفتن sandbox جدید:

```bash
oc login --token=... --server=...
REPO=https://github.com/USERNAME/iran-airspace-watch.git ./deploy/deploy.sh
./deploy/backup.sh restore backups/data-YYYY-MM-DD-HHMMSS.tgz
```

## ۶.۵ وضعیت NOTAM و خطوط هوایی باز

کالکتور NOTAM در `settings.yaml` با `notam.enabled: false` خاموش است (نیاز به کلید FAA داشت).  
پوشش خبری روی کشورهایی تنظیم شده که خط هوایی‌شان با ایران باز است (چین، روسیه، ترکیه، افغانستان، عراق، تایلند، ویتنام، پاکستان) به‌علاوه‌ی رسانه‌های تخصصی هوانوردی (از جمله Flightradar24 و OPSGROUP).

## ۷. عیب‌یابی سریع

| نشانه | علت | راه‌حل |
|---|---|---|
| `READY 0/0` در `oc get deploy` | خاموشی ۱۲ساعته‌ی Sandbox قبل از نصب `keepalive` | اجرای مجدد `deploy.sh` / `deploy.ps1` (یا موقت: `oc scale deploy/airwatch --replicas=1`) |
| `ImagePullBackOff` | بیلد تمام نشده | `oc start-build airwatch --follow` |
| `CrashLoopBackOff` | خطای پیکربندی | `oc logs deployment/airwatch --previous` |
| `409 Conflict` در لاگ تلگرام | دو نمونه از بات همزمان روشن است (مثلاً یکی روی لپ‌تاپ و یکی روی سرور) | نمونه‌ی محلی را متوقف کن |
| `401 / 403` در لاگ تلگرام | توکن غلط یا باطل | توکن را در BotFather چک کن |
| `stale` در `/healthz` | قفل‌شدن کالکتور یا نخ تلگرام | خودکار توسط `livenessProbe` ری‌استارت می‌شود |
