#!/usr/bin/env bash
# استقرار روی OpenShift Developer Sandbox
#
# پیش‌نیاز:
#   1) oc نصب باشد و لاگین کرده باشی:  oc login --token=... --server=...
#   2) فایل .env در ریشه‌ی پروژه باشد
#   3) ریپو پابلیک باشد (بیلد داخل کلاستر به آن دسترسی می‌گیرد)
#
# استفاده:
#   REPO=https://github.com/USERNAME/iran-airspace-watch.git ./deploy/deploy.sh
#   ./deploy/deploy.sh            # اگر ریموت origin همان ریپو باشد

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── پیش‌نیازها ────────────────────────────────────────────────
command -v oc >/dev/null || { echo "خطا: oc نصب نیست."; exit 1; }
oc whoami >/dev/null || { echo "خطا: با oc لاگین نیستی."; exit 1; }
[ -f .env ] || { echo "خطا: فایل .env نیست. از .env.example بساز."; exit 1; }

REPO="${REPO:-$(git remote get-url origin 2>/dev/null || true)}"
[ -n "$REPO" ] || { echo "خطا: REPO مشخص نشده و ریموت origin هم نیست."; exit 1; }

NS="$(oc project -q)"
IMAGE="image-registry.openshift-image-registry.svc:5000/${NS}/iran-airspace-watch:latest"
WEBHOOK_SECRET_FILE="${ROOT}/.openshift-build-webhook-secret"
if [ -s "$WEBHOOK_SECRET_FILE" ]; then
  WEBHOOK_SECRET="$(tr -d '\r\n' < "$WEBHOOK_SECRET_FILE")"
else
  if command -v openssl >/dev/null 2>&1; then
    WEBHOOK_SECRET="$(openssl rand -hex 32)"
  elif command -v python3 >/dev/null 2>&1; then
    WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  else
    echo "خطا: برای ساخت secret امن، openssl یا python3 لازم است."
    exit 1
  fi
  printf '%s' "$WEBHOOK_SECRET" > "$WEBHOOK_SECRET_FILE"
  chmod 600 "$WEBHOOK_SECRET_FILE" 2>/dev/null || true
fi
[[ "$WEBHOOK_SECRET" =~ ^[a-f0-9]{64}$ ]] || {
  echo "خطا: .openshift-build-webhook-secret نامعتبر است؛ فایل را حذف و دوباره اجرا کن."
  exit 1
}

GH_REPO="${GH_REPO:-10to1000k/iran-airspace-watch}"
if [[ "$REPO" =~ github\.com[:/]([^/]+/[^/]+)(\.git)?$ ]]; then
  GH_REPO="${BASH_REMATCH[1]}"
fi

echo "پروژه : ${NS}"
echo "ریپو  : ${REPO}"
echo "ایمیج : ${IMAGE}"
echo

render() {
  sed -e "s|__REPO__|${REPO}|g" \
      -e "s|__IMAGE__|${IMAGE}|g" \
      -e "s|__WEBHOOK_SECRET__|${WEBHOOK_SECRET}|g" "$1"
}

# ── سکرت ──────────────────────────────────────────────────────
echo "→ ساخت/به‌روزرسانی سکرت"
oc create secret generic airwatch-secrets \
  --from-env-file=.env \
  --dry-run=client -o yaml | oc apply -f -

# ── منابع بیلد ────────────────────────────────────────────────
echo "→ اعمال ImageStream / BuildConfig / PVC"
render deploy/build.yaml | oc apply -f -

# ── بیلد ──────────────────────────────────────────────────────
echo "→ بیلد ایمیج داخل کلاستر (چند دقیقه)"
oc start-build airwatch --follow

# ── استقرار ───────────────────────────────────────────────────
echo "→ استقرار"
render deploy/app.yaml | oc apply -f -
# تگ ایمیج ثابت است (:latest)، پس بدون restart پاد قدیمی می‌ماند
oc rollout restart deployment/airwatch
oc rollout status deployment/airwatch --timeout=300s

echo "→ تنظیم استقرار خودکار GitHub Actions پس از پاس‌شدن تست‌ها"
API_SERVER="$(oc whoami --show-server)"
WEBHOOK_URL="${API_SERVER%/}/apis/build.openshift.io/v1/namespaces/${NS}/buildconfigs/airwatch/webhooks/${WEBHOOK_SECRET}/generic"
SECRET_CONFIGURED=false
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if printf '%s' "$WEBHOOK_URL" | gh secret set OPENSHIFT_BUILD_WEBHOOK_URL --repo "$GH_REPO"; then
    SECRET_CONFIGURED=true
    echo "   Actions secret OPENSHIFT_BUILD_WEBHOOK_URL تنظیم شد (${GH_REPO})"
  else
    echo "هشدار: نتوانستم GitHub Actions secret را خودکار ثبت کنم."
  fi
fi
if [ "$SECRET_CONFIGURED" != true ]; then
  echo "در Settings → Secrets and variables → Actions مخزن ${GH_REPO}،"
  echo "secret با نام OPENSHIFT_BUILD_WEBHOOK_URL بساز و URL محرمانه‌ی زیر را در آن بگذار:"
  echo "$WEBHOOK_URL"
  echo "این URL را در چت یا Git منتشر نکن."
fi

echo
echo "✅ مستقر شد."
echo "   لاگ زنده:  oc logs -f deployment/airwatch"
echo "   وضعیت:     oc get pods"
echo
echo "یادت نباشد: یک بار در تلگرام به بات /start بده."
