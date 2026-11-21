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


_DANGEROUS_CALLS = {"os.system", "os.popen", "os.execv", "os.execve", "os.spawnl", "subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call", "eval", "exec", "ctypes.CDLL", "ctypes.WinDLL", "__import__"}
_NETWORK_MODULES = {"socket", "http.client", "urllib.request", "requests", "aiohttp", "httpx", "websocket", "websockets"}


_JS_DANGER = re.compile(r"child_process|execSync|\bexec\s*\(|\beval\s*\(|new\s+Function|\bspawn\s*\(")
_DANGEROUS_ATTRS = {"system", "popen", "execv", "execve", "spawnl", "spawnle", "Popen", "run", "call", "check_output", "check_call"}
# 尾名黑名单：任意命名空间（builtins.eval / b.eval / os.system 等）均命中
_DANGEROUS_TAILS = {"system", "popen", "execv", "execve", "spawnl", "spawnle", "Popen", "check_output", "check_call", "eval", "exec", "CDLL", "WinDLL", "__import__", "import_module", "getattr"}


def _static_scan(entries: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """SBX-02 出网/危险调用静态扫描（R-1 加固）：别名归一 + from-import + 动态调用 + JS 入口。"""
    findings: list[dict[str, Any]] = []
    for name, content in entries.items():
        lowered = name.lower()
        # F6：原生二进制/脚本入口无 AST 可解析 → 一律标记（不可静态验证即不放行）
        if lowered.endswith((".exe", ".dll", ".so", ".dylib", ".bin", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".com", ".scr")):
            findings.append({"file": name, "line": 1, "kind": "exec_danger", "detail": "原生/脚本入口不可静态验证"})
            continue
        if lowered.endswith((".js", ".mjs", ".cjs", ".ts")):
            text = content.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if _JS_DANGER.search(line):
                    findings.append({"file": name, "line": i, "kind": "exec_danger", "detail": "js危险调用"})
            continue
        if not lowered.endswith(".py"):
            continue
        # F5：非 UTF-8 源码（coding cookie 混淆）不得静默跳过——按声明编码解码，失败即标记
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            import codecs
            cookie = re.search(rb"coding[:=]\s*([-\w.]+)", content[:200])
            try:
                source = content.decode(cookie.group(1).decode("ascii")) if cookie else None
            except (LookupError, UnicodeDecodeError):
                source = None
            if source is None:
                findings.append({"file": name, "line": 1, "kind": "exec_danger", "detail": "源码编码无法解析，拒绝静态放行"})
                continue
        try:
            tree = ast.parse(source, filename=name)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            # 三轮对抗修复：无法解析的源码不得静默放行（null 字节/语法混淆曾绕过扫描）
            findings.append({"file": name, "line": 1, "kind": "exec_danger", "detail": "源码无法解析，拒绝静态放行"})
            continue
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    aliases[a.asname or a.name.split(".")[0]] = a.name
                    if a.name in _NETWORK_MODULES or a.name.split(".")[0] in _NETWORK_MODULES:
                        findings.append({"file": name, "line": node.lineno, "kind": "network", "detail": a.name})
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    aliases[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
                # F7：from urllib import request → urllib.request 也算出网导入
                submodules = {f"{mod}.{a.name}" for a in node.names} if mod else set()
                if mod in _NETWORK_MODULES or mod.split(".")[0] in _NETWORK_MODULES or any(s in _NETWORK_MODULES for s in submodules):
                    findings.append({"file": name, "line": node.lineno, "kind": "network", "detail": mod or ",".join(sorted(submodules))})

        # 四轮对抗修复：跟踪简单赋值别名（g = getattr / f = os.system），此前 g(os,'system') 绕过
        # 五轮扩展：容器存危险 callable（d={'k':os.system} / l=[os.system]）与类属性（class A: m=os.system）
        container_danger: set[str] = set()   # 存过危险 callable 的容器/类名
        def _dangerous_value(v: ast.expr) -> str | None:
            if isinstance(v, ast.Name):
                lbl = aliases.get(v.id, v.id)
                return lbl if (lbl in _DANGEROUS_CALLS or lbl.rsplit(".", 1)[-1] in _DANGEROUS_TAILS) else None
            if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
                base = aliases.get(v.value.id, v.value.id)
                lbl = f"{base}.{v.attr}"
                return lbl if (lbl in _DANGEROUS_CALLS or v.attr in _DANGEROUS_TAILS) else None
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                if isinstance(node.value, ast.Name):
                    aliases[target] = aliases.get(node.value.id, node.value.id)
                elif isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name):
                    base = aliases.get(node.value.value.id, node.value.value.id)
                    aliases[target] = f"{base}.{node.value.attr}"
                elif isinstance(node.value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                    parts = list(node.value.values) if isinstance(node.value, ast.Dict) else list(node.value.elts)
                    hits = [h for h in (_dangerous_value(p) for p in parts if p is not None) if h]
                    if hits:
                        container_danger.add(target)
                        findings.append({"file": name, "line": node.lineno, "kind": "exec_danger", "detail": f"容器存入危险调用 {hits[0]}"})
            # 六轮：实例属性赋危险 callable（a.m = os.system 后 a.m(...)）
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute):
                hit = _dangerous_value(node.value)
                if hit:
                    findings.append({"file": name, "line": node.lineno, "kind": "exec_danger", "detail": f"实例属性存入危险调用 {hit}"})
            # 类体内属性赋危险 callable
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        hits = [h for h in (_dangerous_value(stmt.value) if stmt.value is not None else None,) if h]
                        if hits:
                            container_danger.add(node.name)
                            findings.append({"file": name, "line": stmt.lineno, "kind": "exec_danger", "detail": f"类属性存入危险调用 {hits[0]}"})

        def resolve(func: ast.expr) -> str:
            if isinstance(func, ast.Name):
                return aliases.get(func.id, func.id)
            if isinstance(func, ast.Attribute):
                base = func.value.id if isinstance(func.value, ast.Name) else ""
                base = aliases.get(base, base)
                return f"{base}.{func.attr}" if base else func.attr
            # 嵌套调用返回 callable（如 getattr(os,'system')(...)）：解析内层以捕获危险取值
            if isinstance(func, ast.Call):
                return resolve(func.func)
            return ""

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            label = resolve(node.func)
            # 三轮对抗修复：按尾名匹配（此前 from builtins import eval / import builtins as b; b.eval 归一为
            # "builtins.eval" 不在全名黑名单 → 绕过）；任意命名空间下的危险函数名一律命中。
            tail = label.rsplit(".", 1)[-1]
            danger = (
                label in _DANGEROUS_CALLS
                or tail in _DANGEROUS_TAILS
                or label.endswith(".import_module")
            )
            # getattr(obj, 'system') 动态取危险属性（含别名归一后的 *.getattr）
            if tail == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in _DANGEROUS_ATTRS:
                danger = True
            # 五轮：危险 callable 作为实参传递（functools.partial(os.system,...) / map(os.system,...)）
            for arg in node.args:
                lbl_arg = resolve(arg) if isinstance(arg, (ast.Name, ast.Attribute)) else ""
                if lbl_arg and (lbl_arg in _DANGEROUS_CALLS or lbl_arg.rsplit(".", 1)[-1] in _DANGEROUS_TAILS):
                    danger = True
                    label = label or f"pass-dangerous:{lbl_arg}"
                    break
            # 下标取属性动态调用：vars(os)['system'] / os.__dict__['system'] / __builtins__['__import__']
            if isinstance(node.func, ast.Subscript) and isinstance(node.func.slice, ast.Constant) and node.func.slice.value in _DANGEROUS_ATTRS | {"__import__", "eval", "exec", "getattr"}:
                danger = True
                label = label or f"subscript[{node.func.slice.value}]"
            if danger:
                findings.append({"file": name, "line": node.lineno, "kind": "exec_danger", "detail": label or "dynamic_call"})
    return findings


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
        capability_registry: Any = None,
    ) -> None:
        self.root = Path(root or os.environ.get(PLUGIN_ROOT_ENV, DEFAULT_PLUGIN_ROOT))
        self.registry_path = Path(registry_path or os.environ.get(PLUGIN_REGISTRY_ENV, DEFAULT_PLUGIN_REGISTRY))
        self.security = security or PluginSecurityConfig.from_env()
        self.records: dict[str, PluginRecord] = {}
        self._restore_pending: set[str] = set()
        self._lifecycle_lock = asyncio.Lock()
        default_loader: PluginLoader = NoopPluginLoader()
        if sandbox_runner is not None:
            from .sandbox import MCPPluginLoader, ProcessPluginLoader

            process_loader = ProcessPluginLoader(sandbox_runner, on_crash=self._on_plugin_crash)
            self.loaders: dict[str, PluginLoader] = {
                "tool": process_loader,
                "theme": default_loader,
                "capability": process_loader,
                "hook": process_loader,
            }
            if capability_registry is not None:
                self.loaders["tool"] = MCPPluginLoader(
                    sandbox_runner, capability_registry, self.registry_path.with_name("plugin-sandbox-audit.jsonl"), self._on_plugin_crash,
                )
        else:
            self.loaders = {"tool": default_loader, "theme": default_loader, "capability": default_loader, "hook": default_loader}
        if loaders:
            self.loaders.update(loaders)
        self._load_registry()

    async def _on_plugin_crash(self, manifest: PluginManifest, package_dir: Path) -> bool:
        record = self.records.get(manifest.name)
        if record is None or record.status != "active":
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
                    self._restore_pending.add(name)
                self.records[name] = record
            except (KeyError, TypeError, ValueError, PluginError):
                continue

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="plugins-", suffix=".json", dir=str(self.registry_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                records = {name: record.to_dict() for name, record in sorted(self.records.items())}
                for name in self._restore_pending:
                    records[name]["status"] = "active"
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.registry_path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        return [record.info() for _, record in sorted(self.records.items())]

    def get(self, name: str) -> dict[str, Any] | None:
        record = self.records.get(name)
        return record.info() if record else None

    def tool_host(self, tool_name: str):
        from .sandbox import MCPPluginLoader

        loader = self._loader("tool")
        if isinstance(loader, MCPPluginLoader):
            name = loader.tools.get(tool_name)
            record = self.records.get(name) if name is not None else None
            if record is not None and record.status == "active":
                return loader.hosts.get(name)
        return None

    async def restore_active(self) -> None:
        async with self._lifecycle_lock:
            for name in sorted(self._restore_pending):
                record = self.records[name]
                manifest = record.manifest
                path = Path(record.install_path)
                loader = self._loader(manifest.type)
                try:
                    await loader.load(manifest, path)
                    await loader.activate(manifest, path)
                except Exception as exc:
                    await self._deactivate_loader(loader, manifest, path, force=True)
                    record.status = "quarantined"
                    log_event(log, 30, "plugin.restore.failed", plugin=name, error_type=type(exc).__name__)
                else:
                    record.status = "active"
                self._restore_pending.remove(name)
                record.updated_at = _now()
                self._save_registry()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            for name, record in self.records.items():
                if record.status == "active":
                    await self._deactivate_loader(self._loader(record.manifest.type), record.manifest, Path(record.install_path), force=True)
                    record.status = "installed"
                    self._restore_pending.add(name)

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
                _safe_resolve(candidate, root)
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
                subprocess.run(
                    ["git", "-c", "http.followRedirects=false", "clone", "--depth", "1", "--", url, str(checkout)],
                    check=True, capture_output=True, timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                shutil.rmtree(checkout, ignore_errors=True)
                raise PluginValidationError("git 插件源获取失败", detail={"source": raw}) from exc
            candidates = [checkout]
            candidates.extend(sorted(path for path in checkout.iterdir() if path.is_dir() and path.name != ".git" and (path / "manifest.yaml").exists()))
            for candidate in candidates:
                if (candidate / "manifest.yaml").exists():
                    return candidate, lambda: shutil.rmtree(checkout, ignore_errors=True)
            shutil.rmtree(checkout, ignore_errors=True)
            raise PluginValidationError("git 插件源缺少 manifest.yaml", detail={"source": raw})
        raise PluginValidationError("安装源不存在或格式不支持", detail={"source": raw})

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
            # F8：解压前按 ZipInfo.file_size 预检体积，防 zip bomb 打爆内核内存（非主题包此前无上限）
            _MAX_MEMBER = 32 * 1024 * 1024
            _MAX_TOTAL = 128 * 1024 * 1024
            sizes = {name: int(info.file_size) for name, info in members.items()}
            oversized = [n for n, s in sizes.items() if s > _MAX_MEMBER]
            if oversized:
                raise PluginValidationError("插件包单文件解压体积超限", detail={"limit_bytes": _MAX_MEMBER, "files": oversized[:5]})
            total = sum(sizes.values()) + len(raw_manifest)
            if total > _MAX_TOTAL:
                raise PluginValidationError("插件包解压总体积超限", detail={"limit_bytes": _MAX_TOTAL, "total_bytes": total})
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
            relative_path = path.relative_to(source)
            if relative_path.parts[0] == ".git":
                continue
            if path.is_symlink():
                raise PluginValidationError("插件目录禁止符号链接", detail={"path": str(relative_path)})
            if path.is_dir():
                continue
            name = _safe_member_name(relative_path.as_posix())
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

    def _validated_source(self, source: str | Path, *, no_verify: bool) -> tuple[PluginManifest, str, dict[str, bytes], list[dict[str, Any]]]:
        if no_verify and not self.security.allow_unverified:
            raise PluginPolicyError("当前模式禁止跳过插件签名校验", detail={"violation": "signature_bypass"})
        package, cleanup = self._resolve_source(source)
        try:
            reader = self._read_directory if package.is_dir() else self._read_package
            manifest, digest, entries, _ = reader(package, verify_signature=not no_verify)
            scan = _static_scan(entries)
            # SBX-02：危险 exec 调用（os.system/subprocess/eval/exec/ctypes）拒绝；出网导入告警需 allow_hosts 声明
            violations = [f for f in scan if f["kind"] == "exec_danger"]
            if violations:
                raise PluginPolicyError("插件含危险调用，静态扫描拒绝", detail={"violation": "static_scan.exec", "findings": violations})
            return manifest, digest, entries, scan
        finally:
            cleanup()

    def validate(self, source: str | Path, *, no_verify: bool = False) -> dict[str, Any]:
        manifest, digest, entries, scan = self._validated_source(source, no_verify=no_verify)
        return {**manifest.to_info(status="validated", package_sha256=digest, install_path=""), "files": sorted(entries), "dependencies": list(manifest.dependencies), "static_scan": scan}

    async def install(self, source: str | Path, *, no_verify: bool = False) -> dict[str, Any]:
        manifest, digest, entries, scan = await asyncio.to_thread(self._validated_source, source, no_verify=no_verify)
        async with self._lifecycle_lock:
            result = await self._install(str(source), manifest, digest, entries)
        if scan:
            log_event(log, 30, "plugin.static_scan.warning", plugin=manifest.name, findings=scan)
        return result

    async def _install(self, source: str, manifest: PluginManifest, digest: str, entries: Mapping[str, bytes]) -> dict[str, Any]:
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
            record = PluginRecord(manifest, "installed", digest, str(target), source, _now(), _now())
            self.records[manifest.name] = record
            try:
                self._save_registry()
                await loader.activate(manifest, target)
                record.status = "active"
                record.updated_at = _now()
                self._save_registry()
            except (Exception, asyncio.CancelledError) as exc:
                await self._deactivate_loader(loader, manifest, target, force=True)
                self.records.pop(manifest.name, None)
                shutil.rmtree(target, ignore_errors=True)
                _remove_empty_directory(target.parent)
                self._save_registry()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, PluginError):
                    raise
                raise PluginError("UH-KRN-2010", "插件 activate 失败，已回滚", status_code=400, detail={"plugin": manifest.name, "stage": "activate"}) from exc
            log_event(log, 20, "plugin.install", plugin=manifest.name, version=manifest.version, plugin_type=manifest.type)
            return record.info()
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if staging_parent.exists() and not any(staging_parent.iterdir()):
                staging_parent.rmdir()

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
        async with self._lifecycle_lock:
            return await self._deactivate(name)

    async def _deactivate(self, name: str) -> dict[str, Any]:
        record = self.records.get(name)
        if record is None:
            raise PluginValidationError("插件不存在", detail={"plugin": name})
        if name in self._restore_pending:
            self._restore_pending.remove(name)
            self._save_registry()
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
        async with self._lifecycle_lock:
            return await self._uninstall(name)

    async def _uninstall(self, name: str) -> dict[str, Any]:
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
            self._restore_pending.discard(name)
            self.records.pop(name, None)
            self._save_registry()
        if path.exists():
            raise PluginError("UH-KRN-2010", "插件卸载后仍有残留文件", status_code=500, detail={"plugin": name, "violation": "residue"})
        log_event(log, 20, "plugin.uninstall", plugin=name)
        return {"name": record.manifest.name, "version": record.manifest.version, "status": "uninstalled"}

    async def update_config(self, name: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
        async with self._lifecycle_lock:
            return self._update_config(name, config)

    def _update_config(self, name: str, config: Mapping[str, Any] | None) -> dict[str, Any]:
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
