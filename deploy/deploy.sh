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

echo "پروژه : ${NS}"
echo "ریپو  : ${REPO}"
echo "ایمیج : ${IMAGE}"
echo

render() {
  sed -e "s|__REPO__|${REPO}|g" -e "s|__IMAGE__|${IMAGE}|g" "$1"
}

# ── سکرت ──────────────────────────────────────────────────────
echo "→ ساخت/به‌روزرسانی سکرت"
oc create secret generic airwatch-secrets \
  --from-env-file=.env \
  --dry-run=client -o yaml | oc apply -f -

# ── منابع بیلد ────────────────────────────────────────────────
echo "→ اعمال ImageStream / BuildConfig / PVC"
render deploy/build.yaml | oc apply -f -

# ── RSSHub و Redis ────────────────────────────────────────────
echo "→ اعمال RSSHub/Redis (کش هشتگ دو دقیقه‌ای)"
oc apply -f deploy/social-feed-hub.yaml
oc rollout status deployment/social-feed-redis --timeout=180s
oc rollout status deployment/social-feed-hub --timeout=300s

# ── بیلد ──────────────────────────────────────────────────────
echo "→ بیلد ایمیج داخل کلاستر (چند دقیقه)"
oc start-build airwatch --follow

# ── استقرار ───────────────────────────────────────────────────
echo "→ استقرار"
render deploy/app.yaml | oc apply -f -
# تگ ایمیج ثابت است (:latest)، پس بدون restart پاد قدیمی می‌ماند
oc rollout restart deployment/airwatch
oc rollout status deployment/airwatch --timeout=300s

echo
echo "✅ مستقر شد."
echo "   لاگ زنده:  oc logs -f deployment/airwatch"
echo "   وضعیت:     oc get pods"
echo
echo "یادت نباشد: یک بار در تلگرام به بات /start بده."
