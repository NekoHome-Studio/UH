"""Validation and display metadata for data-only theme packages."""
from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote

if TYPE_CHECKING:
    from .plugins import PluginDriver, PluginRecord


_VARIABLE = re.compile(r"--uh-[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_FONT = re.compile(r"[\w][\w -]{0,63}")
_LENGTH = r"(?:0|(?:\d{1,3}(?:\.\d{1,3})?|\.\d{1,3})(?:px|rem|em|%))"
_VALUE = re.compile(
    r"(?:#[0-9a-f]{3}(?:[0-9a-f]{1}|[0-9a-f]{3}|[0-9a-f]{5})?"
    r"|[a-z][a-z-]{0,31}|" + _LENGTH + r"(?: " + _LENGTH + r"){0,3})", re.ASCII | re.IGNORECASE,
)
_MAX_ASSET_BYTES = 2 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def validate_theme_config(config: Mapping[str, Any]) -> None:
    variables = config.get("css_variables")
    if not isinstance(variables, dict) or not 1 <= len(variables) <= 256:
        raise ValueError("主题须声明 1 至 256 个 CSS 变量")
    for name, value in variables.items():
        if not isinstance(name, str) or len(name) > 80 or not _VARIABLE.fullmatch(name):
            raise ValueError("主题变量名须符合 --uh-* 格式")
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise ValueError(f"主题变量值非法：{name}")
        if name in {"--uh-radius", "--uh-spacing"} and not re.fullmatch(_LENGTH, value):
            raise ValueError(f"主题变量须为单一长度：{name}")
    fonts = config.get("font_stack", [])
    if not isinstance(fonts, list) or len(fonts) > 8 or any(
        not isinstance(font, str) or font != font.strip() or not _FONT.fullmatch(font) for font in fonts
    ):
        raise ValueError("主题字体最多 8 个，只允许字母、数字、空格、下划线和连字符")


def _valid_png(content: bytes) -> bool:
    if not content.startswith(_PNG_MAGIC) or len(content) < 45:
        return False
    width, height = struct.unpack_from(">II", content, 16)
    if content[8:16] != b"\0\0\0\rIHDR" or not (0 < width <= 2048 and 0 < height <= 2048):
        return False
    offset, has_data = 8, False
    while offset + 12 <= len(content):
        size, kind = struct.unpack_from(">I4s", content, offset)
        end = offset + 12 + size
        if end > len(content):
            return False
        if zlib.crc32(content[offset + 4:end - 4]) != int.from_bytes(content[end - 4:end], "big"):
            return False
        if kind == b"IEND":
            return size == 0 and end == len(content) and has_data
        has_data |= kind == b"IDAT" and size > 0
        offset = end
    return False


def theme_asset_type(name: str, content: bytes, fonts: list[str]) -> str:
    if not 0 < len(content) <= _MAX_ASSET_BYTES:
        raise ValueError("单个主题资源不得超过 2 MiB")
    if name == "assets/preview.png" and _valid_png(content):
        return "image/png"
    for family in fonts:
        if name == f"assets/fonts/{family}.woff2" and len(content) > 48:
            magic, _, length, tables, reserved = struct.unpack_from(">4sIIHH", content)
            if magic == b"wOF2" and length == len(content) and tables and reserved == 0:
                return "font/woff2"
    raise ValueError("主题只允许包内 PNG 预览和与 font_stack 对应的 WOFF2 字体")


def validate_theme_asset_sizes(sizes: Mapping[str, int]) -> None:
    if len(sizes) > 9 or sum(sizes.values()) > 8 * 1024 * 1024:
        raise ValueError("主题资源总大小不得超过 8 MiB")
    if any(not 0 < size <= _MAX_ASSET_BYTES for size in sizes.values()):
        raise ValueError("单个主题资源不得超过 2 MiB")


def validate_theme_package(config: Mapping[str, Any], assets: Mapping[str, bytes]) -> None:
    validate_theme_config(config)
    for name, data in assets.items():
        theme_asset_type(name, data, config.get("font_stack", []))


def theme_asset(record: PluginRecord, name: str) -> tuple[bytes, str]:
    fonts = record.manifest.config.get("font_stack", [])
    if name != "assets/preview.png" and name not in {f"assets/fonts/{family}.woff2" for family in fonts}:
        raise ValueError("未知主题资源")
    root = Path(record.install_path).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("主题资源路径越界")
    with path.open("rb") as handle:
        content = handle.read(_MAX_ASSET_BYTES + 1)
    return content, theme_asset_type(name, content, fonts)


def installed_theme(driver: PluginDriver, name: str) -> PluginRecord:
    record = driver.records.get(name)
    if record is None or record.manifest.type != "theme" or record.status == "quarantined":
        raise KeyError(name)
    validate_theme_config(record.manifest.config)
    return record


def _selection_path(driver: PluginDriver) -> Path:
    return driver.registry_path.with_suffix(".theme.json")


def _read_selection(driver: PluginDriver) -> str | None:
    try:
        active = json.loads(_selection_path(driver).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return active if isinstance(active, str) else None


def theme_catalog(driver: PluginDriver) -> dict:
    active = _read_selection(driver)
    themes = []
    for name in sorted(driver.records):
        try:
            record = installed_theme(driver, name)
        except (KeyError, ValueError):
            continue
        config = record.manifest.config
        files = [
            {"family": family, "path": f"/api/v1/themes/{name}/assets/fonts/{quote(family)}.woff2"}
            for family in config.get("font_stack", [])
            if (Path(record.install_path) / f"assets/fonts/{family}.woff2").is_file()
        ]
        themes.append({
            "name": name, "version": record.manifest.version, "css_variables": config["css_variables"],
            "font_stack": config.get("font_stack", []), "fonts": files,
        })
    return {"active": active if any(theme["name"] == active for theme in themes) else None, "themes": themes}


def select_theme(driver: PluginDriver, name: str | None) -> None:
    if name is not None:
        installed_theme(driver, name)
    path = _selection_path(driver)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="theme-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(name, handle)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def clear_uninstalled_theme(driver: PluginDriver, name: str) -> None:
    if name not in driver.records and _read_selection(driver) == name:
        _selection_path(driver).unlink(missing_ok=True)
