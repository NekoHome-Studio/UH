# M2-1 插件驱动实现对照

> 状态：待评审。该文件记录 M2-1 的实现约定，待内核负责人评审后再冻结为新增契约编号。

## 1. 包体与摘要

`.uhpkg` 是 ZIP 包，根目录必须含 `manifest.yaml`。解包前拒绝绝对路径、`..` 路径、反斜杠路径、重复路径和符号链接。包体摘要排除 `SIGNATURE`，按规范化 POSIX 路径排序后写入路径长度、路径、内容长度和内容，计算 SHA-256。

## 2. 签名

`SIGNATURE` 使用 JSON：

```json
{
  "algorithm": "ed25519",
  "key_id": "uh-official-2026",
  "digest": "<sha256 hex>",
  "signature": "<base64>"
}
```

公钥由部署配置的受信 key ring 提供，包体不能自带信任根。运行时从 `UH_PLUGIN_TRUSTED_KEYS_FILE` 读取 JSON 映射 `key_id -> 公钥（base64/hex）`。`no_verify` 仅在本地模式且显式开启 `UH_PLUGIN_ALLOW_UNVERIFIED=1` 时允许。

## 3. 生命周期

| 阶段 | 状态变化 | 失败行为 |
|---|---|---|
| install/load | 不存在 → installed | 删除 staging 与目标目录，不写注册表 |
| activate | installed → active | 调用 deactivate/force_kill，删除目录与注册表记录 |
| execute | active → active | 由 M2-2 沙箱与后续 SDK 负责 |
| deactivate | active → installed | 5 秒内未 flush 则 force_kill，状态为 quarantined |
| uninstall | installed/active/quarantined → removed | 清理插件目录与注册表记录 |

重复安装同一摘要幂等返回现有记录。同名不同摘要拒绝安装。

## 4. 配置与兼容

部署应设置 `UH_CLIENT_ID_SECRET_FILE` 作为生产身份签名密钥。插件注册表默认位于 `.uh/plugins.json`，插件目录默认位于 `.uh/plugins`，均可由环境变量覆盖。

