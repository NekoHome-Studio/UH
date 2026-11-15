# UH! 内核服务器部署（Windows / OpenSSH）
# 关键坑：1) pip 走系统注册表代理 → 必须 NO_PROXY=* + 国内镜像
#         2) SSH 会话结束会杀子进程 → 用 WMI Win32_Process.Create 脱离会话常驻
param([string]$Root = "D:\uh", [int]$Port = 8080)
$ErrorActionPreference = "Stop"
Set-Location $Root
$env:NO_PROXY = "*"; $env:no_proxy = "*"; $env:HTTP_PROXY = ""; $env:HTTPS_PROXY = ""
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv .venv }
& $py -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --quiet --disable-pip-version-check --retries 2 --timeout 30 fastapi uvicorn pytest httpx
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
netsh advfirewall firewall add rule name="UH Kernel $Port" dir=in action=allow protocol=TCP localport=$Port | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn*kernel.main*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
$bt = [char]96
$inner = "cd /d $Root && $py -m uvicorn kernel.main:app --host 0.0.0.0 --port $Port >> $Root\kernel.out.log 2>> $Root\kernel.err.log"
$cmd = "cmd.exe /c " + $bt + '"' + $inner + $bt + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $Root }
Write-Output ("CREATE_RC=" + $r.ReturnValue + " PID=" + $r.ProcessId)
Start-Sleep -Seconds 5
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
Write-Output ("LISTEN=" + ($conn | Measure-Object).Count)
