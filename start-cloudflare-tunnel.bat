@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"

if not defined TOOLS_DIR set "TOOLS_DIR=%ROOT%tools"
if not defined CLOUDFLARED set "CLOUDFLARED=%TOOLS_DIR%\cloudflared.exe"
if not defined CLOUDFLARED_DOWNLOAD_URL set "CLOUDFLARED_DOWNLOAD_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
if not defined LOG_DIR set "LOG_DIR=%ROOT%logs"
if not defined LOG_FILE set "LOG_FILE=%LOG_DIR%\cloudflared-bat.log"
if not defined FASTAPI_LOG_FILE set "FASTAPI_LOG_FILE=%LOG_DIR%\fastapi-bat.log"
if not defined FASTAPI_ERR_FILE set "FASTAPI_ERR_FILE=%LOG_DIR%\fastapi-bat.err.log"
if not defined URL_FILE set "URL_FILE=%ROOT%tunnel-urls.txt"

if not defined LOCAL_URL set "LOCAL_URL=http://127.0.0.1:8017"
if not defined FASTAPI_HOST set "FASTAPI_HOST=127.0.0.1"
if not defined FASTAPI_PORT set "FASTAPI_PORT=8017"
if not defined START_FASTAPI set "START_FASTAPI=true"
if not defined TENANT_ID set "TENANT_ID=demo-tenant"
if not defined NICKY_WEBHOOK_TOKEN set "NICKY_WEBHOOK_TOKEN=tenant_webhook_token_here"
if not defined FASTAPI_EXE (
  if exist "%ROOT%.venv\Scripts\python.exe" (
    set "FASTAPI_EXE=%ROOT%.venv\Scripts\python.exe"
  ) else (
    set "FASTAPI_EXE=python"
  )
)

if not exist "%CLOUDFLARED%" (
  echo cloudflared.exe nao encontrado em:
  echo %CLOUDFLARED%
  echo.
  echo Baixando cloudflared para a pasta tools...
  if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:CLOUDFLARED_DOWNLOAD_URL -OutFile $env:CLOUDFLARED"
  if errorlevel 1 (
    echo [ERROR] Falha ao baixar cloudflared.exe.
    echo URL: %CLOUDFLARED_DOWNLOAD_URL%
    exit /b 1
  )
  if not exist "%CLOUDFLARED%" (
    echo [ERROR] Download finalizado, mas cloudflared.exe nao foi encontrado em:
    echo %CLOUDFLARED%
    exit /b 1
  )
)

"%CLOUDFLARED%" --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] cloudflared.exe existe, mas nao executou corretamente:
  echo %CLOUDFLARED%
  exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if exist "%LOG_FILE%" del /q "%LOG_FILE%" >nul 2>nul
if exist "%URL_FILE%" del /q "%URL_FILE%" >nul 2>nul

echo Subindo Cloudflare Tunnel para %LOCAL_URL% ...
echo Log: %LOG_FILE%
echo.

start "Nicky Ticket Tailor Cloudflare Tunnel" /min cmd /c ""%CLOUDFLARED%" tunnel --url "%LOCAL_URL%" --no-autoupdate > "%LOG_FILE%" 2>&1"

set "PUBLIC_URL="
for /L %%i in (1,1,60) do (
  for /f "tokens=* delims=" %%u in ('findstr /r /c:"https://.*\.trycloudflare\.com" "%LOG_FILE%" 2^>nul') do (
    set "LINE=%%u"
    for %%w in (!LINE!) do (
      echo %%w | findstr /r "^https://.*\.trycloudflare\.com" >nul
      if not errorlevel 1 set "PUBLIC_URL=%%w"
    )
  )
  if defined PUBLIC_URL goto :FOUND_URL
  timeout /t 1 /nobreak >nul
)

echo [ERROR] Nao foi possivel capturar a URL do Cloudflare Tunnel.
echo Veja o log em: %LOG_FILE%
exit /b 1

:FOUND_URL
set "PUBLIC_URL=%PUBLIC_URL:,=%"
set "PUBLIC_URL=%PUBLIC_URL:)=%"
set "PUBLIC_URL=%PUBLIC_URL:(=%"

