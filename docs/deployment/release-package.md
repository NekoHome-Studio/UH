# v0.1.0 发布包

> 构建：`python scripts/build-release.py --key-file <PEM>`
> 产物目录：`dist/release/`（gitignored，随 GitHub Release 附件分发）

## 1. 产物清单

| 文件 | 说明 |
|---|---|
| `audit-log.uhpkg` | 审计日志钩子（type=hook） |
| `db-query.uhpkg` | 数据库查询工具（type=tool） |
| `file.uhpkg` | 文件读写工具（type=tool） |
| `theme-light.uhpkg` | 浅色主题（type=theme） |
| `web-search.uhpkg` | 网页搜索工具（type=tool） |
| `trusted-keys.json` | **公钥**（key_id → base64 raw），供安装方配置信任根 |
| `release-manifest.json` | 包清单：名称 / 字节数 / SHA-256 / 摘要 / key_id |
| `sbom.cdx.json` | CycloneDX 1.5 SBOM（随包发布，R-3 验收要求） |

**私钥不入产物、不入仓库**。经 `--key-file` 或 `UH_RELEASE_KEY_FILE` 传入。

## 2. 签名机制

- 算法：**Ed25519**
- 签名对象：排除 `SIGNATURE` 后按路径排序的规范化包体 SHA-256 摘要
  （`_canonical_digest`：4 字节名长 + 名 + 8 字节内容长 + 内容）
- **关键**：内核 `_verify_signature` 用 `bytes.fromhex(digest)` 验签，
  故签名必须对**原始摘要字节**而非 hex 字符串（首次实现踩此坑，5 包全部校验失败）
- `SIGNATURE` 为 JSON：`{algorithm, key_id, digest, signature}`
- **公钥不随包信任**：必须由安装方经 `PluginSecurityConfig.trusted_keys` 提供，
  避免插件自带公钥自建信任根（已有测试断言未配置信任根时拒绝安装）

## 3. 安装验证（实测 5/5 通过）

```bash
# CLI
export UH_PLUGIN_TRUSTED_KEYS_FILE=dist/release/trusted-keys.json
uh plugin install dist/release/web-search.uhpkg
uh plugin list
```

```python
# Python API
import asyncio, json
from kernel.plugins import PluginDriver, PluginSecurityConfig
rel = json.load(open('dist/release/release-manifest.json', encoding='utf-8'))
keys = {rel['key_id']: rel['public_key_base64']}
async def main():
    d = PluginDriver(root='.uh/plugins', security=PluginSecurityConfig(trusted_keys=keys))
    for p in rel['packages']:
        await d.install('dist/release/' + p['file'])
    print(d.list())
    await d.shutdown()
asyncio.run(main())
```

**实测**：`installed=5/5 listed=5`，类型分布 hook×1 / tool×3 / theme×1。

## 4. 校验包完整性

```python
import hashlib, json
from pathlib import Path
rel = json.load(open('dist/release/release-manifest.json', encoding='utf-8'))
for p in rel['packages']:
    actual = hashlib.sha256(Path('dist/release', p['file']).read_bytes()).hexdigest()
    assert actual == p['sha256'], p['name']
    print('OK', p['name'])
```

## 5. 容器镜像

`Dockerfile` 已就绪（多阶段、非 root 10001、HEALTHCHECK、bubblewrap、OCI SBOM 标签）。

```bash
# 单架构
docker build -t ultraharness/kernel:0.1.0 .

# 多架构（amd64 + arm64，R-3 要求）
docker buildx create --use --name uh-builder
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ultraharness/kernel:0.1.0 -t ultraharness/kernel:latest --push .
```

> **本机未构建镜像**（无 Docker 环境）。多架构构建需在具备 buildx 的机器执行；
> 命令与 Dockerfile 已就绪，如实标注为待执行项。

## 6. 密钥管理

| 项 | 位置 | 入仓库 |
|---|---|---|
| 签名私钥 | `--key-file` 指定 | ❌ **严禁** |
| 公钥 | `trusted-keys.json` + `release-manifest.json` | ✅ 随 Release 分发 |
| key_id | `uh-official-2026`（`--key-id` 可覆盖） | ✅ |

首次生成：`python scripts/build-release.py --generate-key --key-file <安全路径>`
（脚本尝试 `chmod 600`）。**私钥离线保管并备份**，丢失后无法再签同 key_id 的包。

## 7. 已知限制
- 多架构镜像未实际构建推送（缺 Docker/buildx 环境）
- Desktop Tauri 二进制未编译（见 `desktop/README.md`）
- 发布包为源码级 `.uhpkg`，不含预编译 wheel
