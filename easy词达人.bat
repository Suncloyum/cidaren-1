@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   词达人 cidaren 控制台
echo ================================
echo.
echo 正在启动服务...
echo.

REM 检查是否有 uv，优先使用 uv；否则用 pip
where uv >nul 2>&1
if %errorlevel% equ 0 (
    echo [检测到 uv] 使用 uv 运行...
    uv run python -m cidaren
) else (
    echo [未检测到 uv] 使用系统 Python 运行...
    python -m cidaren
)

echo.
echo 服务已停止。
pause