set "HEALTH_URL=%PUBLIC_URL%/health"
set "DOCS_URL=%PUBLIC_URL%/docs"
set "ADMIN_UI_URL=%PUBLIC_URL%/admin-ui"
set "AUTH0_CALLBACK_URL=%PUBLIC_URL%/admin-ui/callback"
set "TICKET_TAILOR_WEBHOOK_URL=%PUBLIC_URL%/webhooks/ticket-tailor/%TENANT_ID%"
set "NICKY_WEBHOOK_URL=%PUBLIC_URL%/webhooks/nicky/%TENANT_ID%?token=%NICKY_WEBHOOK_TOKEN%"
set "NICKY_SUCCESS_URL=%PUBLIC_URL%/nicky/success"
set "NICKY_CANCEL_URL=%PUBLIC_URL%/nicky/cancel"

if /I "%START_FASTAPI%"=="true" call :ENSURE_FASTAPI
goto :WRITE_URL_FILE

:ENSURE_FASTAPI
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $ProgressPreference='SilentlyContinue'; (Invoke-WebRequest -Uri '%LOCAL_URL%/health' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  echo FastAPI ja respondeu em %LOCAL_URL%/health.
  echo Se Auth0 usar a URL publica, confirme que o processo atual foi iniciado com APP_BASE_URL=%PUBLIC_URL%.
  exit /b 0
)

echo FastAPI nao respondeu em %LOCAL_URL%/health.
echo Subindo FastAPI com APP_BASE_URL=%PUBLIC_URL% ...
if exist "%FASTAPI_LOG_FILE%" del /q "%FASTAPI_LOG_FILE%" >nul 2>nul
if exist "%FASTAPI_ERR_FILE%" del /q "%FASTAPI_ERR_FILE%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $env:APP_BASE_URL='%PUBLIC_URL%'; $env:NICKY_SUCCESS_URL='%NICKY_SUCCESS_URL%'; $env:NICKY_CANCEL_URL='%NICKY_CANCEL_URL%'; Start-Process -FilePath '%FASTAPI_EXE%' -ArgumentList @('-m','uvicorn','app.main:app','--host','%FASTAPI_HOST%','--port','%FASTAPI_PORT%') -WorkingDirectory '%ROOT%' -WindowStyle Minimized -RedirectStandardOutput '%FASTAPI_LOG_FILE%' -RedirectStandardError '%FASTAPI_ERR_FILE%'"
for /L %%i in (1,1,30) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Invoke-WebRequest -Uri '%LOCAL_URL%/health' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }"
  if not errorlevel 1 (
    echo FastAPI pronto em %LOCAL_URL%
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)
echo [WARN] FastAPI foi iniciado, mas o health check ainda nao respondeu.
echo Veja logs:
echo %FASTAPI_LOG_FILE%
echo %FASTAPI_ERR_FILE%
exit /b 0

:WRITE_URL_FILE

(
  echo Public URL: %PUBLIC_URL%
  echo Health: %HEALTH_URL%
  echo Docs: %DOCS_URL%
  echo Admin UI: %ADMIN_UI_URL%
  echo Auth0 callback URL: %AUTH0_CALLBACK_URL%
  echo.
  echo Ticket Tailor webhook:
  echo %TICKET_TAILOR_WEBHOOK_URL%
  echo.
  echo Nicky webhook:
  echo %NICKY_WEBHOOK_URL%
  echo.
  echo Nicky successUrl:
  echo %NICKY_SUCCESS_URL%
  echo.
  echo Nicky cancelUrl:
  echo %NICKY_CANCEL_URL%
  echo.
  echo Local service expected at:
  echo %LOCAL_URL%
  echo.
  echo FastAPI log:
  echo %FASTAPI_LOG_FILE%
  echo %FASTAPI_ERR_FILE%
  echo.
  echo Cloudflared log:
  echo %LOG_FILE%
) > "%URL_FILE%"

echo URLs geradas:
echo.
type "%URL_FILE%"
echo.
echo Salvo em:
echo %URL_FILE%
echo.
echo A janela minimizada "Nicky Ticket Tailor Cloudflare Tunnel" deve ficar aberta enquanto o tunel estiver em uso.

endlocal
