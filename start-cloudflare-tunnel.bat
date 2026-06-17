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
if not defined RESTART_FASTAPI set "RESTART_FASTAPI=true"
if not defined TENANT_ID set "TENANT_ID="
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

if /I "%START_FASTAPI%"=="true" call :PRECHECK_FASTAPI_RUNTIME
if errorlevel 1 exit /b 1
if /I "%START_FASTAPI%"=="true" call :PRECHECK_AUTH0
if errorlevel 1 exit /b 1

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
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul
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
if defined TENANT_ID (
  set "TICKET_TAILOR_WEBHOOK_URL=%PUBLIC_URL%/webhooks/ticket-tailor/%TENANT_ID%"
) else (
  set "TICKET_TAILOR_WEBHOOK_URL=%PUBLIC_URL%/webhooks/ticket-tailor/{tenant_uuid}"
)
set "NICKY_SUCCESS_URL=%PUBLIC_URL%/nicky/success"
set "NICKY_CANCEL_URL=%PUBLIC_URL%/nicky/cancel"

if /I "%START_FASTAPI%"=="true" call :ENSURE_FASTAPI
if errorlevel 1 exit /b 1
goto :WRITE_URL_FILE

:PRECHECK_FASTAPI_RUNTIME
if /I not "%RESTART_FASTAPI%"=="true" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $ProgressPreference='SilentlyContinue'; (Invoke-WebRequest -Uri '%LOCAL_URL%/health' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }"
  if not errorlevel 1 exit /b 0
)

"%FASTAPI_EXE%" -c "import uvicorn" >nul 2>nul
if not errorlevel 1 exit /b 0

echo [ERROR] O helper precisa iniciar FastAPI, mas o runtime selecionado nao possui o modulo 'uvicorn'.
echo Runtime verificado:
echo %FASTAPI_EXE%
echo.
echo Corrija o ambiente antes de abrir o tunel:
echo   python -m venv .venv
echo   .\.venv\Scripts\Activate.ps1
echo   pip install -e .
echo.
echo Ou inicie o servico manualmente e execute novamente com:
echo   set START_FASTAPI=false
exit /b 1

:PRECHECK_AUTH0
pushd "%ROOT%" >nul
"%FASTAPI_EXE%" -c "from app.config import get_settings; from app.admin_auth import auth0_enabled; raise SystemExit(0 if auth0_enabled(get_settings()) else 1)" >nul 2>nul
set "AUTH0_CHECK=%ERRORLEVEL%"
popd >nul
if "%AUTH0_CHECK%"=="0" exit /b 0

echo [ERROR] Auth0 e obrigatorio, mas AUTH0_DOMAIN/AUTH0_CLIENT_ID nao estao configurados.
echo.
echo Crie um .env a partir de .env.example ou defina antes de executar:
echo   set AUTH0_DOMAIN=seu-tenant.auth0.com
echo   set AUTH0_CLIENT_ID=seu-client-id
echo   set AUTH0_AUDIENCE=sua-audience-opcional
echo   set ADMIN_ALLOWED_ROLES=Admin
echo.
echo Para o teste local com o client Auth0 de desenvolvimento, use:
echo   start-local-auth0-compat.bat
exit /b 1

:ENSURE_FASTAPI
if /I "%RESTART_FASTAPI%"=="true" call :STOP_FASTAPI_ON_PORT

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
set "APP_BASE_URL=%PUBLIC_URL%"
set "NICKY_SUCCESS_URL=%NICKY_SUCCESS_URL%"
set "NICKY_CANCEL_URL=%NICKY_CANCEL_URL%"
start "Nicky Ticket Tailor FastAPI" /min /d "%ROOT%" cmd /c ""%FASTAPI_EXE%" -m uvicorn app.main:app --host %FASTAPI_HOST% --port %FASTAPI_PORT% > "%FASTAPI_LOG_FILE%" 2> "%FASTAPI_ERR_FILE%""
for /L %%i in (1,1,30) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Invoke-WebRequest -Uri '%LOCAL_URL%/health' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }"
  if not errorlevel 1 (
    echo FastAPI pronto em %LOCAL_URL%
    exit /b 0
  )
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul
)
echo [WARN] FastAPI foi iniciado, mas o health check ainda nao respondeu.
echo Veja logs:
echo %FASTAPI_LOG_FILE%
echo %FASTAPI_ERR_FILE%
exit /b 1

:STOP_FASTAPI_ON_PORT
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%FASTAPI_PORT% .*LISTENING" 2^>nul') do (
  if not "%%p"=="0" (
    echo Reiniciando FastAPI: parando processo na porta %FASTAPI_PORT% ^(PID %%p^) ...
    taskkill /PID %%p /F >nul 2>nul
  )
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" >nul
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
  echo Cadastrado automaticamente ao salvar o tenant na UI.
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
