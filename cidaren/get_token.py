"""
自动获取词达人 Token
使用 mitmproxy 拦截请求，自动保存 USERTOKEN、ABC、AUTH_V 到 .env
"""
import os
import sys
import subprocess
import shutil
import ctypes
import winreg


def set_windows_proxy(enable, host="127.0.0.1", port=8888):
    """设置 Windows 系统代理"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_WRITE
        )

        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
        winreg.CloseKey(key)

        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        internet_set_option = ctypes.windll.Wininet.InternetSetOptionW
        internet_set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        internet_set_option(0, INTERNET_OPTION_REFRESH, 0, 0)

        return True
    except Exception as e:
        print(f"[WARN] Failed to set proxy: {e}")
        return False


def get_original_proxy():
    """获取原始代理设置"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_READ
        )

        try:
            proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except FileNotFoundError:
            proxy_enable = 0

        try:
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except FileNotFoundError:
            proxy_server = ""

        winreg.CloseKey(key)
        return {"enabled": bool(proxy_enable), "server": proxy_server}
    except Exception:
        return None


def main():
    print("=" * 50)
    print("  cidaren Token Catcher")
    print("=" * 50)
    print()

    if not shutil.which("mitmdump"):
        print("[ERROR] mitmdump not found!")
        print("Please install mitmproxy: pip install mitmproxy")
        input("Press Enter to exit...")
        return

    addon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_catcher.py")
    if not os.path.exists(addon_path):
        print(f"[ERROR] Addon not found: {addon_path}")
        input("Press Enter to exit...")
        return

    print("[1/3] Saving original proxy settings...")
    original = get_original_proxy()

    print("[2/3] Setting system proxy to 127.0.0.1:8888...")
    set_windows_proxy(True)

    print("[3/3] Starting mitmdump...")
    print()
    print("=" * 50)
    print("  Please open your browser and visit:")
    print("  https://app.vocabgo.com/studentv1/")
    print()
    print("  Or open PC WeChat and visit the authorization link.")
    print("=" * 50)
    print()

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    mitm = None
    try:
        mitm = subprocess.Popen(
            [
                "mitmdump",
                "-s", addon_path,
                "--listen-port", "8888",
                "--quiet",
                "--set", "console_eventlog_verbosity=error",
            ],
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
        )

        print("Waiting for tokens... (Press Ctrl+C to stop)")
        print()
        mitm.wait()

    except KeyboardInterrupt:
        print("\n[INFO] Stopping...")
    finally:
        if mitm:
            mitm.terminate()
            try:
                mitm.wait(timeout=3)
            except subprocess.TimeoutExpired:
                mitm.kill()
                mitm.wait()

        print("[INFO] Restoring original proxy settings...")
        if original:
            set_windows_proxy(original["enabled"])
        else:
            set_windows_proxy(False)

        print("[OK] Done!")
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()
