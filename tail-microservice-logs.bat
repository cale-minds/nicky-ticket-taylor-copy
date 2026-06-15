@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "OUT_LOG=%ROOT%logs\uvicorn-real-nicky.out.log"
set "ERR_LOG=%ROOT%logs\uvicorn-real-nicky.err.log"
set "TUNNEL_LOG=%ROOT%logs\cloudflared-bat.log"

echo Abrindo logs do microservico em tempo real...
echo.
echo STDOUT/access log:
echo %OUT_LOG%
echo.
echo STDERR/stack traces:
echo %ERR_LOG%
echo.
echo Tunnel log:
echo %TUNNEL_LOG%
echo.

start "FastAPI stdout/access" powershell -NoExit -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%OUT_LOG%') { Get-Content '%OUT_LOG%' -Tail 120 -Wait } else { Write-Host 'Arquivo nao encontrado: %OUT_LOG%' }"
start "FastAPI stderr/errors" powershell -NoExit -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%ERR_LOG%') { Get-Content '%ERR_LOG%' -Tail 120 -Wait } else { Write-Host 'Arquivo nao encontrado: %ERR_LOG%' }"
start "Cloudflare tunnel" powershell -NoExit -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%TUNNEL_LOG%') { Get-Content '%TUNNEL_LOG%' -Tail 120 -Wait } else { Write-Host 'Arquivo nao encontrado: %TUNNEL_LOG%' }"

echo Janelas abertas. Pode fechar esta janela.
endlocal
