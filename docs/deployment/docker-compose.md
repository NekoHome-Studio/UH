# Docker Compose 部署

使用 Docker Compose v2 和 Linux 容器。内核镜像由本仓库的 `Dockerfile` 构建。`ultra-harness/llama-index:0.1.0`、`ultra-harness/langgraph:0.1.0` 和启用协作时的 `ultra-harness/crewai:0.1.0` 需由部署方预先提供为本地镜像，或从已配置的镜像仓库获取。

## 注入配置与密钥

1. 将 `.env.example` 复制为 `.env`，通过受控文件或当前部署进程的环境注入 `DB_PASSWORD`、`UH_CLIENT_ID_SECRET` 和 `UH_MEMORY_MASTER_KEY`。`.env` 中包含 `$` 的字面值用单引号包裹。按模型服务配置 `UH_LLM_PROVIDER`、`UH_LLM_BASE_URL`、`UH_LLM_MODEL` 和 `UH_LLM_API_KEY`。示例启用 `mock`。
2. 在检出目录之外创建鉴权 token 文件，内容为单行 bearer token。另建信任库 JSON 文件，内容从签名的 `key_id` 映射到对应的 Ed25519 公钥。暂时没有受信插件时可用 `{}`。将两个宿主机文件的路径分别填入 `UH_AUTH_TOKEN_SOURCE` 和 `UH_PLUGIN_TRUSTED_KEYS_SOURCE`。
3. Compose 将文件只读挂载到内核的 `/run/secrets/`。宿主文件权限须允许容器 UID/GID `10001:10001` 读取。容器启用 `UH_LOCAL_MODE=0` 和 `UH_AUTH_REQUIRED=1`，运行中的内核和 `uh plugin install` 通过 `UH_AUTH_TOKEN_FILE` 读取同一文件。

`.env` 由 Git 和镜像构建上下文排除。Redis、Chroma 和 PostgreSQL 通过 Compose 服务名访问。PostgreSQL 从 `DB_PASSWORD` 获得初始化密码，内核的 libpq 通过 `PGPASSWORD` 获得同一个值。Chroma 的 `CHROMA_PERSIST_PATH` 指向契约卷 `chroma-data` 的挂载路径。内核使用持久卷 `uh-state` 保存插件、注册表和本地运行状态。排查配置时用 `docker compose config --quiet`，避免打印展开后的环境变量。

## 启动与核对

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose port uh-kernel 8080
```

默认启动六个服务，宿主机仅发布内核 TCP `8080`。核对 `docker compose ps` 的 `PORTS` 列时，只有 `uh-kernel` 应显示 `8080` 的宿主机映射。`GET /health` 供 Docker 健康检查使用。需要协作能力时执行 `docker compose --profile collaborate up -d`。

## 安装签名插件

先把与信任库中公钥匹配的 `.uhpkg` 复制到内核容器，再执行安装。

```sh
docker compose cp ./my-plugin.uhpkg uh-kernel:/tmp/my-plugin.uhpkg
docker compose exec -T uh-kernel uh plugin install /tmp/my-plugin.uhpkg
```

也可以用 `docker exec <kernel-container> uh plugin install /tmp/my-plugin.uhpkg`。安装请求由容器内的 `127.0.0.1:8080` 内核处理，成功时返回 `status: active`。插件文件和状态保存在 `/app/.uh` 卷。执行子进程的插件由现有 Linux 沙箱启动，部署节点及容器运行时需支持 bubblewrap 所需的用户命名空间。
