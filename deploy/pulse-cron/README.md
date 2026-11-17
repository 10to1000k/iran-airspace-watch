# ضربان خارجی پالس (جلوگیری از از کار افتادن بات)

## چرا اینجا وجود دارد

زمانبندی داخلی GitHub Actions (`cron: "*/5 * * * *"` در `pulse.yml`) قابلاعتماد
نیست: در این مخزن شکاف ۹روزه (۷←۱۶ نوامبر) و فاصلههای ۹ساعته بین اجراها
دیده شد. نتیجه آن خاموشی کامل بات بود: `/start` کاربر در صف تلگرام میماند،
گیرندهای ثبت نمیشد و پیامها «به چرخهی بعد موکول» میشدند.

راهحل: یک **Cloudflare Worker رایگان** هر ۱۵ دقیقه یک رویداد
`repository_dispatch` با نام `airwatch-tick` میزند. `pulse.yml` این رویداد را
همانند اجرای عادی پردازش میکند. بازه ۱۵ دقیقه عمداً با طول معمول ۵–۱۲ دقیقهای
پالس هماهنگ است تا اجراها در `concurrency group: pulse` انباشته نشوند.

## چیزهایی که از قبل انجام شده

- `repository_dispatch` با نوع `airwatch-tick` به `pulse.yml` اضافه شد.
- متغیر `TELEGRAM_CHAT_ID = 1186133617` در تنظیمات مخزن ثبت شد و پالس آن را
  بهعنوان گیرنده ثابت تزریق میکند؛ بنابراین حتی اگر برنچ `state` ریست شود یا
  `/start` مصرف نشود، ارسال متوقف نمیشود.

## نصب (یک بار، حدود ۵ دقیقه)

1. در Cloudflare وارد شوید و در ترمینال:

   ```powershell
   cd "$env:USERPROFILE\Desktop\iran-airspace-watch\deploy\pulse-cron"
   npx wrangler login
   ```

2. یک **Personal Access Token** در GitHub بسازید:

   - سادهترین: Classic PAT فقط با اسکوپ `repo` (تاریخ انقضاء ۹۰ روزه، یادآوری
     تقویمی بگذارید).
   - یا Fine-grained PAT محدود به مخزن `iran-airspace-watch` با دسترسی
     **Contents: write** (در صورت نیاز سازمان، **Actions: read and write** هم بدهید).

3. توکن را بهعنوان SecretWorker بدهید (مقدار چاپ نمیشود):

   ```powershell
   npx wrangler secret put GH_PAT
   ```

4. Worker را مستقر کنید:

   ```powershell
   npx wrangler deploy
   ```

5. تأیید: ۱۵ دقیقه صبر کنید سپس:

   ```powershell
   gh run list --workflow=pulse.yml --limit 5
   ```

   باید اجراهایی با رویداد `repository_dispatch` ببینید، حتی اگر زمانبندی
   داخلی GitHub باز هم تأخیر داشت.

هزینه: Plan رایگان Workers شامل Cron Trigger و ۱۰۰٬۰۰۰ درخواست در روز میشود؛
این Worker حدود ۹۶ درخواست در روز میزند.

## تست محلی

```powershell
node --test test/index.test.js
```

## هشدار قطع شدن (اختیاری ولی مهم)

در `pulse.yml` قابلیت ping به healthchecks.io وجود دارد ولی از طریق سکرت
`HEALTHCHECKS_UUID` تنظیم نشده است. اگر میخواهید موقع متوقف شدن پالس هشدار
بگیرید:

1. در [healthchecks.io](https://healthchecks.io) یک چک رایگان بسازید (بازه
   ۱۵ دقیقه، Grace ۱۰ دقیقه).
2. `gh secret set HEALTHCHECKS_UUID --body "<uuid>"`

سپس اگر پالس بیش از ۲۵ دقیقه اجرا نشود، چک به حالت fail میرود.
