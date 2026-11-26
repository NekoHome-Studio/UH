"""R2 P1-3 回归：secret-scan 在无 .git 的导出/发布树中不得 fail-open。

扫描文件数为 0 时必须非零退出并报错，而非打印
`SECRET_SCAN_PASSED (0 text files, 0 hits)` 以 0 退出（等于没扫）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN = ROOT / "scripts" / "secret-scan.mjs"


def _import_scan(tmp_path):
    """把 secret-scan.mjs 复制到无 .git 的临时目录并 import 其导出函数。"""
    target_dir = tmp_path / "scan-pkg"
    target_dir.mkdir()
    shutil.copyfile(SCAN, target_dir / "secret-scan.mjs")
    script = (
        "import {scanTrackedFiles} from './secret-scan.mjs';"
        "console.log(JSON.stringify(scanTrackedFiles(process.argv[2])));"
    )
    r = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(tmp_path)],
        cwd=target_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


def test_scan_tracked_files_returns_empty_without_git(tmp_path):
    """无 .git 目录：不抛异常，返回空文件列表（不误当作 pass）。"""
    assert _import_scan(tmp_path) == []


def test_cli_exits_nonzero_without_git(tmp_path):
    """无 .git：主入口必须非零退出并报错，禁止打印 SECRET_SCAN_PASSED。"""
    target_dir = tmp_path / "scan-pkg"
    target_dir.mkdir()
    shutil.copyfile(SCAN, target_dir / "secret-scan.mjs")
    # root = 脚本所在目录的父目录 = tmp_path（无 .git）
    r = subprocess.run(
        ["node", "secret-scan.mjs"],
        cwd=target_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert "SECRET_SCAN_FAILED" in (r.stdout + r.stderr)
    assert "SECRET_SCAN_PASSED" not in (r.stdout + r.stderr)


def test_cli_passes_on_normal_git_repo():
    """有 .git 的正常仓库：保持 SECRET_SCAN_PASSED（不回退为空文件）。"""
    r = subprocess.run(
        ["node", str(SCAN)], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SECRET_SCAN_PASSED" in r.stdout
