<#
.SYNOPSIS
    بکاپ و بازگردانی دیتابیس روی OpenShift — نسخه‌ی ویندوز

.DESCRIPTION
    آرشیو داخل pod ساخته و همان‌جا تست می‌شود، بعد با oc cp کپی می‌شود.
    استریم مستقیم tar به stdout قابل‌اعتماد نیست (فایل ناقص تولید می‌کند).

.EXAMPLE
    .\deploy\backup.ps1

.EXAMPLE
    .\deploy\backup.ps1 -Restore backups\data-2026-10-21-120000.tgz
#>
[CmdletBinding()]
param(
    [string]$Restore = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# کنسول ویندوز پیش‌فرض cp1252 است؛ بدون این خط متن فارسی نامفهوم چاپ می‌شود.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Fail([string]$Message) {
    Write-Host "خطا: $Message" -ForegroundColor Red
    exit 1
}

# شبکه‌ی sandbox گاهی TLS handshake را تایم‌اوت می‌کند؛ خروجی خالی یعنی تلاش مجدد.
function Get-OcText([string]$Command) {
    for ($i = 1; $i -le 3; $i++) {
        $line = cmd /c "$Command 2>nul"
        if ($line) { return ("$line").Trim() }
        Start-Sleep -Seconds 2
    }
    return ""
}

# Remove-Item روی مسیرهای TEMP ویندوز (نام کوتاه 8.3) خطا می‌دهد؛ این امن است.
function Remove-Quiet([string]$Path) {
    try {
        if ($Path -and [System.IO.File]::Exists($Path)) { [System.IO.File]::Delete($Path) }
    }
    catch { }
}

cmd /c "oc version --client >nul 2>&1"
if ($LASTEXITCODE -ne 0) { Fail "oc در PATH نیست. ترمینال را دوباره باز کن." }

$namespace = Get-OcText "oc project -q"
if (-not $namespace) { Fail "اتصال به کلاستر برقرار نشد (oc project -q خالی برگشت). دوباره امتحان کن." }
$image = "image-registry.openshift-image-registry.svc:5000/$namespace/iran-airspace-watch:latest"
$pod = Get-OcText "oc get pod -l app=airwatch -o jsonpath={.items[0].metadata.name}"
if (-not $pod) { Fail "pod پیدا نشد. اول استقرار را انجام بده." }

