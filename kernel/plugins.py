"""M2-1 插件驱动基础设施。

本模块只负责插件包的可信安装边界和生命周期协调。插件运行时隔离由
M2-2 的 sandbox runner 提供，工具/能力/钩子的具体协议由后续 SDK 接入。

插件包是 `.uhpkg` ZIP，根目录必须包含 ``manifest.yaml``。签名文件使用
JSON 格式，签名的是排除 ``SIGNATURE`` 后按路径排序的规范化包体摘要：

.. code-block:: json

   {
     "algorithm": "ed25519",
     "key_id": "uh-official-2026",
     "digest": "<sha256 hex>",
     "signature": "<base64>"
   }

公钥不随包信任，必须由调用方通过 ``PluginSecurityConfig.trusted_keys``
提供。这样可以避免插件包自带公钥后自行建立信任根。
"""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import total_ordering
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft7Validator, RefResolver

from .logging import get_logger, log_event
from .themes import validate_theme_asset_sizes, validate_theme_config, validate_theme_package

log = get_logger("kernel.plugins")

MANIFEST_ROOT = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "manifest"
MANIFEST_SCHEMA = MANIFEST_ROOT.parent / "manifest.schema.json"
PLUGIN_ROOT_ENV = "UH_PLUGIN_ROOT"
PLUGIN_REGISTRY_ENV = "UH_PLUGIN_REGISTRY"
DEFAULT_PLUGIN_ROOT = Path(".uh") / "plugins"
DEFAULT_PLUGIN_REGISTRY = Path(".uh") / "plugins.json"
DEACTIVATE_TIMEOUT_S = 5.0
SIGNATURE_FILE = "SIGNATURE"


