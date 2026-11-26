@echo off
chcp 65001 >nul
title CodeBoss 安装
cd /d "%~dp0"
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo 安装没成功（退出码 %RC%）。把上面的报错截图发给客服即可。
pause
