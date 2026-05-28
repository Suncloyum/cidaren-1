@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   词达人 - 安装依赖
echo ================================
echo.

where uv >nul 2>&1
if %errorlevel% equ 0 (
    echo [检测到 uv] 使用 uv 安装依赖...
    uv sync
) else (
    echo [未检测到 uv] 使用 pip 安装依赖...
    pip install flask requests
)

echo.
echo 安装完成！现在可以双击 "启动词达人.bat" 启动控制台。
pause