class PluginError(Exception):
    """结构化插件错误，字段与 UH! 错误响应保持一致。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": 0,
            "detail": dict(self.detail),
        }


class PluginValidationError(PluginError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__("UH-USR-1003", message, detail=detail)


class PluginPolicyError(PluginError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__("UH-KRN-2010", message, status_code=403, detail=detail)


@total_ordering
@dataclass(frozen=True)
class SemVer:
    """最小 SemVer 值对象，支持插件依赖需要的比较语义。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = field(default_factory=tuple, compare=False)

    _RE = re.compile(
        r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-((?:0|[1-9]\d*|\d*[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
        r"(?:\.(?:0|[1-9]\d*|\d*[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
    )

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = cls._RE.fullmatch(str(value).strip())
        if not match:
            raise ValueError(f"invalid SemVer: {value}")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), pre)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base + ("-" + ".".join(self.prerelease) if self.prerelease else "")

    def _cmp_key(self) -> tuple[int, int, int, int, tuple[tuple[int, Any], ...]]:
        if not self.prerelease:
            pre_key: tuple[tuple[int, Any], ...] = ((1, 0),)
        else:
            values: list[tuple[int, Any]] = []
            for part in self.prerelease:
                values.append((0, int(part)) if part.isdigit() else (1, part))
            pre_key = ((0, tuple(values)),)
        return self.major, self.minor, self.patch, pre_key[0][0], pre_key[0][1]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() < other._cmp_key()

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        return not self <= other

    def __ge__(self, other: object) -> bool:
        return not self < other


def _range_satisfies(version: SemVer, expression: str) -> bool:
    """判断常用 SemVer 范围，覆盖 manifest 依赖的最小表达集。"""

    expr = str(expression or "").strip()
    if not expr or expr in {"*", "x", "X"}:
        return True
    if " || " in expr:
        return any(_range_satisfies(version, item) for item in expr.split(" || "))
    for token in expr.replace(",", " ").split():
        if token.startswith("^"):
            base = SemVer.parse(token[1:])
            if base.major > 0:
                upper = SemVer(base.major + 1, 0, 0)
            elif base.minor > 0:
                upper = SemVer(0, base.minor + 1, 0)
            else:
                upper = SemVer(0, 0, base.patch + 1)
            if not (version >= base and version < upper):
                return False
            continue
        if token.startswith("~"):
            base = SemVer.parse(token.lstrip("~"))
            upper = SemVer(base.major, base.minor + 1, 0)
            if not (version >= base and version < upper):
                return False
            continue
        match = re.match(r"^(>=|<=|>|<|=)?(.+)$", token)
        if not match:
            return False
        operator, raw = match.groups()
        if raw.lower() in {"x", "*"}:
            continue
        wildcard = raw.split(".")
        if any(part.lower() in {"x", "*"} for part in wildcard):
            if len(wildcard) == 1 or wildcard[0].lower() in {"x", "*"}:
                continue
            if len(wildcard) == 2:
                if version.major != int(wildcard[0]):
                    return False
            elif len(wildcard) >= 3:
                if (version.major, version.minor) != (int(wildcard[0]), int(wildcard[1])):
                    return False
            continue
        try:
            target = SemVer.parse(raw)
        except ValueError:
            return False
        if operator in {None, "="} and version != target:
            return False
        if operator == ">=" and version < target:
            return False
        if operator == "<=" and version > target:
            return False
        if operator == ">" and version <= target:
            return False
        if operator == "<" and version >= target:
            return False
    return True


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    type: str
    description: str
    author: str
    license: str
    permissions: tuple[str, ...]
    entry: str | None
    config: dict[str, Any]
    dependencies: tuple[dict[str, str], ...] = ()
    network: dict[str, Any] | None = None
    risk_level: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginManifest":
        return cls(
            name=str(value["name"]),
            version=str(value["version"]),
            type=str(value["type"]),
            description=str(value["description"]),
            author=str(value["author"]),
            license=str(value["license"]),
            permissions=tuple(str(item) for item in value.get("permissions", [])),
            entry=str(value["entry"]) if value.get("entry") is not None else None,
            config=dict(value.get("config") or {}),
            dependencies=tuple(dict(item) for item in value.get("dependencies", [])),
            network=dict(value["network"]) if value.get("network") else None,
            risk_level=str(value["risk_level"]) if value.get("risk_level") is not None else None,
            raw=dict(value),
        )

    def to_info(self, *, status: str, package_sha256: str, install_path: str) -> dict[str, Any]:
        result = {
            "name": self.name,
            "version": self.version,
            "type": self.type,
            "status": status,
            "permissions": list(self.permissions),
            "package_sha256": package_sha256,
            "install_path": install_path,
        }
        if self.risk_level is not None:
            result["risk_level"] = self.risk_level
        return result


@dataclass
class PluginRecord:
    manifest: PluginManifest
    status: str
    package_sha256: str
    install_path: str
    source: str
    installed_at: str
    updated_at: str
    crash_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.raw,
            "status": self.status,
            "package_sha256": self.package_sha256,
            "install_path": self.install_path,
            "source": self.source,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "crash_count": self.crash_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginRecord":
        return cls(
            manifest=PluginManifest.from_dict(value["manifest"]),
            status=str(value["status"]),
            package_sha256=str(value["package_sha256"]),
            install_path=str(value["install_path"]),
            source=str(value.get("source", "")),
            installed_at=str(value.get("installed_at", "")),
            updated_at=str(value.get("updated_at", "")),
            crash_count=int(value.get("crash_count", 0)),
        )

    def info(self) -> dict[str, Any]:
        return self.manifest.to_info(
            status=self.status,
            package_sha256=self.package_sha256,
            install_path=self.install_path,
        )


@dataclass
class PluginSecurityConfig:
    signature_required: bool = True
    signature_exceptions: set[str] = field(default_factory=set)
    trusted_keys: dict[str, bytes | str | Ed25519PublicKey] = field(default_factory=dict)
    allow_unverified: bool = False

    @classmethod
    def from_env(cls) -> "PluginSecurityConfig":
        exceptions = {
            item.strip()
            for item in os.environ.get("UH_PLUGIN_SIGNATURE_EXCEPTIONS", "").split(",")
            if item.strip()
        }
        allow_unverified = _truthy(os.environ.get("UH_PLUGIN_ALLOW_UNVERIFIED")) and _truthy(os.environ.get("UH_LOCAL_MODE"))
        trusted_keys: dict[str, bytes | str | Ed25519PublicKey] = {}
        trusted_path = os.environ.get("UH_PLUGIN_TRUSTED_KEYS_FILE")
        if trusted_path:
            try:
                loaded = json.loads(Path(trusted_path).read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    trusted_keys = {str(key): str(value) for key, value in loaded.items() if isinstance(value, str) and value.strip()}
            except (OSError, json.JSONDecodeError, TypeError):
                log_event(log, 30, "plugin.trust_store.unavailable", path=trusted_path)
        if not trusted_keys and _truthy(os.environ.get("UH_AUTH_REQUIRED")) and not _truthy(os.environ.get("UH_LOCAL_MODE")):
            log_event(log, 30, "plugin.trust_store.empty", reason="UH_PLUGIN_TRUSTED_KEYS_FILE is not configured")
        return cls(
            signature_required=not _falsey(os.environ.get("UH_PLUGIN_SIGNATURE_REQUIRED")),
            signature_exceptions=exceptions,
            trusted_keys=trusted_keys,
            allow_unverified=allow_unverified,
        )


class PluginLoader(Protocol):
    async def load(self, manifest: PluginManifest, package_dir: Path) -> None: ...

    async def activate(self, manifest: PluginManifest, package_dir: Path) -> None: ...

    async def deactivate(self, manifest: PluginManifest, package_dir: Path) -> None: ...

    async def uninstall(self, manifest: PluginManifest, package_dir: Path) -> None: ...

    def force_kill(self, manifest: PluginManifest, package_dir: Path) -> None: ...


class NoopPluginLoader:
    """M2-1 默认加载器。

    它完成清单/入口边界检查并提供生命周期钩子，实际进程启动交给 M2-2。
    """

    async def load(self, manifest: PluginManifest, package_dir: Path) -> None:
        if manifest.type != "theme" and manifest.entry:
            entry = package_dir / manifest.entry
            if entry.suffix == ".py":
                try:
                    ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
                except (OSError, SyntaxError) as exc:
                    raise PluginValidationError("插件入口语法校验失败", detail={"plugin": manifest.name, "entry": manifest.entry}) from exc

    async def activate(self, manifest: PluginManifest, package_dir: Path) -> None:
        return None

    async def deactivate(self, manifest: PluginManifest, package_dir: Path) -> None:
        return None

    async def uninstall(self, manifest: PluginManifest, package_dir: Path) -> None:
        return None

    def force_kill(self, manifest: PluginManifest, package_dir: Path) -> None:
        return None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decode_public_key(value: bytes | str | Ed25519PublicKey) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    raw: bytes
    if isinstance(value, bytes):
        raw = value
    else:
        text = value.strip()
        try:
            raw = base64.b64decode(text, validate=True)
        except Exception:
            raw = bytes.fromhex(text)
    return Ed25519PublicKey.from_public_bytes(raw)


def _safe_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise PluginValidationError("插件包包含非法路径", detail={"path": name})
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginValidationError("插件包路径越界", detail={"path": name})
    return path.as_posix()


def _canonical_digest(entries: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(entries, key=lambda item: item[0]):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_manifest_from_bytes(raw: bytes, source: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PluginValidationError("manifest.yaml 无法解析", detail={"source": source}) from exc
    if not isinstance(value, dict):
        raise PluginValidationError("manifest.yaml 须为对象", detail={"source": source})
    return value


def _validate_manifest(value: Mapping[str, Any], *, package_entries: set[str] | None = None) -> PluginManifest:
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    resolver = RefResolver(MANIFEST_ROOT.as_uri() + "/", schema)
    validator = Draft7Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(dict(value)), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        path = ".".join(str(item) for item in first.path) or "manifest"
        raise PluginValidationError("manifest 校验失败", detail={"field": path, "reason": first.message})
    manifest = PluginManifest.from_dict(value)
    try:
        SemVer.parse(manifest.version)
    except ValueError as exc:
        raise PluginValidationError("插件版本不是合法 SemVer", detail={"plugin": manifest.name, "version": manifest.version}) from exc
    if package_entries is not None and manifest.type != "theme":
        if not manifest.entry or manifest.entry not in package_entries:
            raise PluginValidationError("插件入口文件不存在", detail={"plugin": manifest.name, "entry": manifest.entry})
    return manifest


def _read_signature(raw: bytes, source: str) -> dict[str, str]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPolicyError("SIGNATURE 格式无效", detail={"source": source, "violation": "signature_format"}) from exc
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value.get(key) for key in ("algorithm", "key_id", "digest", "signature")):
        raise PluginPolicyError("SIGNATURE 字段不完整", detail={"source": source, "violation": "signature_format"})
    return {key: str(value[key]) for key in ("algorithm", "key_id", "digest", "signature")}


def _verify_signature(signature: Mapping[str, str], digest: str, config: PluginSecurityConfig, plugin_name: str) -> None:
    if signature.get("algorithm") != "ed25519":
        raise PluginPolicyError("插件签名算法不受支持", detail={"plugin": plugin_name, "violation": "signature_algorithm"})
    if signature.get("digest", "").lower() != digest.lower():
        raise PluginPolicyError("插件包 SHA-256 校验失败", detail={"plugin": plugin_name, "violation": "sha256"})
    key_id = signature.get("key_id", "")
    trusted = config.trusted_keys.get(key_id)
    if trusted is None:
        raise PluginPolicyError("插件签名公钥不受信任", detail={"plugin": plugin_name, "key_id": key_id, "violation": "signature_trust"})
    try:
        key = _decode_public_key(trusted)
        key.verify(base64.b64decode(signature["signature"], validate=True), bytes.fromhex(digest))
    except (ValueError, InvalidSignature) as exc:
        raise PluginPolicyError("插件签名校验失败", detail={"plugin": plugin_name, "key_id": key_id, "violation": "signature"}) from exc


def _safe_resolve(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PluginPolicyError("插件路径越界", detail={"resource": str(path), "violation": "path"})
    return resolved


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


class PluginDriver:
    """插件包安装、校验、生命周期和注册表管理。"""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        registry_path: str | Path | None = None,
        security: PluginSecurityConfig | None = None,
        loaders: Mapping[str, PluginLoader] | None = None,
        sandbox_runner: Any = None,
    ) -> None:
        self.root = Path(root or os.environ.get(PLUGIN_ROOT_ENV, DEFAULT_PLUGIN_ROOT))
        self.registry_path = Path(registry_path or os.environ.get(PLUGIN_REGISTRY_ENV, DEFAULT_PLUGIN_REGISTRY))
        self.security = security or PluginSecurityConfig.from_env()
        self.records: dict[str, PluginRecord] = {}
        default_loader: PluginLoader = NoopPluginLoader()
        if sandbox_runner is not None:
            from .sandbox import ProcessPluginLoader

            process_loader = ProcessPluginLoader(sandbox_runner, on_crash=self._on_plugin_crash)
            self.loaders: dict[str, PluginLoader] = {
                "tool": process_loader,
                "theme": default_loader,
                "capability": process_loader,
                "hook": process_loader,
            }
        else:
            self.loaders = {"tool": default_loader, "theme": default_loader, "capability": default_loader, "hook": default_loader}
        if loaders:
            self.loaders.update(loaders)
        self._load_registry()

    async def _on_plugin_crash(self, manifest: PluginManifest, package_dir: Path) -> bool:
        record = self.records.get(manifest.name)
        if record is None:
            return False
        record.crash_count += 1
        record.updated_at = _now()
        if record.crash_count >= 3:
            record.status = "quarantined"
            self._save_registry()
            log_event(log, 30, "plugin.quarantined", plugin=manifest.name, crash_count=record.crash_count)
            return False
        record.status = "active"
        self._save_registry()
        log_event(log, 30, "plugin.crash", plugin=manifest.name, crash_count=record.crash_count)
        return True

    def _load_registry(self) -> None:
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(value, dict):
            return
        for name, raw in value.items():
            try:
                record = PluginRecord.from_dict(raw)
                if record.manifest.name != name:
                    continue
                _safe_resolve(Path(record.install_path), self.root)
                if record.status == "active":
                    # A process is never considered active until the current
                    # runtime has attached a loader and started it.
                    record.status = "installed"
                self.records[name] = record
            except (KeyError, TypeError, ValueError, PluginError):
                continue

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="plugins-", suffix=".json", dir=str(self.registry_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({name: record.to_dict() for name, record in sorted(self.records.items())}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.registry_path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        return [record.info() for _, record in sorted(self.records.items())]

    def get(self, name: str) -> dict[str, Any] | None:
        record = self.records.get(name)
        return record.info() if record else None

    def _loader(self, plugin_type: str) -> PluginLoader:
        loader = self.loaders.get(plugin_type)
        if loader is None:
            raise PluginValidationError("插件类型没有对应加载器", detail={"type": plugin_type})
        return loader

    def _resolve_source(self, source: str | Path) -> tuple[Path, Any]:
        """Resolve local, market:// and git+https:// sources without shell evaluation."""
        raw = str(source)
        local = Path(raw)
        if local.exists():
            return local.resolve(), lambda: None
        if raw.startswith("market://"):
            reference = raw[len("market://"):]
            if "@" not in reference:
                raise PluginValidationError("market 来源须为 market://name@version", detail={"source": raw})
            name, version = reference.rsplit("@", 1)
            root = Path(os.environ.get("UH_PLUGIN_MARKET_ROOT", ".uh/market")).resolve()
            candidates = (
                root / f"{name}@{version}.uhpkg",
                root / f"{name}-{version}.uhpkg",
                root / name / version,
            )
            for candidate in candidates:
                if candidate.exists():
                    return candidate, lambda: None
            raise PluginValidationError("market 插件不存在", detail={"source": raw, "market_root": str(root)})
        if raw.startswith("git+https://"):
            url = raw[len("git+"):]
            parsed = urlparse(url)
            allowed = {item.strip().lower() for item in os.environ.get("UH_PLUGIN_GIT_HOSTS", "github.com").split(",") if item.strip()}
            if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed:
                raise PluginPolicyError("git 来源主机不在白名单", detail={"source": raw, "violation": "git.host"})
            checkout = Path(tempfile.mkdtemp(prefix="uh-plugin-git-"))
            try:
                subprocess.run(["git", "clone", "--depth", "1", "--", url, str(checkout)], check=True, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                shutil.rmtree(checkout, ignore_errors=True)
                raise PluginValidationError("git 插件源获取失败", detail={"source": raw}) from exc
            candidates = [checkout]
            candidates.extend(path for path in checkout.iterdir() if path.is_dir() and (path / "manifest.yaml").exists())
            for candidate in candidates:
                if (candidate / "manifest.yaml").exists():
                    return candidate, lambda: shutil.rmtree(checkout, ignore_errors=True)
            shutil.rmtree(checkout, ignore_errors=True)
            raise PluginValidationError("git 插件源缺少 manifest.yaml", detail={"source": raw})
        raise PluginValidationError("安装源不存在或格式不支持", detail={"source": raw})

    @staticmethod
    def _read_source(source: Path, *, verify_signature: bool, driver: "PluginDriver"):
        reader = driver._read_directory if source.is_dir() else driver._read_package
        return reader(source, verify_signature=verify_signature)

    def _check_dependencies(self, manifest: PluginManifest) -> None:
        for dependency in manifest.dependencies:
            name = str(dependency.get("name", ""))
            expression = str(dependency.get("version", ""))
            dependency_record = self.records.get(name)
            if dependency_record is None or not _range_satisfies(SemVer.parse(dependency_record.manifest.version), expression):
                raise PluginValidationError("插件依赖未就绪", detail={"plugin": manifest.name, "dependency": name, "version": expression})

    def _read_package(self, package: Path, *, verify_signature: bool = True) -> tuple[PluginManifest, str, dict[str, bytes], dict[str, str] | None]:
        if not package.is_file() or package.suffix.lower() != ".uhpkg":
            raise PluginValidationError("安装源必须是 .uhpkg 文件", detail={"source": str(package)})
        try:
            archive = zipfile.ZipFile(package)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PluginValidationError("插件包不是有效 ZIP", detail={"source": str(package)}) from exc
        with archive:
            members: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_member_name(info.filename)
                if name in members:
                    raise PluginValidationError("插件包包含重复路径", detail={"path": name})
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PluginValidationError("插件包禁止符号链接", detail={"path": name})
                members[name] = info
            if "manifest.yaml" not in members:
                raise PluginValidationError("插件包缺少 manifest.yaml", detail={"source": str(package)})
            raw_manifest = archive.read(members.pop("manifest.yaml"))
            manifest = _validate_manifest(_read_manifest_from_bytes(raw_manifest, str(package)), package_entries=set(members) | {"manifest.yaml"})
            if manifest.type == "theme":
                self._validate_theme_sizes({name: info.file_size for name, info in members.items() if name != SIGNATURE_FILE})
            entries = {"manifest.yaml": raw_manifest, **{name: archive.read(info) for name, info in members.items()}}
        self._validate_theme_entries(manifest, entries)
        content_entries = [(name, content) for name, content in entries.items() if name != SIGNATURE_FILE]
        digest = _canonical_digest(content_entries)
        signature = _read_signature(entries[SIGNATURE_FILE], str(package)) if SIGNATURE_FILE in entries else None
        if verify_signature and self.security.signature_required and manifest.type not in self.security.signature_exceptions:
            if signature is None:
                raise PluginPolicyError("插件包缺少签名", detail={"plugin": manifest.name, "violation": "signature_missing"})
            _verify_signature(signature, digest, self.security, manifest.name)
        elif verify_signature and signature is not None:
            _verify_signature(signature, digest, self.security, manifest.name)
        return manifest, digest, entries, signature

    def _read_directory(self, source: Path, *, verify_signature: bool = True) -> tuple[PluginManifest, str, dict[str, bytes], dict[str, str] | None]:
        if not source.is_dir():
            raise PluginValidationError("验证源必须是插件目录或 .uhpkg 文件", detail={"source": str(source)})
        members: dict[str, Path] = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise PluginValidationError("插件目录禁止符号链接", detail={"path": str(path.relative_to(source))})
            if path.is_dir():
                continue
            name = _safe_member_name(path.relative_to(source).as_posix())
            if name in members:
                raise PluginValidationError("插件目录包含重复路径", detail={"path": name})
            members[name] = path
        if "manifest.yaml" not in members:
            raise PluginValidationError("插件目录缺少 manifest.yaml", detail={"source": str(source)})
        raw_manifest = members.pop("manifest.yaml").read_bytes()
        manifest = _validate_manifest(_read_manifest_from_bytes(raw_manifest, str(source)), package_entries=set(members) | {"manifest.yaml"})
        if manifest.type == "theme":
            self._validate_theme_sizes({name: path.stat().st_size for name, path in members.items() if name != SIGNATURE_FILE})
        entries = {"manifest.yaml": raw_manifest, **{name: path.read_bytes() for name, path in members.items()}}
        self._validate_theme_entries(manifest, entries)
        content_entries = [(name, content) for name, content in entries.items() if name != SIGNATURE_FILE]
        digest = _canonical_digest(content_entries)
        signature = _read_signature(entries[SIGNATURE_FILE], str(source)) if SIGNATURE_FILE in entries else None
        if verify_signature and self.security.signature_required and manifest.type not in self.security.signature_exceptions:
            if signature is None:
                raise PluginPolicyError("插件源缺少签名", detail={"plugin": manifest.name, "violation": "signature_missing"})
            _verify_signature(signature, digest, self.security, manifest.name)
        elif verify_signature and signature is not None:
            _verify_signature(signature, digest, self.security, manifest.name)
        return manifest, digest, entries, signature

    @staticmethod
    def _validate_theme_sizes(sizes: Mapping[str, int]) -> None:
        try:
            validate_theme_asset_sizes(sizes)
        except ValueError as exc:
            raise PluginValidationError("主题包校验失败", detail={"field": "theme", "reason": str(exc)}) from exc

    @staticmethod
    def _validate_theme_entries(manifest: PluginManifest, entries: Mapping[str, bytes]) -> None:
        if manifest.type != "theme":
            return
        try:
            assets = {name: content for name, content in entries.items() if name not in {"manifest.yaml", SIGNATURE_FILE}}
            validate_theme_asset_sizes({name: len(content) for name, content in assets.items()})
            validate_theme_package(manifest.config, assets)
        except ValueError as exc:
            raise PluginValidationError("主题包校验失败", detail={"field": "theme", "reason": str(exc)}) from exc

    def validate(self, source: str | Path, *, no_verify: bool = False) -> dict[str, Any]:
        package, cleanup = self._resolve_source(source)
        try:
            if no_verify:
                if not self.security.allow_unverified:
                    raise PluginPolicyError("当前模式禁止跳过插件签名校验", detail={"violation": "signature_bypass"})
                manifest, digest, entries, _ = self._read_source(package, verify_signature=False, driver=self)
            else:
                manifest, digest, entries, _ = self._read_source(package, verify_signature=True, driver=self)
            return {**manifest.to_info(status="validated", package_sha256=digest, install_path=""), "files": sorted(entries), "dependencies": list(manifest.dependencies)}
        finally:
            cleanup()

    async def install(self, source: str | Path, *, no_verify: bool = False) -> dict[str, Any]:
        original_source = str(source)
        package, cleanup_source = self._resolve_source(source)
        if no_verify and not self.security.allow_unverified:
            cleanup_source()
            raise PluginPolicyError("当前模式禁止跳过插件签名校验", detail={"violation": "signature_bypass"})
        try:
            manifest, digest, entries, _ = self._read_source(package, verify_signature=not no_verify, driver=self)
        except Exception:
            cleanup_source()
            raise
        if no_verify:
            # _read_package 已经做了签名校验路径判断，显式 bypass 只允许在本地开发配置中使用。
            pass
        self._check_dependencies(manifest)
        current = self.records.get(manifest.name)
        if current is not None:
            if current.package_sha256 == digest:
                return current.info()
            raise PluginValidationError("同名插件已安装不同版本", detail={"plugin": manifest.name})

        self.root.mkdir(parents=True, exist_ok=True)
        staging_parent = self.root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{manifest.name}-", dir=str(staging_parent)))
        target = _safe_resolve(self.root / manifest.name / manifest.version, self.root)
        record: PluginRecord | None = None
        try:
            for name, content in entries.items():
                destination = _safe_resolve(staging / name, staging)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            loader = self._loader(manifest.type)
            await loader.load(manifest, staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise PluginValidationError("插件安装目录已存在", detail={"plugin": manifest.name})
            staging.rename(target)
            record = PluginRecord(manifest, "installed", digest, str(target), original_source, _now(), _now())
            self.records[manifest.name] = record
            self._save_registry()
            try:
                await loader.activate(manifest, target)
            except Exception as exc:
                await self._deactivate_loader(loader, manifest, target, force=True)
                self.records.pop(manifest.name, None)
                self._save_registry()
                shutil.rmtree(target, ignore_errors=True)
                _remove_empty_directory(target.parent)
                raise PluginError("UH-KRN-2010", "插件 activate 失败，已回滚", status_code=400, detail={"plugin": manifest.name, "stage": "activate"}) from exc
            record.status = "active"
            record.updated_at = _now()
            self._save_registry()
            log_event(log, 20, "plugin.install", plugin=manifest.name, version=manifest.version, plugin_type=manifest.type)
            return record.info()
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if staging_parent.exists() and not any(staging_parent.iterdir()):
                staging_parent.rmdir()
            cleanup_source()

    async def _deactivate_loader(self, loader: PluginLoader, manifest: PluginManifest, path: Path, *, force: bool = False) -> bool:
        try:
            await asyncio.wait_for(loader.deactivate(manifest, path), timeout=DEACTIVATE_TIMEOUT_S)
            return False
        except asyncio.TimeoutError:
            loader.force_kill(manifest, path)
            return True
        except Exception:
            if force:
                loader.force_kill(manifest, path)
                return True
            raise

    async def deactivate(self, name: str) -> dict[str, Any]:
        record = self.records.get(name)
        if record is None:
            raise PluginValidationError("插件不存在", detail={"plugin": name})
        if record.status not in {"active", "quarantined"}:
            return record.info()
        loader = self._loader(record.manifest.type)
        forced = await self._deactivate_loader(loader, record.manifest, Path(record.install_path))
        record.status = "quarantined" if forced else "installed"
        record.updated_at = _now()
        self._save_registry()
        log_event(log, 20, "plugin.deactivate", plugin=name, forced=forced)
        return record.info()

    async def uninstall(self, name: str) -> dict[str, Any]:
        record = self.records.get(name)
        if record is None:
            raise PluginValidationError("插件不存在", detail={"plugin": name})
        loader = self._loader(record.manifest.type)
        path = _safe_resolve(Path(record.install_path), self.root)
        if record.status == "active":
            await self._deactivate_loader(loader, record.manifest, path, force=True)
        try:
            await loader.uninstall(record.manifest, path)
        finally:
            shutil.rmtree(path, ignore_errors=True)
            _remove_empty_directory(path.parent)
            self.records.pop(name, None)
            self._save_registry()
        if path.exists():
            raise PluginError("UH-KRN-2010", "插件卸载后仍有残留文件", status_code=500, detail={"plugin": name, "violation": "residue"})
        log_event(log, 20, "plugin.uninstall", plugin=name)
        return {"name": record.manifest.name, "version": record.manifest.version, "status": "uninstalled"}

    async def update_config(self, name: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
        record = self.records.get(name)
        if record is None:
            raise PluginValidationError("插件不存在", detail={"plugin": name})
        requested = dict(config or {})
        allowed = set(record.manifest.config)
        unknown = sorted(set(requested) - allowed)
        if unknown:
            raise PluginValidationError("插件配置包含未知字段", detail={"plugin": name, "fields": unknown})
        manifest = _validate_manifest({**record.manifest.raw, "config": {**record.manifest.config, **requested}})
        if manifest.type == "theme":
            try:
                validate_theme_config(manifest.config)
            except ValueError as exc:
                raise PluginValidationError("主题配置校验失败", detail={"field": "config", "reason": str(exc)}) from exc
        old_config = dict(record.manifest.config)
        old_timestamp = record.updated_at
        record.manifest.config.update(manifest.config)
        record.manifest.raw["config"] = dict(manifest.config)
        record.updated_at = _now()
        try:
            self._save_registry()
        except OSError:
            record.manifest.config.clear()
            record.manifest.config.update(old_config)
            record.manifest.raw["config"] = old_config
            record.updated_at = old_timestamp
            raise
        return record.info()


__all__ = [
    "DEACTIVATE_TIMEOUT_S",
    "PluginDriver",
    "PluginError",
    "PluginLoader",
    "PluginManifest",
    "PluginPolicyError",
    "PluginRecord",
    "PluginSecurityConfig",
    "PluginValidationError",
    "SemVer",
]
