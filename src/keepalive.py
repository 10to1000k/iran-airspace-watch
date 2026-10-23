"""بیدارنگه‌دارنده‌ی خودکار برای OpenShift Developer Sandbox.

چرا این ماژول وجود دارد:
کلاستر رایگان OpenShift Developer Sandbox یک کنترلر سیستمی به نام `Idler` دارد
که هر Pod با عمر بیش از ۱۲ ساعت (یا بیش از ۵۰ ری‌استارت) را خاموش می‌کند و مقدار
`spec.replicas` را در `Deployment` روی `0` می‌گذارد — ولی روی `CronJob` اعمال نمی‌شود.

این اسکریپت از داخل یک `CronJob` سبک (هر ۵ دقیقه، با همان ایمیج اصلی پروژه) اجرا می‌شود و:
۱) اگر `Deployment` به `replicas=0` رفته باشد، فوراً آن را به `replicas=1` برمی‌گرداند.
۲) اگر عمر Pod فعلی از ۱۱ ساعت گذشته باشد (قبل از رسیدن به سقف ۱۲ ساعتِ Sandbox) یا
   شمارش ری‌استارتش بالا رفته باشد، یک ری‌استارت نرم (Rollout Restart) می‌زند تا Pod
   تازه با تایمر صفر جایگزین شود و ربات حتی یک دقیقه هم توسط Idler خاموش نشود.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import requests

from . import util

log = logging.getLogger(__name__)

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEFAULT_MAX_POD_AGE_SECONDS = 11 * 3600  # ۱۱ ساعت؛ یک ساعت زودتر از سقف ۱۲ ساعت Sandbox
DEFAULT_MAX_RESTARTS = 40                # پایین‌تر از سقف ۵۰ ری‌استارت در Idler


class KeepAliveClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        namespace: str,
        verify: str | bool = True,
        session: requests.Session | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.namespace = namespace
        self.verify = verify
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}"})

    @classmethod
    def from_cluster(cls) -> KeepAliveClient:
        token_path = Path(os.getenv("K8S_TOKEN_FILE", str(SA_DIR / "token")))
        ns_path = Path(os.getenv("K8S_NAMESPACE_FILE", str(SA_DIR / "namespace")))
        ca_path = Path(os.getenv("K8S_CA_FILE", str(SA_DIR / "ca.crt")))
        api_url = os.getenv("K8S_API_URL", "https://kubernetes.default.svc")

        token = token_path.read_text(encoding="utf-8").strip()
        namespace = os.getenv("POD_NAMESPACE") or ns_path.read_text(encoding="utf-8").strip()
        verify: str | bool = str(ca_path) if ca_path.exists() else True
        return cls(api_url=api_url, token=token, namespace=namespace, verify=verify)

    def _deploy_url(self, deployment: str) -> str:
        return f"{self.api_url}/apis/apps/v1/namespaces/{self.namespace}/deployments/{deployment}"

    def _pods_url(self) -> str:
        return f"{self.api_url}/api/v1/namespaces/{self.namespace}/pods"

    def ensure_awake(
        self,
        deployment: str = "airwatch",
        app_label: str = "airwatch",
        max_pod_age_seconds: int = DEFAULT_MAX_POD_AGE_SECONDS,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        now: datetime | None = None,
    ) -> str:
        """وضعیت Deployment را بررسی می‌کند و اقدام انجام‌شده را برمی‌گرداند:
        `scaled-up` | `restarted` | `healthy`
        """
        current_now = now or util.now_utc()
        resp = self.http.get(self._deploy_url(deployment), verify=self.verify, timeout=15)
        resp.raise_for_status()
        deploy_obj = resp.json()

        replicas = int(deploy_obj.get("spec", {}).get("replicas", 0) or 0)
        if replicas < 1:
            patch = {"spec": {"replicas": 1}}
            p = self.http.patch(
                self._deploy_url(deployment),
                json=patch,
                headers={"Content-Type": "application/strategic-merge-patch+json"},
                verify=self.verify,
                timeout=15,
            )
            p.raise_for_status()
            log.warning(
                "Deployment %s خاموش شده بود (replicas=%d) → به replicas=1 بیدار شد",
                deployment,
                replicas,
            )
            return "scaled-up"

        pods_resp = self.http.get(
            self._pods_url(),
            params={"labelSelector": f"app={app_label}"},
            verify=self.verify,
            timeout=15,
        )
        pods_resp.raise_for_status()
        pods = pods_resp.json().get("items") or []

        needs_recycle = False
        reason = ""
        for pod in pods:
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            status = pod.get("status") or {}
            if status.get("phase") != "Running":
                continue
            restarts = max(
                (int(cs.get("restartCount", 0)) for cs in (status.get("containerStatuses") or [])),
                default=0,
            )
            if restarts >= max_restarts:
                needs_recycle = True
                reason = f"restartCount={restarts}"
                break
            started = util.parse_iso(status.get("startTime"))
            if started:
                age = (current_now - started).total_seconds()
                if age >= max_pod_age_seconds:
                    needs_recycle = True
                    reason = f"age={round(age / 3600, 1)}h"
                    break

        if needs_recycle:
            stamp = util.iso(current_now)
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"airwatch.io/restartedAt": stamp}
                        }
                    }
                }
            }
            p = self.http.patch(
                self._deploy_url(deployment),
                json=patch,
                headers={"Content-Type": "application/strategic-merge-patch+json"},
                verify=self.verify,
                timeout=15,
            )
            p.raise_for_status()
            log.info(
                "چرخش پیشگیرانه‌ی Pod برای %s انجام شد (%s) تا مشمول Idler نشود",
                deployment,
                reason,
            )
            return "restarted"

        log.info("Deployment %s فعال و سالم است (replicas=%d)", deployment, replicas)
        return "healthy"


def run_keepalive() -> int:
    deployment = os.getenv("KEEPALIVE_DEPLOYMENT", "airwatch")
    app_label = os.getenv("KEEPALIVE_APP_LABEL", "airwatch")
    max_age = int(os.getenv("KEEPALIVE_MAX_POD_AGE_SECONDS", str(DEFAULT_MAX_POD_AGE_SECONDS)))
    client = KeepAliveClient.from_cluster()
    client.ensure_awake(
        deployment=deployment,
        app_label=app_label,
        max_pod_age_seconds=max_age,
    )
    return 0
