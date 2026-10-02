@echo off
rem Lance CyColabConnect (interface web + serveur MCP) sur http://127.0.0.1:8420
cd /d "%~dp0"
if not exist "frontend\dist\index.html" (
  echo Construction de l'interface...
  pushd frontend
  call npm install || exit /b 1
  call npm run build || exit /b 1
  popd
)
set "CC_ARGS=%*"
:run
uv run --directory backend colabconnect serve %CC_ARGS%
rem Code 3 = redemarrage demande apres une mise a jour
if %ERRORLEVEL%==3 (
  set "CC_ARGS=%* --no-browser"
  goto run
)
