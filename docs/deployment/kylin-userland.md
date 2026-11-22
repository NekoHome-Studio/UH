# Kylin 跨网部署手册（无 root 用户态）

> 目标机：rsk-pc · 银河麒麟 Kylin Desktop V10 SP1（debian 系，x86_64，kernel 5.4.18）
> 接入：`60.7.194.147:13000`（SSH，用户 a）· 内网 192.168.0.125 · 8 核 / 30G RAM / home 698G
> 特点：**无 sudo/root**，全部用户态安装；github/pypi 首页可达但 **pypi 包源需清华镜像**

## 1. 环境限制与对策
| 限制 | 对策 |
|---|---|
| 用户 a 不在 sudoers | 全部装到 `$HOME`，不碰系统目录 |
| 系统 Python 3.8（内核用 `dict[str,Any]` 需 3.9+） | **uv 用户态装 CPython 3.12.16** |
| venv 缺 ensurepip | `get-pip.py` 引导（3.8 用 `/pip/3.8/get-pip.py`） |
| pypi 官方源不可达 | `~/.pip/pip.conf` 指清华镜像 |
| 公网只映射 13000(SSH) | 服务端口外部不可达，需 SSH 隧道或申请端口映射 |

## 2. 安装步骤（可复现）

### 2.1 传项目（SFTP）
本机用 paramiko SFTP 上传 `kernel/ ui/ docs/contracts/ scripts/ pyproject.toml` 到 `~/uh`（96 文件）。
```python
import paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('60.7.194.147', port=13000, username='a', password='<PASSWORD>')
sf = c.open_sftp()  # 递归 put，跳过 __pycache__/.git/*.pyc
```

### 2.2 pip 引导 + 镜像
```bash
mkdir -p ~/.pip
printf "[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\ntrusted-host = pypi.tuna.tsinghua.edu.cn\n" > ~/.pip/pip.conf
python3 -m venv ~/uh/.venv          # 3.8 临时 venv，仅用于装 uv
curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/pip/3.8/get-pip.py
~/uh/.venv/bin/python /tmp/get-pip.py -q
```

### 2.3 uv 装 Python 3.12（关键：绕过无 root）
```bash
~/uh/.venv/bin/python -m pip install -q uv
~/uh/.venv/bin/uv python install 3.12
# 装到 ~/.local/share/uv/python/cpython-3.12.16-linux-x86_64-gnu/bin/python3.12
```
**注意**：uv 装在旧 venv 里，删 venv 前先用它装好 Python；之后用**绝对路径**的 python3.12 重建 venv。

### 2.4 重建 venv + 依赖
```bash
PY=$HOME/.local/share/uv/python/cpython-3.12.16-linux-x86_64-gnu/bin/python3.12
cd ~/uh && rm -rf .venv && $PY -m venv .venv
.venv/bin/python -m pip install -q fastapi 'uvicorn[standard]' cryptography jsonschema pyyaml fakeredis httpx
.venv/bin/python -c 'import kernel.main'   # 验证导入
```

## 3. 启动服务
`~/uh/start-uh.sh`（已就位）：
```bash
cd $HOME/uh
export UH_LOCAL_MODE=0 UH_AUTH_REQUIRED=1
export UH_AUTH_TOKEN_FILE=$HOME/uh/.auth-token
[ -f .auth-token ] || openssl rand -base64 32 | tr -d '\n' > .auth-token
chmod 600 .auth-token
export UH_PUBLIC_BASE_URL=http://60.7.194.147:18080
export UH_RETRIEVAL_ROOT=$HOME/uh/data
setsid nohup .venv/bin/python -m uvicorn kernel.main:app \
  --host 0.0.0.0 --port 18080 --no-access-log --log-level warning \
  > logs/kernel.out.log 2> logs/kernel.err.log < /dev/null &
```
- **setsid nohup + </dev/null**：脱离 SSH 会话，断开不掉。
- token 经文件引用，不进命令行/仓库（AGENTS.md §4）。
- 日志 `~/uh/logs/`；`--no-access-log` 防 token 入访问日志。

