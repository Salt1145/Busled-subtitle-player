@echo off
rem SPDX-License-Identifier: MIT
rem Copyright (c) 2026 Salt1145
rem https://github.com/Salt1145/Busled-subtitle-player
rem ---------------------------------------------------------------
rem  LED subtitle player launcher
rem  ASCII only on purpose (cmd.exe parses this file as GBK/OEM,
rem  non-ASCII bytes would break the script). Path uses %~dp0.
rem ---------------------------------------------------------------
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found in PATH.
  echo Install Python 3 and make sure "python" works in a terminal.
  echo.
  pause
  exit /b 1
)

echo Starting LED subtitle player...
echo (config window opens first, nothing plays until you press start)
echo.

python "%~dp0led_player.py" %*

if errorlevel 1 (
  echo.
  echo [ERROR] player exited with an error. See messages above.
  pause
)
