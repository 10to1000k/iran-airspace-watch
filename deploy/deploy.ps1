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

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$key] = $value
    }
    return $values
}

function Sync-SocialSecrets {
    $dotenv = Read-DotEnv (Join-Path $root ".env")
    $keys = @("TWITTER_AUTH_TOKEN", "INSTAGRAM_COOKIE")
    $dir = Join-Path $root ".deploy-social-secrets"
    if (Test-Path -LiteralPath $dir) {
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $dir | Out-Null

    $present = @()
    foreach ($key in $keys) {
        $value = $dotenv[$key]
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            # بدون newline انتهایی؛ oc محتوای فایل را عیناً مقدار سکرت می‌گیرد.
            [System.IO.File]::WriteAllText((Join-Path $dir $key), $value, $utf8)
            $present += $key
        }
    }

    if ($present.Count -eq 0) {
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        return $false
    }

    # ساخت YAML و apply به‌جای get/create/set: در namespace تازه، stderr دستور
    # oc get با ErrorActionPreference=Stop کل اسکریپت را متوقف می‌کرد.
    $secretYaml = & oc create secret generic social-feed-secrets "--from-file=$dir" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0 -or -not $secretYaml) {
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        Fail "ساخت YAML سکرت social-feed-secrets شکست خورد."
    }
    $secretFile = Join-Path $root ".deploy-social-secrets.rendered.yaml"
    [System.IO.File]::WriteAllText(
        $secretFile,
        (($secretYaml -join [Environment]::NewLine) + [Environment]::NewLine),
        $utf8
    )
    & oc apply -f $secretFile *> $null
    $code = $LASTEXITCODE
    Remove-Item -LiteralPath $secretFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    if ($code -ne 0) {
        Fail "اعمال سکرت social-feed-secrets شکست خورد."
    }
    return $true
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

# ── سکرت سوشال ────────────────────────────────────────────────
Step "به‌روزرسانی سکرت social-feed-secrets از .env (در صورت وجود)"
$socialSecretsUpdated = Sync-SocialSecrets
if ($socialSecretsUpdated) {
    Write-Host "   اعتبارنامه‌های سوشال به‌روز شد" -ForegroundColor DarkGray
} else {
    Write-Host "   TWITTER_AUTH_TOKEN/INSTAGRAM_COOKIE در .env نیست؛ سکرت سوشال دست‌نخورده ماند" -ForegroundColor DarkGray
}

# ── منابع بیلد ────────────────────────────────────────────────
Step "اعمال ImageStream / BuildConfig / PVC"
Render "deploy/build.yaml" ".deploy-build.rendered.yaml"
& oc apply -f .deploy-build.rendered.yaml
if ($LASTEXITCODE -ne 0) { Fail "اعمال منابع بیلد شکست خورد." }

# ── RSSHub و Redis ────────────────────────────────────────────
Step "ساخت/به‌روزرسانی ConfigMap پچ اینستاگرام"
$igPatchYaml = & oc create configmap rsshub-ig-patch `
    "--from-file=instagram-tags.mjs=$root\deploy\instagram-tags-patch.mjs" `
    "--from-file=rsshub-boot.mjs=$root\deploy\rsshub-boot.mjs" `
    --dry-run=client -o yaml
if ($LASTEXITCODE -ne 0 -or -not $igPatchYaml) { Fail "ساخت ConfigMap پچ اینستاگرام شکست خورد." }
$igPatchFile = Join-Path $root ".deploy-ig-patch.rendered.yaml"
[System.IO.File]::WriteAllText(
    $igPatchFile,
    (($igPatchYaml -join [Environment]::NewLine) + [Environment]::NewLine),
    $utf8
)
& oc apply -f $igPatchFile *> $null
$code = $LASTEXITCODE
Remove-Item -LiteralPath $igPatchFile -Force -ErrorAction SilentlyContinue
if ($code -ne 0) { Fail "اعمال ConfigMap پچ اینستاگرام شکست خورد." }

Step "اعمال RSSHub/Redis (کش هشتگ دو دقیقه‌ای)"
& oc apply -f deploy/social-feed-hub.yaml
if ($LASTEXITCODE -ne 0) { Fail "اعمال RSSHub/Redis شکست خورد." }
if ($socialSecretsUpdated) {
    # envFrom فقط هنگام شروع pod خوانده می‌شود؛ بدون restart کوکی تازه اعمال نمی‌شود.
    & oc rollout restart deployment/social-feed-hub | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "ری‌استارت RSSHub شکست خورد." }
}
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
