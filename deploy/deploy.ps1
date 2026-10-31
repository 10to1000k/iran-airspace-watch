<#
.SYNOPSIS
    استقرار روی OpenShift Developer Sandbox — نسخه‌ی ویندوز / PowerShell

.DESCRIPTION
    همان کاری را می‌کند که deploy.sh می‌کند، بدون نیاز به Git Bash یا WSL:
      ۱) سکرت را از .env می‌سازد
      ۲) ImageStream + BuildConfig + PVC را اعمال می‌کند
      ۳) ایمیج را داخل کلاستر بیلد می‌کند
      ۴) Deployment را مستقر می‌کند و منتظر آماده‌شدن می‌ماند

.PARAMETER Repo
    آدرس گیت پابلیک. پیش‌فرض: ریموت origin

.PARAMETER SkipBuild
    بیلد را رد کن (وقتی فقط تنظیمات عوض شده)

.EXAMPLE
    .\deploy\deploy.ps1

.EXAMPLE
    .\deploy\deploy.ps1 -Repo https://github.com/10to1000k/iran-airspace-watch.git
#>
[CmdletBinding()]
param(
    [string]$Repo = "",
    [string]$Image = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Fail([string]$Message) {
    Write-Host "خطا: $Message" -ForegroundColor Red
    exit 1
}
function Step([string]$Message) {
    Write-Host ""
    Write-Host "→ $Message" -ForegroundColor Cyan
}

# ── پیش‌نیازها ────────────────────────────────────────────────
& oc version --client *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "oc نصب نیست یا در PATH نیست. ترمینال را ببند و دوباره باز کن، یا C:\Users\<you>\bin را به PATH اضافه کن."
}

& oc whoami *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "با oc لاگین نیستی. اول این را بزن:  oc login --token=... --server=https://api.sandbox-....openshiftapps.com:6443"
}

if (-not (Test-Path ".env")) {
    Fail "فایل .env نیست. این را بزن:  Copy-Item .env.example .env  و بعد توکن‌ها را داخلش بگذار."
}

if (-not $Repo) {
    $repoOutput = git remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0 -and $repoOutput) {
        $Repo = (($repoOutput | Select-Object -First 1).ToString()).Trim()
    } else {
        $Repo = ""
        Fail "ریپو مشخص نشده و ریموت origin هم نیست. با -Repo آدرس بده."
    }
}

$nsOutput = & oc project -q
if ($LASTEXITCODE -ne 0 -or -not $nsOutput) { Fail "پروژه‌ی فعال پیدا نشد." }
$ns = (($nsOutput | Select-Object -First 1).ToString()).Trim()

if (-not $Image) {
    $Image = "image-registry.openshift-image-registry.svc:5000/$ns/iran-airspace-watch:latest"
}

Write-Host "پروژه : $ns"
Write-Host "ریپو  : $Repo"
Write-Host "ایمیج : $Image"

function Render([string]$Source, [string]$Destination) {
    $path = Join-Path $root $Source
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $text = $text.Replace("__REPO__", $Repo).Replace("__IMAGE__", $Image)
    [System.IO.File]::WriteAllText((Join-Path $root $Destination), $text, $utf8)
}

# ── سکرت ──────────────────────────────────────────────────────
Step "ساخت/به‌روزرسانی سکرت airwatch-secrets"
# از پوشه‌ی Temp کاربر استفاده نکن؛ در برخی ویندوزها TEMP به مسیر 8.3
# (مثل C:\Users\SHOPA~1) اشاره می‌کند که ممکن است در PowerShell قابل resolve نباشد.
$secretFile = Join-Path $root ".deploy-secret.rendered.yaml"
$secretYaml = & oc create secret generic airwatch-secrets --from-env-file=.env --dry-run=client -o yaml
if ($LASTEXITCODE -ne 0 -or -not $secretYaml) { Fail "خواندن .env و ساخت سکرت شکست خورد." }
[System.IO.File]::WriteAllText(
    $secretFile,
    (($secretYaml -join [Environment]::NewLine) + [Environment]::NewLine),
    $utf8
)
& oc apply -f $secretFile *> $null
if ($LASTEXITCODE -ne 0) { Fail "اعمال سکرت شکست خورد." }
if (Test-Path -LiteralPath $secretFile) {
    try {
        Remove-Item -LiteralPath $secretFile -Force -ErrorAction Stop
    } catch {
        Write-Warning "فایل موقت سکرت پاک نشد: $secretFile"
    }
}
Write-Host "   سکرت به‌روز شد" -ForegroundColor DarkGray

# ── منابع بیلد ────────────────────────────────────────────────
Step "اعمال ImageStream / BuildConfig / PVC"
Render "deploy/build.yaml" ".deploy-build.rendered.yaml"
& oc apply -f .deploy-build.rendered.yaml
if ($LASTEXITCODE -ne 0) { Fail "اعمال منابع بیلد شکست خورد." }

# ── RSSHub و Redis ────────────────────────────────────────────
Step "اعمال RSSHub/Redis (کش هشتگ دو دقیقه‌ای)"
& oc apply -f deploy/social-feed-hub.yaml
if ($LASTEXITCODE -ne 0) { Fail "اعمال RSSHub/Redis شکست خورد." }
& oc rollout status deployment/social-feed-redis --timeout=180s
if ($LASTEXITCODE -ne 0) { Fail "Redis آماده نشد." }
& oc rollout status deployment/social-feed-hub --timeout=300s
if ($LASTEXITCODE -ne 0) { Fail "RSSHub آماده نشد." }

# ── بیلد ──────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Step "بیلد ایمیج داخل کلاستر (چند دقیقه؛ اولین بار طولانی‌تر)"
    & oc start-build airwatch --follow
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "بیلد شکست خورد. لاگ کامل:" -ForegroundColor Yellow
        Write-Host "   oc logs -f bc/airwatch" -ForegroundColor Yellow
        Fail "بیلد ناموفق بود."
    }
}

# ── استقرار ───────────────────────────────────────────────────
Step "استقرار Deployment"
Render "deploy/app.yaml" ".deploy-app.rendered.yaml"
& oc apply -f .deploy-app.rendered.yaml
if ($LASTEXITCODE -ne 0) { Fail "اعمال Deployment شکست خورد." }

# تگ ایمیج ثابت است (:latest)، پس بدون restart پاد قدیمی می‌ماند.
& oc rollout restart deployment/airwatch | Out-Null

Step "انتظار برای آماده‌شدن pod"
& oc rollout status deployment/airwatch --timeout=300s
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "pod آماده نشد. وضعیت و لاگ:" -ForegroundColor Yellow
    Write-Host "   oc get pods" -ForegroundColor Yellow
    Write-Host "   oc logs deployment/airwatch --tail=50" -ForegroundColor Yellow
    Fail "استقرار کامل نشد."
}

Remove-Item ".deploy-build.rendered.yaml", ".deploy-app.rendered.yaml" -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "✅ مستقر شد." -ForegroundColor Green
Write-Host "   لاگ زنده : oc logs -f deployment/airwatch"
Write-Host "   وضعیت    : oc get pods"
Write-Host ""
Write-Host "یادت نباشد: یک بار در تلگرام به بات /start بده." -ForegroundColor Yellow