if ($Restore) {
    if (-not (Test-Path $Restore)) { Fail "فایل پیدا نشد: $Restore" }

    $fs = [System.IO.File]::OpenRead($Restore)
    $magic = New-Object byte[] 2
    $fs.Read($magic, 0, 2) | Out-Null
    $fs.Close()
    if (-not ($magic[0] -eq 0x1F -and $magic[1] -eq 0x8B)) {
        Fail "فایل بکاپ معتبر نیست (gzip نیست). احتمالاً کپی ناقص بوده."
    }

    Write-Host "→ توقف بات (تا کسی وسط بازگردانی روی دیتابیس ننویسد)" -ForegroundColor Cyan
    cmd /c "oc scale deployment/airwatch --replicas=0 >nul"
    cmd /c "oc wait --for=delete pod -l app=airwatch --timeout=180s >nul 2>&1"

    $tmpManifest = Join-Path $env:TEMP "airwatch-restore.yaml"
    $restored = $false
    try {
        Write-Host "→ pod موقت برای بازگردانی" -ForegroundColor Cyan
        $manifest = @"
apiVersion: v1
kind: Pod
metadata:
  name: airwatch-restore
  labels:
    app: airwatch-restore
spec:
  restartPolicy: Never
  containers:
    - name: restore
      image: $image
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /app/data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: airwatch-data
"@
        [System.IO.File]::WriteAllText($tmpManifest, $manifest, (New-Object System.Text.UTF8Encoding($false)))

        cmd /c "oc apply -f `"$tmpManifest`" >nul"
        if ($LASTEXITCODE -ne 0) { Fail "ساخت pod بازگردانی شکست خورد." }
        cmd /c "oc wait --for=condition=Ready pod/airwatch-restore --timeout=180s >nul"
        if ($LASTEXITCODE -ne 0) { Fail "pod بازگردانی آماده نشد." }

        # oc cp مسیر مطلق ویندوز (C:\...) را با spec پاد اشتباه می‌گیرد؛ مسیر نسبی لازم است.
        $relRestore = Resolve-Path -Relative $Restore
        cmd /c "oc cp `"$relRestore`" airwatch-restore:/tmp/restore.tgz"
        if ($LASTEXITCODE -ne 0) { Fail "کپی فایل بکاپ شکست خورد." }
        cmd /c "oc exec airwatch-restore -- tar xzf /tmp/restore.tgz -C /app/data --no-overwrite-dir"
        if ($LASTEXITCODE -ne 0) { Fail "بازگردانی شکست خورد." }
        $restored = $true
    }
    finally {
        # در هر حالتی بات باید برگردد، وگرنه بعد از یک خطا خاموش می‌ماند.
        cmd /c "oc delete pod airwatch-restore --ignore-not-found --wait=true >nul 2>&1"
        Remove-Quiet $tmpManifest
        Write-Host "→ روشن‌کردن بات" -ForegroundColor Cyan
        cmd /c "oc scale deployment/airwatch --replicas=1 >nul"
        cmd /c "oc rollout status deployment/airwatch --timeout=300s"
    }

    if (-not $restored) { Fail "بازگردانی کامل نشد؛ دیتابیس دست‌نخورده ماند." }
    Write-Host "✅ بازگردانی شد." -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Force -Path "backups" | Out-Null
$out = "backups\data-$(Get-Date -Format 'yyyy-MM-dd-HHmmss').tgz"

Write-Host "→ ساخت آرشیو داخل $pod" -ForegroundColor Cyan
# فایل‌ها صریح نام برده می‌شوند تا خودِ پوشه‌ی . در آرشیو نباشد؛
# وگرنه بازگردانی موقع chmod روی ریشه‌ی volume با UID تصادفی شکست می‌خورد.
cmd /c "oc exec $pod -- tar czf /tmp/airwatch-backup.tgz --ignore-failed-read -C /app/data watch.db watch.db-wal watch.db-shm telegram"
if ($LASTEXITCODE -ne 0) { Fail "ساخت آرشیو شکست خورد." }

cmd /c "oc exec $pod -- tar tzf /tmp/airwatch-backup.tgz >nul"
if ($LASTEXITCODE -ne 0) { Fail "آرشیو داخل pod سالم نیست." }

$remoteSize = [int64](cmd /c "oc exec $pod -- stat -c %s /tmp/airwatch-backup.tgz").Trim()

# شبکه‌ی sandbox ناپایدار است: چند تلاش، فایل موقت، و بررسی اندازه و هدر gzip.
$part = "$out.part"
$copied = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    Remove-Quiet $part
    cmd /c "oc cp ${pod}:/tmp/airwatch-backup.tgz `"$part`" 2>nul"
    if ((Test-Path $part) -and (Get-Item $part).Length -eq $remoteSize) {
        $bytes = [System.IO.File]::ReadAllBytes($part)
        if ($bytes.Length -gt 2 -and $bytes[0] -eq 0x1F -and $bytes[1] -eq 0x8B) {
            Move-Item -Force $part $out
            $copied = $true
            break
        }
    }
    Write-Host "   تلاش $attempt ناموفق بود، دوباره..." -ForegroundColor DarkYellow
    Start-Sleep -Seconds 4
}

cmd /c "oc exec $pod -- rm -f /tmp/airwatch-backup.tgz >nul"

if (-not $copied) {
    Remove-Quiet $part
    Fail "کپی آرشیو شکست خورد (انتظار $remoteSize بایت). شبکه را چک کن و دوباره بزن."
}

$size = "{0:N0} KB" -f ((Get-Item $out).Length / 1KB)
Write-Host "✅ ذخیره شد: $out ($size)" -ForegroundColor Green
