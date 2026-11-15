# Codex connectivity helper: detect local proxy -> set HTTPS_PROXY -> live ping
# Usage: powershell -File scripts/codex-connect.ps1 [-ProxyHost 127.0.0.1] [-ProxyPort 7890]
param([string]$ProxyHost = "127.0.0.1", [int]$ProxyPort = 0)
$ErrorActionPreference = "SilentlyContinue"
if ($ProxyPort -eq 0) {
  $candidates = @(65532, 7890, 7891, 7897, 1080, 10809, 10808, 1087, 8118, 2080, 10800, 20171, 20170, 33210)
  foreach ($p in $candidates) {
    if (Get-NetTCPConnection -LocalPort $p -State Listen) { $ProxyPort = $p; break }
  }
}
if ($ProxyPort -eq 0) {
  Write-Output "NO_PROXY_LISTENING"
  Write-Output "Start your proxy tool, or pass -ProxyPort, or use CODEX_API_KEY relay."
  exit 2
}
$proxy = "http://" + $ProxyHost + ":" + $ProxyPort
Write-Output ("USING_PROXY=" + $proxy)
$env:HTTPS_PROXY = $proxy; $env:HTTP_PROXY = $proxy
$out = Join-Path $PSScriptRoot "..\.work\codex-connect.out"
codex exec --sandbox read-only -o $out "Reply with exactly the token CODEX_LINK_OK and nothing else." 2>&1 | Out-Null
if ((Test-Path $out) -and ((Get-Content $out -Raw) -match "CODEX_LINK_OK")) {
  Write-Output "CODEX_LINK_OK"
  exit 0
} else {
  Write-Output "CODEX_LINK_FAILED (proxy may not reach chatgpt.com; check proxy rules)"
  exit 1
}
