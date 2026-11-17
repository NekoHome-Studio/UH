"""高危工具确认（M1-8，§9.6 / §6.3.2）。

write-host / irreversible 工具必须人工确认，确认不能只在 UI 层成立：
CLI / REST 均可确认或拒绝。confirmation_token 一次性防重放且与任务绑定；
拒绝后任务以 UH-USR-1002 优雅结束（task.cancelled, reason=confirmation_rejected），
保留已完成步骤；确认人与选择写入审计 tool.confirm。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .logging import get_logger, log_event

log = get_logger("kernel.confirmation")

# §9.6 confirm_levels：需人工确认的风险级
CONFIRM_LEVELS = {"write-host", "irreversible"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ConfirmationError(Exception):
    """确认错误，字段对齐 errors.md。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "detail": dict(self.detail)}


@dataclass
class ConfirmationRequest:
    token: str
    task_id: str
    call_id: str
    tool_name: str
    risk_level: str
    summary: str
    created_at: str
    expires_at: str
    status: str = "pending"  # pending / approved / rejected
    operator: str | None = None
    decision: str | None = None
    resolved_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "confirmation_token": self.token,
            "task_id": self.task_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "operator": self.operator,
            "decision": self.decision,
            "resolved_at": self.resolved_at,
        }


class ConfirmationManager:
    """一次性、任务绑定、可过期的确认令牌管理 + 审计。"""

    def __init__(self, ttl_s: int = 300) -> None:
        self.ttl_s = ttl_s
        self._requests: dict[str, ConfirmationRequest] = {}
        self.audit: list[dict[str, Any]] = []

    def request(self, task_id: str, tool_name: str, risk_level: str, summary: str, call_id: str | None = None) -> ConfirmationRequest:
        if risk_level not in CONFIRM_LEVELS:
            raise ConfirmationError("UH-USR-1003", f"风险级 {risk_level} 无需人工确认")
        now = _now()
        req = ConfirmationRequest(
            token=secrets.token_urlsafe(24),
            task_id=task_id,
            call_id=call_id or ("call_" + secrets.token_hex(8)),
            tool_name=tool_name,
            risk_level=risk_level,
            summary=summary,
            created_at=_iso(now),
            expires_at=_iso(datetime.fromtimestamp(now.timestamp() + self.ttl_s, tz=timezone.utc)),
        )
        self._requests[req.token] = req
        log_event(log, 20, "tool.confirm.requested", task_id=task_id, tool_name=tool_name, risk_level=risk_level, call_id=req.call_id)
        return req

    def get(self, token: str) -> ConfirmationRequest | None:
        return self._requests.get(token)

    def pending_for_task(self, task_id: str) -> list[ConfirmationRequest]:
        return [r for r in self._requests.values() if r.task_id == task_id and r.status == "pending"]

    def resolve(self, token: str, decision: str, *, operator: str = "user", task_id: str | None = None) -> ConfirmationRequest:
        if decision not in {"approved", "rejected"}:
            raise ConfirmationError("UH-USR-1003", "decision 须为 approved/rejected")
        req = self._requests.get(token)
        if req is None:
            raise ConfirmationError("UH-USR-1003", "confirmation_token 无效")
        if req.status != "pending":
            # 一次性防重放：已裁决的令牌不可再用
            raise ConfirmationError("UH-USR-1003", "confirmation_token 已使用（防重放）", detail={"status": req.status})
        if task_id is not None and req.task_id != task_id:
            raise ConfirmationError("UH-USR-1002", "confirmation_token 与任务不匹配", detail={"resource": "task", "task_id": task_id})
        if _now().timestamp() > datetime.fromisoformat(req.expires_at.replace("Z", "+00:00")).timestamp():
            req.status = "rejected"
            req.decision = "rejected"
            req.operator = "timeout"
            req.resolved_at = _iso(_now())
            self._audit(req)
            raise ConfirmationError("UH-USR-1003", "confirmation_token 已过期")
        req.status = decision
        req.decision = decision
        req.operator = operator
        req.resolved_at = _iso(_now())
        self._audit(req)
        return req

    def mark_timeout(self, req: ConfirmationRequest) -> ConfirmationRequest:
        """TTL 到期自动收敛（M1 审查修复）：标记 rejected/timeout 并审计，
        使 resolved_by=timeout 路径真实可达，任务不再永久挂起。"""
        if req.status == "pending":
            req.status = "rejected"
            req.decision = "rejected"
            req.operator = "timeout"
            req.resolved_at = _iso(_now())
            self._audit(req)
        return req

    def _audit(self, req: ConfirmationRequest) -> None:
        record = {
            "event": "tool.confirm",
            "task_id": req.task_id,
            "call_id": req.call_id,
            "tool_name": req.tool_name,
            "risk_level": req.risk_level,
            "operator": req.operator,
            "decision": req.decision,
            "ts": req.resolved_at,
        }
        self.audit.append(record)
        log_event(log, 20, "tool.confirm", **{k: v for k, v in record.items() if k != "event"})
