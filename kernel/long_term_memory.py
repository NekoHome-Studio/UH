"""长期记忆（M1-5）。

长期记忆把普通（L1）内容写入与 M1-3 共用的向量存储；敏感（L2）内容
只保留 AES-GCM 密文和可过滤的元数据，高敏（L3）内容直接拒绝。这样既
保留了本地优先的语义召回，也避免把 L2 明文或其语义表示暴露给向量库。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .logging import get_logger, log_event
from .retrieval import Document, LocalVectorStore

log = get_logger("kernel.long_term_memory")


class MemoryKeyError(RuntimeError):
    """L2 写入或解密所需的主密钥不可用。"""


class PrivacyLevel:
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


@dataclass(frozen=True)
class Classification:
    level: str
    reasons: tuple[str, ...] = ()


class PrivacyClassifier:
    """可扩展的轻量分级器；规则命中 L3 时优先于 L2。"""

    _id_card = re.compile(r"(?<!\d)(?:\d{17}[0-9Xx])(?!\d)")
    _bank_card = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
    _card_context = re.compile(r"(?i)(?:银行卡|信用卡|借记卡|卡号|bank\s*card|credit\s*card)")
    _secret = re.compile(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|private[_ -]?key|"
        r"bearer|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})"
    )
    _phone = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
    _email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
    _sensitive_words = re.compile(r"(?i)(?:住址|地址|家庭住址|手机号|手机号码|电话号码|生日|出生日期|病历|疾病|诊断|工资|薪资|社保|护照|驾驶证)")

    def classify(self, content: str, *, override: str | None = None) -> Classification:
        if override in {PrivacyLevel.L1, PrivacyLevel.L2, PrivacyLevel.L3}:
            return Classification(override, ("override",))
        text = str(content)
        reasons: list[str] = []
        if self._id_card.search(text):
            reasons.append("id_card")
        if self._secret.search(text):
            reasons.append("credential")
        if self._bank_card.search(text) and (self._looks_like_card(text) or self._card_context.search(text)):
            reasons.append("bank_card")
        if reasons:
            return Classification(PrivacyLevel.L3, tuple(reasons))
        if self._phone.search(text):
            reasons.append("phone")
        if self._email.search(text):
            reasons.append("email")
        if self._sensitive_words.search(text):
            reasons.append("sensitive_field")
        return Classification(PrivacyLevel.L2 if reasons else PrivacyLevel.L1, tuple(reasons))

    @staticmethod
    def _looks_like_card(text: str) -> bool:
        for match in re.finditer(r"(?<!\d)\d{13,19}(?!\d)", text):
            digits = match.group(0)
            # Luhn rejects dates and ordinary numeric prose while catching test cards.
            total = 0
            parity = len(digits) % 2
            for i, char in enumerate(digits):
                n = int(char)
                if i % 2 == parity:
                    n *= 2
                    n = n - 9 if n > 9 else n
                total += n
            if total % 10 == 0:
                return True
        return False


@dataclass
class LongTermEntry:
    entry_id: str
    session_id: str
    privacy_level: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    content: str | None = None
    ciphertext: str | None = None
    nonce: str | None = None

    @property
    def level(self) -> str:
        return self.privacy_level

    @property
    def encrypted_content(self) -> str | None:
        return self.ciphertext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str | date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class LongTermMemory:
    """本地长期记忆存储。

    ``write``/``recall`` 是同步的，方便 CLI 与单元测试；``a*`` 别名提供
    TaskManager 所需的异步适配，不引入新的持久化后端契约。
    """

    def __init__(
        self,
        store: LocalVectorStore | None = None,
        *,
        path: str | Path | None = None,
        vector_store: LocalVectorStore | None = None,
        classifier: PrivacyClassifier | None = None,
        master_key: bytes | str | None = None,
        guard: Any = None,
    ) -> None:
        self.classifier = classifier or PrivacyClassifier()
        # 三审 N9：长期记忆向量库也接存储隔离
        self.vector_store = vector_store or store or LocalVectorStore(
            os.environ.get("UH_LONG_TERM_VECTOR_STORE", ".uh/long_term_vectors.json"),
            guard=guard, module="memory.long_term", namespace="long_term",
        )
        self.path = Path(path or os.environ.get("UH_LONG_TERM_STORE", ".uh/long_term_memory.json"))
        self.entries: list[LongTermEntry] = []
        self._load()
        self._master_key = self._read_key(master_key)

    @staticmethod
    def _read_key(value: bytes | str | None) -> bytes | None:
        raw: bytes | str | None = value if value is not None else os.environ.get("UH_MEMORY_MASTER_KEY")
        if raw is None:
            return None
        if isinstance(raw, bytes):
            key = raw
        else:
            text = raw.strip()
            try:
                key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            except Exception:
                try:
                    key = bytes.fromhex(text)
                except ValueError:
                    key = text.encode("utf-8")
        if len(key) not in {16, 24, 32}:
            # 允许钥匙串返回口令式值，同时由 SHA-256 派生固定 AES-256 密钥；
            # 密钥材料仍只来自调用方/env，不在代码中提供默认秘密。
            key = hashlib.sha256(key).digest()
        return key

    def _load(self) -> None:
        """加载条目，兼容旧「单个 JSON 数组」与新「JSONL 追加」两种格式。

        JSONL 下同 entry_id 多行按后写覆盖前写归并。
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            self.entries = []
            return
        items: list[dict[str, Any]] = []
        if text.lstrip().startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = []
            items = [item for item in loaded if isinstance(item, dict)]
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        merged: dict[str, LongTermEntry] = {}
        for item in items:
            try:
                entry = LongTermEntry(**item)
            except TypeError:
                continue
            merged[entry.entry_id] = entry
        self.entries = list(merged.values())

    def _save(self) -> None:
        """全量重写（供 prune/裁剪等批量变更使用；单条写入走 _append）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(e) for e in self.entries], ensure_ascii=False), encoding="utf-8")

    def _append(self, entry: LongTermEntry) -> None:
        """单条追加，O(1)。原实现每次写入全量重写整个 entries 列表，
        存量增长后单次写入线性变慢（O(n²) 总成本）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _encrypt(self, content: str) -> tuple[str, str]:
        if self._master_key is None:
            raise MemoryKeyError("L2 长期记忆需要 UH_MEMORY_MASTER_KEY")
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(self._master_key).encrypt(nonce, content.encode("utf-8"), None)
        return base64.b64encode(cipher).decode("ascii"), base64.b64encode(nonce).decode("ascii")

    def decrypt(self, entry: LongTermEntry) -> str:
        if entry.privacy_level != PrivacyLevel.L2:
            return entry.content or ""
        if self._master_key is None or not entry.ciphertext or not entry.nonce:
            raise MemoryKeyError("L2 长期记忆需要 UH_MEMORY_MASTER_KEY")
        try:
            return AESGCM(self._master_key).decrypt(
                base64.b64decode(entry.nonce), base64.b64decode(entry.ciphertext), None
            ).decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
            raise MemoryKeyError("L2 长期记忆解密失败") from exc

    def write(
        self,
        content: str,
        *,
        session_id: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: str | date | datetime | None = None,
        privacy_level: str | None = None,
        memory_recall: bool = True,
    ) -> LongTermEntry | None:
        if not memory_recall:
            return None
        # 二审 GPT-6 P2：L3 不可被 privacy_level override 降级——自动分级命中 L3 一律拒入。
        auto = self.classifier.classify(content)
        if auto.level == PrivacyLevel.L3:
            log_event(log, logging.WARNING, "memory.long_term.rejected", session_id=session_id, privacy_level="L3", reasons=list(auto.reasons))
            return None
        classification = self.classifier.classify(content, override=privacy_level)
        if classification.level == PrivacyLevel.L3:
            log_event(log, logging.WARNING, "memory.long_term.rejected", session_id=session_id, privacy_level="L3", reasons=list(classification.reasons))
            return None
        timestamp = created_at if isinstance(created_at, str) else (_parse_time(created_at).isoformat().replace("+00:00", "Z") if created_at else _now())
        entry = LongTermEntry(
            entry_id="uh_mem_" + secrets.token_hex(12),
            session_id=session_id,
            privacy_level=classification.level,
            created_at=timestamp,
            metadata=dict(metadata or {}),
        )
        if classification.level == PrivacyLevel.L2:
            entry.ciphertext, entry.nonce = self._encrypt(str(content))
        else:
            entry.content = str(content)
            document = Document(entry.entry_id, entry.content, f"memory:{session_id}", "long-term")
            self.vector_store.add(document)
        self.entries.append(entry)
        self._append(entry)
        return entry

    add = write
    put = write

    def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        include_sensitive: bool = False,
        memory_recall: bool = True,
    ) -> list[dict[str, Any]]:
        if not memory_recall:
            return []
        allowed = self._metadata_matcher(session_id, metadata)
        rows = []
        for document, score in self.vector_store.query(query, max(1, top_k)):
            entry = next((item for item in self.entries if item.entry_id == document.document_id), None)
            if entry and allowed(entry):
                rows.append({"entry_id": entry.entry_id, "content": entry.content or "", "privacy_level": entry.privacy_level, "score": round(float(score), 6), "session_id": entry.session_id, "metadata": entry.metadata, "created_at": entry.created_at})
        # L2 is deliberately absent from semantic rows. It may only be returned by
        # an explicit metadata-filtered, authorized lookup.
        if include_sensitive and (metadata is not None or session_id is not None):
            for entry in self.entries:
                if entry.privacy_level == PrivacyLevel.L2 and allowed(entry):
                    try:
                        content = self.decrypt(entry)
                    except MemoryKeyError:
                        content = "[解密失败：记录隔离]"  # 二审 N13：单条坏 L2 不崩溃整体召回
                    rows.append({"entry_id": entry.entry_id, "content": content, "privacy_level": entry.privacy_level, "score": 0.0, "session_id": entry.session_id, "metadata": entry.metadata, "created_at": entry.created_at})
        return rows[: max(1, top_k)]

    search = recall

    @staticmethod
    def _metadata_matcher(session_id: str | None, metadata: dict[str, Any] | None):
        def matches(entry: LongTermEntry) -> bool:
            if session_id is not None and entry.session_id != session_id:
                return False
            return all(entry.metadata.get(k) == v for k, v in (metadata or {}).items())
        return matches

    def purge(self, before: str | date | datetime | None = None) -> int:
        cutoff = _parse_time(before)
        keep: list[LongTermEntry] = []
        removed_ids: set[str] = set()
        for entry in self.entries:
            when = _parse_time(entry.created_at)
            if cutoff is None or (when is not None and when < cutoff):
                removed_ids.add(entry.entry_id)
            else:
                keep.append(entry)
        self.entries = keep
        if removed_ids:
            self.vector_store.documents = [d for d in self.vector_store.documents if d.document_id not in removed_ids]
            self.vector_store._save()
            self._save()
        return len(removed_ids)

    def export(self, *, authorize_sensitive: bool = False, redact_sensitive: bool = True) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in self.entries:
            item = {"entry_id": entry.entry_id, "session_id": entry.session_id, "privacy_level": entry.privacy_level, "created_at": entry.created_at, "metadata": entry.metadata}
            if entry.privacy_level == PrivacyLevel.L2:
                if authorize_sensitive:
                    try:
                        item["content"] = self.decrypt(entry)
                    except MemoryKeyError:
                        item["content"] = "[解密失败：记录隔离]"  # 二审 N13：坏 L2 不崩溃整体导出
                elif redact_sensitive:
                    item["content"] = "[REDACTED]"
            else:
                item["content"] = entry.content or ""
            result.append(item)
        return result

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def vector_count(self) -> int:
        return len(self.vector_store.documents)

    @property
    def metric(self) -> int:
        return self.entry_count

    async def awrite(self, *args: Any, **kwargs: Any) -> LongTermEntry | None:
        """真异步写入。

        R2 审查 P0-2：原实现 `return self.write(...)` 只是协程壳，同步体
        （embedding + AES-GCM + 写盘，实测 ~84ms）仍在事件循环内执行。
        调用方按 `inspect.iscoroutinefunction` 判定会直接 await，
        使 to_thread 卸载分支永不触发——100 并发下 P95 劣化到 33s。
        """
        return await asyncio.to_thread(self.write, *args, **kwargs)

    async def arecall(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """真异步召回（同 P0-2：原协程壳阻塞事件循环 ~19ms/次）。"""
        return await asyncio.to_thread(self.recall, *args, **kwargs)


def create_long_term_memory(guard: Any = None) -> LongTermMemory:
    return LongTermMemory(guard=guard)

