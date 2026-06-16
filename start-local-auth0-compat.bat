@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "LOG_DIR=%ROOT%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if not defined FASTAPI_HOST set "FASTAPI_HOST=127.0.0.1"
if not defined FASTAPI_PORT set "FASTAPI_PORT=4200"
if not defined APP_BASE_URL set "APP_BASE_URL=http://localhost:%FASTAPI_PORT%"
if not defined AUTH0_CALLBACK_PATH set "AUTH0_CALLBACK_PATH=/overview"
if not defined AUTH0_DOMAIN set "AUTH0_DOMAIN=dev-eq0ptfwdhb1s1h12.us.auth0.com"
if not defined AUTH0_CLIENT_ID set "AUTH0_CLIENT_ID=SqrJq2fxJ6adrOFaR24oh9COF4vZwqba"
if not defined AUTH0_AUDIENCE set "AUTH0_AUDIENCE=https://nicky-tech.azurewebsites.net"
if not defined AUTH0_CLIENT_SECRET set "AUTH0_CLIENT_SECRET="
if not defined ADMIN_ALLOWED_ROLES set "ADMIN_ALLOWED_ROLES=*"

if not defined FASTAPI_EXE (
  if exist "%ROOT%.venv\Scripts\python.exe" (
    set "FASTAPI_EXE=%ROOT%.venv\Scripts\python.exe"
  ) else (
    set "FASTAPI_EXE=python"
  )
)

echo Starting Nicky Ticket Tailor service in Auth0 local compatibility mode.
echo.
echo Admin UI:
echo %APP_BASE_URL%/overview
echo.
echo Auth0 callback URL:
echo %APP_BASE_URL%%AUTH0_CALLBACK_PATH%
echo.
echo Allowed roles:
echo %ADMIN_ALLOWED_ROLES%
echo.
echo Health:
echo %APP_BASE_URL%/health
echo.
echo Keep this window open while testing.
echo.

cd /d "%ROOT%"
"%FASTAPI_EXE%" -m uvicorn app.main:app --host "%FASTAPI_HOST%" --port "%FASTAPI_PORT%"

endlocal
