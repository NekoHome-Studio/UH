# Brief 0016：M3-1 Docker Compose 编排 + 暴露面收紧（#28）

## 角色与约定
你是实现者（Codex），在 UH! 仓库工作，先读 AGENTS.md。只改/加代码与测试，**不要 git commit**（DSH 审后提交）。
**MS-3 已结项**（M2 插件期完成，tag m2-v0.1.0，265 测试双端 full 8/8）。本任务开启 **M3 生产期**。

## 硬约束（踩过的坑，务必遵守）
- **禁 pip install**（本机 C:\Python314 依赖已齐）；禁写 `$null` / `%VAR%` / `~` 等未展开路径（曾在仓库造垃圾目录）。
- **密钥严禁明文入仓库**：一律 ${ENV} 引用 + .env.example 占位（AGENTS.md §4）。secret-scan 门禁会扫。
- **契约冻结只增不改**：docs/contracts/deploy-hardening.md 是事实源，按它实装，不改它。
- 不得削弱现有 265 测试。

## 必读上下文
- **docs/contracts/deploy-hardening.md §1**（Compose 暴露面契约决议 E1 + 修正后骨架 YAML，第 19-57 行）——这是实装基线，逐字对照。
- uh-issues/issues/M3-1.md（#28 任务与验收）
- docs/contracts/sandbox.md（插件沙箱，热安装相关）；kernel/plugins.py（PluginDriver）；kernel/cli.py（uh plugin 子命令）
- pyproject.toml（依赖）；kernel/main.py（env 读取：UH_LLM_API_KEY/UH_REDIS_URL/UH_CHECKPOINT_POSTGRES_URL/UH_RETRIEVAL_CHROMA 等，**注意内核实际用 UH_ 前缀**）

## 需求
1. **docker-compose.yaml**（仓库根）：按 deploy-hardening §1 骨架实装 7 服务——uh-kernel / uh-llama-index / uh-langgraph / uh-crewai / redis / chroma / postgres。
   - **移除 version 字段**（决议 E6，Compose v2 已废弃）。
   - uh-kernel `ports: ["8080:8080"]` 唯一对外；llama-index/langgraph/crewai 仅 `expose: ["8080"]`；**redis/chroma 完全移除 ports**；postgres 无 ports。
   - uh-crewai `profiles: ["collaborate"]` 默认不启动。
   - depends_on / volumes（chroma-data / pg-data）按骨架。
2. **环境变量与密钥引用**：全部 `${VAR}` 形式，**不落明文**。
   - 核对 kernel 实际读取的 env 名（UH_ 前缀，如 UH_LLM_API_KEY / UH_REDIS_URL / UH_CHECKPOINT_BACKEND / UH_CHECKPOINT_POSTGRES_URL / UH_RETRIEVAL_BACKEND / UH_RETRIEVAL_CHROMA / UH_AUTH_TOKEN_FILE / UH_MEMORY_MASTER_KEY / UH_CLIENT_ID_SECRET / UH_PLUGIN_TRUSTED_KEYS_FILE），compose 里正确接线到容器内服务名（redis://redis:6379、chroma http://chroma:8000、postgres DSN）。
   - 提供 **.env.example**（占位值，不含真密钥）+ 文档说明如何注入（.env / docker secret）。
   - 生产鉴权：UH_LOCAL_MODE=0 / UH_AUTH_REQUIRED=1；token 经文件挂载（UH_AUTH_TOKEN_FILE），**不进命令行/镜像**。
3. **Dockerfile**（kernel 镜像）：多阶段构建、非 root 用户运行（为 M3-7 安全加固铺垫）、只装必要依赖、健康检查 HEALTHCHECK 打 /health。
4. **插件热安装 docker exec 可用**：验证 `docker exec <kernel> uh plugin install <path>` 路径可行——镜像内需含 CLI 入口与插件根目录（UH_PLUGIN_ROOT 指向持久卷），文档给出命令示例。
5. **暴露面自动化校验脚本**：scripts/verify-compose-exposure.mjs（或 .ps1）——解析 docker-compose.yaml，断言：仅 uh-kernel 有 ports 且为 8080；其余服务无 ports；crewai 有 profiles collaborate；无 version 字段；无明文密钥（正则扫 password/token/key 的字面值）。接入 verify-all（新增一项，保持既有 8 项不变）。
6. **测试**：tests/unit/test_compose.py（或 scripts 侧 node 测试）覆盖上述断言 + .env.example 键齐全 + Dockerfile 非 root/HEALTHCHECK 存在。**不需要真跑 docker**（本机可能无 Docker），做静态契约校验即可；若本机有 docker 可选跑 `docker compose config` 验证语法。

## 验收标准（#28）
- 默认 compose up 只起必要服务（crewai 因 profiles 不启）。
- 外网仅暴露 8080（静态断言 + 若有 docker 则 docker port 核对）。
- 插件热安装 docker exec 路径可用（镜像含 CLI + 持久插件根 + 文档命令）。

## 门禁（必须跑并报告真实数字）
- `python -m pytest -q`（期望 ≥265 全绿，含你新增的）
- `node scripts/verify-all.mjs --full`（期望全绿，含新增 compose 暴露面项）
- `node scripts/secret-scan.mjs`（rc=0）

## 报告契约
输出：新增/改动文件清单 + 设计要点（暴露面/密钥注入/非 root/热安装路径）+ 三项门禁真实数字 + 各验收如何验证 + 未能实测的部分（如无 Docker）如实标注。**不要提交**。