## 4. API 调用规范（两个必知坑）

### 4.1 query token 必须 URL-encode
token 是 base64，可能含 `+` `/` `=`。**`+` 在 query 中会被解析成空格导致 401**。
```bash
# 正确
T=$(.venv/bin/python -c "import urllib.parse;print(urllib.parse.quote(open('.auth-token').read().strip(),safe=''))")
curl "$B/api/v1/themes?token=$T"
# 或用 Authorization 头（不受编码影响）
curl -H "Authorization: Bearer $(cat .auth-token)" $B/api/v1/themes
```
> 家里服务器 token 恰好无 `+`，此坑未暴露；Kylin token 含 `+` 才现形。**SDK 文档应写明**。

### 4.2 发消息须带 client_id（五审双因子）
建会话响应含 `client_id` 凭据（`uhcid1.<payload>.<sig>`，103 字符），后续请求必须携带：
```bash
SESS=$(curl -s -X POST "$B/api/v1/sessions?token=$T")
SID=$(echo $SESS | python -c "import sys,json;print(json.load(sys.stdin)['data']['session_id'])")
CID=$(echo $SESS | python -c "import sys,json;print(json.load(sys.stdin)['data']['client_id'])")
CIDE=$(python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$CID")
curl -X POST "$B/api/v1/sessions/$SID/messages?token=$T&client_id=$CIDE" \
  -H "Content-Type: application/json" -d '{"content":"hello","mode":"auto"}'
```
不带 client_id → `UH-USR-1002 需要有效的 client_id 身份凭据`。

## 5. 已验证链路（远端本机实测）
| 项 | 结果 |
|---|---|
| GET /health | 200 |
| GET /openapi.json | 200（豁免） |
| GET /api/v1/themes 无 token | 401 |
| POST /api/v1/sessions | 201 + client_id 凭据 |
| POST /sessions/{sid}/messages | 202 + task_id + stream_url |
| GET /tasks/{tid}/steps | 200，llm generate status=done |
| GET /api/v1/themes、/plugins 带凭据 | 200 |

`stream_url` 正确输出 `ws://60.7.194.147:18080/api/v1/stream?task_id=...`（UH_PUBLIC_BASE_URL 生效）。

## 6. 网络访问限制
- Kylin 内网 **192.168.0.125**（与家庭网段同号但**不同物理网络**）。
- 公网 `60.7.194.147` 是 NAT 出口，**仅映射 13000→SSH**。
- **18080 公网不可达**（Test-NetConnection False），验证走 SSH 进远端本机 curl。
- 外部访问方案：①申请端口映射 18080 ②SSH 隧道 `ssh -L 18080:127.0.0.1:18080 -p 13000 a@60.7.194.147`。

## 7. 运维速查
```bash
# 连接（密码见凭据管理，不入仓库）
ssh -p 13000 a@60.7.194.147
# 重启内核
bash ~/uh/start-uh.sh
# 看日志
tail -f ~/uh/logs/kernel.err.log
# 停服
pkill -f 'uvicorn kernel.main'
# 本机 API 冒烟
bash ~/uh/apitest.sh    # 或 cid.sh（带 client_id 完整链路）
```

## 8. 与家庭服务器对比
| | Kylin rsk-pc | 家庭 DESKTOP-AOI7835 |
|---|---|---|
| OS | 银河麒麟 V10 SP1（Linux） | Win10 22H2 |
| 资源 | 8核 / 30G / 698G | 4核 / 7.9G / 413G |
| 权限 | 普通用户无 root | Administrators |
| Python | 3.12.16（uv 用户态） | 3.12.10 venv |
| Docker | 未装（拟 rootless podman） | WSL2 docker-ce 29.10（**LxssManager 反复崩**） |
| 网络 | 直连 github，pypi 需镜像 | 需梯子 65532 |
| 稳定性 | **好** | WSL 频繁 StopPending 需重启 |

> **结论**：Kylin 适合长期跑内核本体与后续容器验收；家庭 WSL 仅作备用。
