# 运行手册：M0 内核服务器部署

> 靶机：Win10 工控机 DESKTOP-AOI7835（192.168.0.28，SSH 别名 ipc，账户 bqj10）
> 项目路径：D:\uh · 服务：uvicorn kernel.main:app · 端口 8080（0.0.0.0）

## 一键部署（本机执行）

```bash
# 1. 传源码（服务器无 git，用 scp）
ssh ipc "powershell -NoProfile -Command New-Item -ItemType Directory -Force -Path D:\uh\kernel,D:\uh\tests\unit"
scp kernel/*.py ipc:D:/uh/kernel/
scp tests/unit/*.py ipc:D:/uh/tests/unit/
scp pyproject.toml scripts/server-deploy.ps1 ipc:D:/uh/
# 2. 部署 + 启动（venv/pip/pytest/防火墙/WMI 常驻）
ssh ipc "powershell -NoProfile -ExecutionPolicy Bypass -File D:\uh\server-deploy.ps1"
# 3. 跨机验证
curl -i http://192.168.0.28:8080/health
curl -i http://192.168.0.28:8080/ready
```

## 两个关键坑（已固化进脚本）

1. **pip 代理**：服务器 WinINET 注册表配了失效代理，pip/requests 自动探测导致 ProxyError。--isolated / --proxy "" 都挡不住；必须 NO_PROXY=* 强制直连 + 清华镜像。
2. **进程常驻**：Windows OpenSSH 会话结束会杀子进程，Start-Process 启动的 uvicorn 随会话退出。改用 WMI Win32_Process.Create（进程挂 WmiPrvSE，独立于 SSH 会话，以 bqj10 用户运行，非 SYSTEM）。

## 运维

- 日志：D:\uh\kernel.out.log（JSON 业务日志）/ kernel.err.log（uvicorn 运行日志）
- 重启：重跑 server-deploy.ps1（幂等：先杀旧 uvicorn 再起）
- 生产化（M3）：改用专用服务账户 + 非 root 原则（当前 bqj10 为开发测试身份）

## 验证记录（2026-11-15）

- 服务器单测 15/15（Python 3.12.10）
- 跨机 health/ready 200，X-Trace-Id 自动生成；外部 X-Trace-Id 正确回显
- 日志 request.received / request.finished 同 trace_id 串联，latency_ms 记录
