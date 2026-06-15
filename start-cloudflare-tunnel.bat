@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"

if not defined TOOLS_DIR set "TOOLS_DIR=%ROOT%tools"
if not defined CLOUDFLARED set "CLOUDFLARED=%TOOLS_DIR%\cloudflared.exe"
if not defined CLOUDFLARED_DOWNLOAD_URL set "CLOUDFLARED_DOWNLOAD_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
if not defined LOG_DIR set "LOG_DIR=%ROOT%logs"
if not defined LOG_FILE set "LOG_FILE=%LOG_DIR%\cloudflared-bat.log"
if not defined URL_FILE set "URL_FILE=%ROOT%tunnel-urls.txt"

if not defined LOCAL_URL set "LOCAL_URL=http://127.0.0.1:8017"
if not defined TENANT_ID set "TENANT_ID=demo-tenant"
if not defined NICKY_WEBHOOK_TOKEN set "NICKY_WEBHOOK_TOKEN=tenant_webhook_token_here"

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
set "TICKET_TAILOR_WEBHOOK_URL=%PUBLIC_URL%/webhooks/ticket-tailor/%TENANT_ID%"
set "NICKY_WEBHOOK_URL=%PUBLIC_URL%/webhooks/nicky/%TENANT_ID%?token=%NICKY_WEBHOOK_TOKEN%"
set "NICKY_SUCCESS_URL=%PUBLIC_URL%/nicky/success"
set "NICKY_CANCEL_URL=%PUBLIC_URL%/nicky/cancel"

(
  echo Public URL: %PUBLIC_URL%
  echo Health: %HEALTH_URL%
  echo Docs: %DOCS_URL%
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
