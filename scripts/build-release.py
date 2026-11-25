"""R-3 发布打包：官方插件 .uhpkg + Ed25519 签名 + SBOM + 校验清单。

用法：
  python scripts/build-release.py --key-file <ed25519私钥PEM> --key-id uh-official-2026
  python scripts/build-release.py --generate-key   # 生成发布密钥（首次）

密钥严禁入仓库：私钥路径经参数/环境变量传入，产物只含公钥与签名。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kernel.plugins import SIGNATURE_FILE, _canonical_digest

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def collect(plugin_dir: Path) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_dir).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        if path.suffix in SKIP_SUFFIX or rel == SIGNATURE_FILE:
            continue
        entries.append((rel, path.read_bytes()))
    return entries


def load_or_generate_key(key_file, generate):
    if generate:
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        out = Path(key_file or "release-signing-key.pem")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pem)
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print("KEY_GENERATED path=" + str(out) + " (KEEP SECRET, never commit)")
        return key, str(out)
    if not key_file or not Path(key_file).exists():
        raise SystemExit("需要 --key-file <PEM> 或 --generate-key")
    key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("签名密钥必须是 Ed25519")
    return key, key_file


def build_package(plugin_dir: Path, out_dir: Path, key, key_id: str) -> dict:
    entries = collect(plugin_dir)
    if not any(name == "manifest.yaml" for name, _ in entries):
        raise SystemExit(plugin_dir.name + ": 缺 manifest.yaml")
    digest = _canonical_digest(entries)
    # 内核 _verify_signature 用 bytes.fromhex(digest) 验签，签名对象必须是原始摘要字节
    signature = key.sign(bytes.fromhex(digest))
    sig_doc = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "digest": digest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg_path = out_dir / (plugin_dir.name + ".uhpkg")
    with zipfile.ZipFile(pkg_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
        zf.writestr(SIGNATURE_FILE, json.dumps(sig_doc, ensure_ascii=False, indent=2))
    return {
        "name": plugin_dir.name,
        "file": pkg_path.name,
        "bytes": pkg_path.stat().st_size,
        "sha256": hashlib.sha256(pkg_path.read_bytes()).hexdigest(),
        "digest": digest,
        "key_id": key_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="UH! 发布打包")
    ap.add_argument("--key-file", default=os.environ.get("UH_RELEASE_KEY_FILE"))
    ap.add_argument("--generate-key", action="store_true")
    ap.add_argument("--key-id", default=os.environ.get("UH_RELEASE_KEY_ID", "uh-official-2026"))
    ap.add_argument("--out-dir", default="dist/release")
    args = ap.parse_args()

    key, key_path = load_or_generate_key(args.key_file, args.generate_key)
    out_dir = ROOT / args.out_dir
    official = ROOT / "examples" / "plugins" / "official"
    packages = []
    for plugin_dir in sorted(p for p in official.iterdir() if p.is_dir()):
        info = build_package(plugin_dir, out_dir, key, args.key_id)
        packages.append(info)
        print("PACKED " + info["name"] + " bytes=" + str(info["bytes"]) + " sha256=" + info["sha256"][:16])

    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    (out_dir / "trusted-keys.json").write_text(
        json.dumps({args.key_id: pub_b64}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    sbom_src = ROOT / "dist" / "sbom.cdx.json"
    if sbom_src.exists():
        (out_dir / "sbom.cdx.json").write_bytes(sbom_src.read_bytes())

    manifest = {
        "release": "v0.1.0",
        "key_id": args.key_id,
        "public_key_base64": pub_b64,
        "packages": packages,
        "sbom": "sbom.cdx.json" if sbom_src.exists() else None,
    }
    (out_dir / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("RELEASE_BUILT packages=" + str(len(packages)) + " out=" + args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
