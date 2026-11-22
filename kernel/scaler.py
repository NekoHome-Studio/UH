"""Bounded, audited access to the CrewAI Deployment's scale subresource."""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from . import tracing
from .logging import get_logger, log_event

_TARGET = "uh-crewai"
_DEPLOYMENT = f"/apis/apps/v1/namespaces/uh-prod/deployments/{_TARGET}"
_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_LOG = get_logger(__name__)


@dataclass(frozen=True)
class ScaleResult:
    status: Literal["ok", "rejected", "degraded"]
    reason: str = ""


class CrewAIScaler:
    def __init__(self, *, timeout_s: float = 30.0, poll_interval_s: float = 0.5) -> None:
        if not (0 < timeout_s <= 30 and 0 < poll_interval_s):
            raise ValueError("scaler requires a positive timeout up to 30 seconds and polling interval")
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    def _client(self) -> httpx.AsyncClient:
        # A cluster may supply a TLS endpoint on each node to preserve the Pod network policy.
        host = os.getenv("UH_K8S_API_HOST") or os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.getenv("UH_K8S_API_PORT") or os.getenv("KUBERNETES_SERVICE_PORT", "443")
        api_url = httpx.URL(scheme="https", host=host, port=int(port))
        bearer = (_SERVICE_ACCOUNT / "token").read_text(encoding="utf-8").strip()
        if not host or not bearer:
            raise ValueError("missing Kubernetes API host or service account credential")
        return httpx.AsyncClient(
            base_url=api_url,
            headers={"Authorization": f"Bearer {bearer}"},
            verify=ssl.create_default_context(cafile=str(_SERVICE_ACCOUNT / "ca.crt")),
            timeout=self.timeout_s,
            trust_env=False,
        )

    async def scale(self, target: str, replicas: int, *, trace_id: str | None = None) -> ScaleResult:
        """Set the desired count once and wait for the Deployment to observe it."""
        result = ScaleResult("degraded", "cancelled")
        with tracing.request_context(trace_id or tracing.get_trace_id()):
            try:
                if target != _TARGET:
                    result = ScaleResult("rejected", "target")
                elif type(replicas) is not int or replicas not in (0, 1):
                    result = ScaleResult("rejected", "replicas")
                else:
                    result = await self._apply(replicas)
                return result
            finally:
                level = logging.INFO if result.status == "ok" else logging.WARNING
                log_event(
                    _LOG, level, "audit.scaler.result", target=target, replicas=replicas,
                    result=result.status, reason=result.reason,
                )

    async def _apply(self, replicas: int) -> ScaleResult:
        try:
            client = self._client()
        except (KeyError, OSError, ValueError, httpx.InvalidURL):
            return ScaleResult("degraded", "cluster_unavailable")

        try:
            async with client, asyncio.timeout(self.timeout_s):
                response = await client.patch(
                    f"{_DEPLOYMENT}/scale",
                    json={"spec": {"replicas": replicas}},
                    headers={"Content-Type": "application/merge-patch+json"},
                )
                response.raise_for_status()
                while True:
                    response = await client.get(_DEPLOYMENT)
                    response.raise_for_status()
                    deployment = response.json()
                    status = deployment.get("status", {})
                    if (
                        deployment["spec"]["replicas"] == replicas
                        and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
                        and status.get("replicas", 0) == replicas
                        and status.get("readyReplicas", 0) == replicas
                    ):
                        return ScaleResult("ok")
                    await asyncio.sleep(self.poll_interval_s)
        except (TimeoutError, httpx.TimeoutException):
            # The API server may have accepted the patch. Do not undo a concurrent caller's work.
            return ScaleResult("degraded", "timeout")
        except httpx.HTTPError:
            return ScaleResult("degraded", "api_error")
        except (AttributeError, KeyError, TypeError, ValueError):
            return ScaleResult("degraded", "invalid_api_response")
