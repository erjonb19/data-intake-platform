"""A small read-only Kubernetes API client for one namespace (Stage 11, S11.6; PLAN.md
section 8.13): the pods and events of `dip-airflow`, read by the platform check and by the
incident agent's `get_k8s_events` tool through the MCP server.

In the cluster it uses the pod's service-account token and CA (a namespace-scoped Role
allows get and list on pods and events in `dip-airflow`, nothing else). DIP_K8S_API and
DIP_K8S_NAMESPACE override the API server and the namespace; a test or an eval passes its
own `http` function instead, as the read tools do for Airflow.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


@dataclass
class K8s:
    base: str = ""
    namespace: str = "dip-airflow"
    token: str = ""
    cafile: str | None = None
    http: Callable[[str, dict[str, str]], Any] | None = None

    @classmethod
    def from_env(cls) -> K8s:
        token = (SA / "token").read_text().strip() if (SA / "token").exists() else ""
        return cls(
            base=os.environ.get("DIP_K8S_API", "https://kubernetes.default.svc" if token else ""),
            namespace=os.environ.get("DIP_K8S_NAMESPACE", "dip-airflow"),
            token=token,
            cafile=str(SA / "ca.crt") if (SA / "ca.crt").exists() else None,
        )

    def get(self, path: str) -> Any:
        if self.http is None and not self.base:
            raise LookupError("no Kubernetes API here (not in a cluster, DIP_K8S_API unset)")
        url = f"{self.base}/api/v1/namespaces/{self.namespace}/{path}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.http is not None:
            return self.http(url, headers)
        ctx = ssl.create_default_context(cafile=self.cafile) if self.cafile else None
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read())

    def pods(self) -> list[dict[str, Any]]:
        return self.get("pods").get("items", [])

    def events(self) -> list[dict[str, Any]]:
        return self.get("events").get("items", [])


def terminations(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each container that ended (now or in its last state), with why: the reason
    (OOMKilled, Error, Completed), exit code and time, and the Airflow task the pod ran
    (KubernetesExecutor labels its pods with dag_id and task_id)."""
    out = []
    for p in pods:
        meta, status = p.get("metadata", {}), p.get("status", {})
        labels = meta.get("labels") or {}
        for c in status.get("containerStatuses") or []:
            for where in ("state", "lastState"):
                t = (c.get(where) or {}).get("terminated")
                if t:
                    out.append({
                        "pod": meta.get("name"), "container": c.get("name"),
                        "reason": t.get("reason"), "exit_code": t.get("exitCode"),
                        "finished_at": t.get("finishedAt"),
                        "dag_id": labels.get("dag_id"), "task_id": labels.get("task_id"),
                        "limits": next((x.get("resources", {}).get("limits") for x in
                                        p.get("spec", {}).get("containers", [])
                                        if x.get("name") == c.get("name")), None),
                    })  # fmt: skip
    return out
