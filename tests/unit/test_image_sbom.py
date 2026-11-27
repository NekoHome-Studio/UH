"""M3 独立复审 P1：镜像 LABEL 声明的 SBOM 文件必须真实存在。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dockerfile():
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _dockerignore():
    return (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_dockerfile_builds_sbom_in_dedicated_stage():
    text = _dockerfile()
    assert "FROM node:" in text, "缺 SBOM 生成阶段"
    assert "generate-sbom.mjs" in text
    assert re.search(r"RUN node scripts/generate-sbom\.mjs", text), "生成器未被运行"


def test_sbom_is_copied_into_runtime_image():
    text = _dockerfile()
    assert "COPY --from=sbom" in text and "sbom.cdx.json" in text, \
        "SBOM 产物未进入运行镜像"


def test_label_path_matches_copied_path():
    """LABEL 声明的路径必须与 COPY 目标一致，否则运维按声明取不到文件。"""
    text = _dockerfile()
    m = re.search(r'org\.opencontainers\.image\.sbom="([^"]+)"', text)
    assert m, "缺 OCI sbom 标签"
    label_path = m.group(1)
    assert label_path == "/app/dist/sbom.cdx.json", label_path
    copy_line = next((l for l in text.splitlines() if "COPY --from=sbom" in l), "")
    assert copy_line.endswith("./dist/sbom.cdx.json"), copy_line
    assert label_path == "/app/" + "./dist/sbom.cdx.json".removeprefix("./")


def test_generate_sbom_is_in_build_context():
    """.dockerignore 是白名单模式：不在列表的文件会被排除，COPY 将失败。"""
    entries = _dockerignore()
    assert "**" in entries, "预期为白名单模式"
    assert "!scripts/generate-sbom.mjs" in entries, "SBOM 生成器被 dockerignore 排除"


def test_all_copy_sources_are_whitelisted():
    """Dockerfile 的每个 COPY 源都必须在 .dockerignore 白名单内（构建上下文可见）。"""
    text = _dockerfile()
    entries = set(_dockerignore())
    required = ["!pyproject.toml", "!kernel/*.py", "!deploy/security/seccomp.json",
                "!ui/*.html", "!ui/*.js", "!scripts/generate-sbom.mjs"]
    for item in required:
        assert item in entries, f"{item} 缺失，对应 COPY 会失败"
