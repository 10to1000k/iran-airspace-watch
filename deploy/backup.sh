#!/usr/bin/env bash
# بکاپ و بازگردانی دیتابیس از pod روی OpenShift
#
#   ./deploy/backup.sh                       → بکاپ در backups/
#   ./deploy/backup.sh restore backups/x.tgz → بازگردانی

set -euo pipefail

POD="$(oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$POD" ] || { echo "خطا: pod پیدا نشد. اول استقرار را انجام بده."; exit 1; }

if [ "${1:-}" = "restore" ]; then
  FILE="${2:-}"
  [ -f "$FILE" ] || { echo "استفاده: $0 restore backups/data-YYYY-MM-DD-HHMMSS.tgz"; exit 1; }
  echo "→ بازگردانی $FILE روی $POD"
  cat "$FILE" | oc exec -i "$POD" -- tar xzf - -C /app/data
  echo "✅ بازگردانی شد. برای اطمینان:"
  echo "   oc rollout restart deployment/airwatch"
  exit 0
fi

mkdir -p backups
OUT="backups/data-$(date +%Y-%m-%d-%H%M%S).tgz"
echo "→ بکاپ از $POD"
oc exec "$POD" -- tar czf - -C /app/data . > "$OUT"
echo "✅ ذخیره شد: $OUT ($(du -h "$OUT" | cut -f1))"
