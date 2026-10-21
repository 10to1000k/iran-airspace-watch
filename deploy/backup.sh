#!/usr/bin/env bash
# بکاپ و بازگردانی دیتابیس روی OpenShift
#
#   ./deploy/backup.sh                       → بکاپ در backups/
#   ./deploy/backup.sh restore backups/x.tgz → بازگردانی
#
# روش: آرشیو داخل pod ساخته می‌شود، سالم‌بودنش همان‌جا چک می‌شود،
# بعد با oc cp کپی می‌شود. استریم مستقیم tar به stdout قابل‌اعتماد نیست
# (یک بار فایل ناقص تولید کرد).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAMESPACE="$(oc project -q)"
IMAGE="image-registry.openshift-image-registry.svc:5000/${NAMESPACE}/iran-airspace-watch:latest"
POD="$(oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

if [ "${1:-}" = "restore" ]; then
  FILE="${2:-}"
  [ -f "$FILE" ] || { echo "استفاده: $0 restore backups/data-YYYY-MM-DD-HHMMSS.tgz"; exit 1; }
  [ -n "$POD" ] || { echo "خطا: pod پیدا نشد."; exit 1; }

  echo "→ توقف بات (تا کسی وسط بازگردانی روی دیتابیس ننویسد)"
  oc scale deployment/airwatch --replicas=0
  oc wait --for=delete pod -l app=airwatch --timeout=180s 2>/dev/null || true

  # در هر حالتی بات باید برگردد، وگرنه بعد از یک خطا خاموش می‌ماند.
  cleanup_restore() {
    oc delete pod airwatch-restore --ignore-not-found --wait=true >/dev/null 2>&1 || true
    echo "→ روشن‌کردن بات"
    oc scale deployment/airwatch --replicas=1 >/dev/null 2>&1 || true
    oc rollout status deployment/airwatch --timeout=300s || true
  }
  trap cleanup_restore EXIT

  echo "→ pod موقت برای بازگردانی"
  cat <<EOF | oc apply -f - >/dev/null
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
      image: ${IMAGE}
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /app/data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: airwatch-data
EOF
  oc wait --for=condition=Ready pod/airwatch-restore --timeout=180s
  oc cp "$FILE" airwatch-restore:/tmp/restore.tgz
  # --no-overwrite-dir: UID تصادفی اجازه‌ی chmod/utime روی ریشه‌ی volume را ندارد
  oc exec airwatch-restore -- tar xzf /tmp/restore.tgz -C /app/data --no-overwrite-dir
  echo "✅ بازگردانی شد."
  exit 0
fi

[ -n "$POD" ] || { echo "خطا: pod پیدا نشد. اول استقرار را انجام بده."; exit 1; }

mkdir -p backups
OUT="backups/data-$(date +%Y-%m-%d-%H%M%S).tgz"

echo "→ ساخت آرشیو داخل $POD"
# فایل‌ها صریح نام برده می‌شوند تا خودِ پوشه‌ی . در آرشیو نباشد؛
# وگرنه بازگردانی موقع chmod روی ریشه‌ی volume با UID تصادفی شکست می‌خورد.
oc exec "$POD" -- tar czf /tmp/airwatch-backup.tgz --ignore-failed-read -C /app/data watch.db watch.db-wal watch.db-shm telegram
oc exec "$POD" -- tar tzf /tmp/airwatch-backup.tgz >/dev/null

REMOTE_SIZE="$(oc exec "$POD" -- stat -c %s /tmp/airwatch-backup.tgz | tr -d '\r')"
PART="$OUT.part"
COPIED=0
for attempt in 1 2 3 4 5; do
  rm -f "$PART"
  if oc cp "$POD:/tmp/airwatch-backup.tgz" "$PART" >/dev/null 2>&1; then
    LOCAL_SIZE="$(wc -c < "$PART" | tr -d ' ')"
    MAGIC="$(head -c 2 "$PART" | od -An -tx1 | tr -d ' \n')"
    if [ "$LOCAL_SIZE" = "$REMOTE_SIZE" ] && [ "$MAGIC" = "1f8b" ]; then
      mv -f "$PART" "$OUT"
      COPIED=1
      break
    fi
  fi
  echo "   تلاش $attempt ناموفق بود، دوباره..."
  sleep 4
done
oc exec "$POD" -- rm -f /tmp/airwatch-backup.tgz

if [ "$COPIED" != "1" ]; then
  rm -f "$PART"
  echo "خطا: کپی آرشیو شکست خورد (انتظار $REMOTE_SIZE بایت). شبکه را چک کن و دوباره بزن."
  exit 1
fi
echo "✅ ذخیره شد: $OUT ($(du -h "$OUT" | cut -f1))"
