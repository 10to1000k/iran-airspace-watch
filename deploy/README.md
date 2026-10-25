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

برای ترجمه‌ی خبرهای غیر فارسی و گفت‌وگوی پرسش‌وپاسخ درباره‌ی هر خبر، **`LLM_API_KEY` الزامی است**؛ اگر ترجمه انجام نشود، خبر غیرفارسی به‌صورت متن خام منتشر نمی‌شود و برای تلاش بعدی باقی می‌ماند. علاوه‌بر توکن بات، توصیه می‌شود **`TELEGRAM_CHAT_ID` خودتان را هم در `.env` بنویسید** تا حتی بدون `/start` هم گیرنده همیشه مشخص باشد:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LLM_API_KEY=...
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-2.0-flash
```

هر خبر دکمه‌ی «گفت‌وگو درباره‌ی خبر» دارد که کاربر را به DM بات و گفت‌وگوی موقت با LLM می‌برد. نام کاربری بات با Bot API شناسایی می‌شود؛ در صورت نیاز می‌توان `TELEGRAM_BOT_USERNAME` را هم در `.env` تعیین کرد. `/stopchat` جلسه را می‌بندد.

### رصد شبکه‌های اجتماعی (اختیاری)

- پیش‌نمایش کانال‌های عمومی تلگرام با `ENABLE_TELEGRAM_PUBLIC_PREVIEW=true` از `t.me/s` خوانده می‌شود و نیاز به ادمین‌کردن بات ندارد؛ فهرست تأییدشده در `config/sources.yaml` است و هر نام اضافه را می‌توان در `TELEGRAM_CHANNELS=channel_one,channel_two` نوشت. این روش HTML عمومی است، نه API رسمی پایش؛ فقط کانال عمومی را می‌خواند و ممکن است با تغییر صفحه یا rate limit موقتاً متوقف شود.
- اگر بات را در کانالی ادمین کرده‌ای، می‌توانی مسیر مستقیم Bot API را هم با `ENABLE_TELEGRAM_CHANNELS=true` فعال کنی؛ فقط پست‌های جدید را می‌گیرد.
- X: `ENABLE_X=true` و `X_BEARER_TOKEN=...` را با توکن رسمی X API v2 تنظیم کن؛ دسترسی `recent search` ممکن است به پلن پولی نیاز داشته باشد.
- هیچ توکن/کلیدی را در چت یا Git نگذار. بعد از تغییر `.env` در OpenShift اجرا کن: `.\deploy\deploy.ps1 -SkipBuild` تا Secret به‌روز و پاد ری‌استارت شود.
- هشتگ‌های ثابت و queryهای جست‌وجو در `config/settings.yaml` هستند؛ هشتگ‌های تازه فقط پس از تأیید فیلتر ایران‌محور و تکرار در منابع مستقل دنبال می‌شوند. Bot API تلگرام جست‌وجوی سراسری کانال‌ها را انجام نمی‌دهد.

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
5. برای `BuildConfig/airwatch` یک Generic webhook امن تنظیم می‌کند و تلاش می‌کند secret لازم GitHub Actions را ثبت کند

### استقرار خودکار بعد از موفقیت تست‌ها

یک‌بار راه‌اندازی اولیه نیاز به ورود معتبر OpenShift دارد. اگر `oc` خطای credential می‌دهد، از Web Console سند جدید `Copy login command` را بگیر و دوباره `oc login` کن؛ GitHub Actions به اطلاعات ورود یا توکن `oc` نیاز نخواهد داشت.

بعد از تنظیم `.env` و لاگین، اسکریپت deploy را اجرا کن. اسکریپت یک کلید تصادفی ۲۵۶بیتی می‌سازد و آن را فقط در فایل محلیِ نادیده‌گرفته‌شده‌ی `.openshift-build-webhook-secret` و BuildConfig ثبت می‌کند. سپس URL webhook را به‌عنوان secret مخزن GitHub با نام زیر تنظیم می‌کند:

```text
OPENSHIFT_BUILD_WEBHOOK_URL
```

اگر GitHub CLI (`gh`) نصب و احراز هویت شده باشد، اسکریپت ثبت secret را خودکار امتحان می‌کند. در غیر این صورت (یا اگر دسترسی `gh` کافی نباشد)، URL محرمانه را در پایان نشان می‌دهد؛ آن را در Git یا چت نگذار و در GitHub از مسیر **Settings → Secrets and variables → Actions → New repository secret** دستی ذخیره کن. در PowerShell برای نمایش URL جهت تنظیم دستی می‌توانی `-SkipGitHubSecret` بدهی؛ در Bash نیز نبودن/واردنبودن `gh` به همین روش دستی می‌انجامد.

پس از این راه‌اندازی یک‌باره:

- فقط **push به `main`** که همه‌ی jobهای تست workflow را با موفقیت بگذراند webhook را صدا می‌زند. push به شاخه‌های دیگر، Pull Request و اجرای دستی workflow استقرار نمی‌کنند.
- BuildConfig کد `main` را با build داخلی OpenShift می‌سازد؛ تغییر ImageStream هم Deployment بات و هم template مربوط به `CronJob` را به‌روز می‌کند. اجرای دستی `oc start-build` و `oc rollout restart` برای انتشار نسخه‌های جدید لازم نیست.
- BuildConfig، ImageStream، فضای ذخیره‌سازی و `CronJob/airwatch-keepalive` روی همان OpenShift Developer Sandbox باقی می‌مانند؛ میزبانی به سرویس دیگری منتقل نمی‌شود.
- GitHub فقط webhook URL را به‌صورت Actions secret نگه می‌دارد و هیچ توکن/credential مربوط به OpenShift در GitHub ذخیره نمی‌شود. موفقیت job یعنی OpenShift درخواست build را پذیرفته است؛ نتیجه‌ی خود build را می‌توان در بخش Builds کنسول یا با `oc logs -f bc/airwatch` دید.

اگر sandbox عوض شد یا secret webhook افشا شد، فایل محلی `.openshift-build-webhook-secret` را حذف کن و deploy script را با احراز هویت OpenShift دوباره اجرا کن؛ سپس Actions secret جدید را جایگزین کن.

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
