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

dotenv_value() {
  local key="$1"
  local line=""
  line="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  local value="${line#*=}"
  # trim leading/trailing whitespace and optional matching quotes
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [ "${#value}" -ge 2 ]; then
    local first="${value:0:1}"
    local last="${value: -1}"
    if { [ "$first" = '"' ] && [ "$last" = '"' ]; } || { [ "$first" = "'" ] && [ "$last" = "'" ]; }; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

# ── سکرت ──────────────────────────────────────────────────────
echo "→ ساخت/به‌روزرسانی سکرت"
oc create secret generic airwatch-secrets \
  --from-env-file=.env \
  --dry-run=client -o yaml | oc apply -f -

# ── سکرت سوشال ────────────────────────────────────────────────
echo "→ به‌روزرسانی سکرت social-feed-secrets از .env (در صورت وجود)"
social_dir=".deploy-social-secrets"
trap 'rm -rf "$social_dir"' EXIT
rm -rf "$social_dir"
mkdir -p "$social_dir"
for key in TWITTER_AUTH_TOKEN INSTAGRAM_COOKIE; do
  value="$(dotenv_value "$key")"
  if [ -n "$value" ]; then
    # بدون newline انتهایی؛ oc محتوای فایل را عیناً مقدار سکرت می‌گیرد.
    printf '%s' "$value" > "$social_dir/$key"
  fi
done
social_secrets_updated=0
if [ -n "$(ls -A "$social_dir")" ]; then
  oc get secret social-feed-secrets >/dev/null 2>&1 || oc create secret generic social-feed-secrets
  oc set data secret/social-feed-secrets --from-file="$social_dir"
  social_secrets_updated=1
  echo "   اعتبارنامه‌های سوشال به‌روز شد"
else
  echo "   TWITTER_AUTH_TOKEN/INSTAGRAM_COOKIE در .env نیست؛ سکرت سوشال دست‌نخورده ماند"
fi
rm -rf "$social_dir"

# ── منابع بیلد ────────────────────────────────────────────────
echo "→ اعمال ImageStream / BuildConfig / PVC"
render deploy/build.yaml | oc apply -f -

# ── RSSHub و Redis ────────────────────────────────────────────
echo "→ اعمال RSSHub/Redis (کش هشتگ دو دقیقه‌ای)"
oc apply -f deploy/social-feed-hub.yaml
if [ "$social_secrets_updated" -eq 1 ]; then
  # envFrom فقط هنگام شروع pod خوانده می‌شود؛ بدون restart کوکی تازه اعمال نمی‌شود.
  oc rollout restart deployment/social-feed-hub
fi
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
