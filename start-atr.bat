@echo off
setlocal
title ATR Trading Dashboard

rem ------------------------------------------------------------------
rem  ATR dashboard launcher — double-click this to start the app.
rem  Your browser opens automatically. Close this window to stop it.
rem ------------------------------------------------------------------

cd /d "D:\ALGO"

rem ---- locate the uv launcher (needed to run the app) ----
set "UV="
where uv >nul 2>nul && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe"      set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Programs\uv\uv.exe"    set "UV=%LOCALAPPDATA%\Programs\uv\uv.exe"
if not defined UV if exist "C:\Program Files\uv\uv.exe"           set "UV=C:\Program Files\uv\uv.exe"
if not defined UV (
  echo.
  echo  ATR could not find "uv" on this computer.
  echo  Install it once at https://docs.astral.sh/uv/ then try again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting ATR... your browser will open in a moment.
echo   Keep this window open while you use the app.
echo   Close this window (or press Ctrl+C) to stop the dashboard.
echo.

"%UV%" run atr serve %*
if errorlevel 1 (
  echo.
  echo  The app stopped unexpectedly. Read the message above.
  echo  You can close this window now.
  pause
)
endlocal