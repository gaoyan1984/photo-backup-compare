# -*- coding: utf-8 -*-
"""
把「照片备份对比/补齐工具」装到 Windows 开机自启

做法：在「启动」文件夹里放一个 .vbs，用 pythonw.exe 无窗口静默拉起 app.py。
这样每次开机/登录后工具自己就跑了，直接浏览器访问 http://127.0.0.1:8765/ 即可，
不用再手动双击任何东西，也不会弹出黑色命令行窗口。

用法：
    python install_autostart.py          安装
    python install_autostart.py install  安装
    python install_autostart.py remove   卸载
    python install_autostart.py status   查看状态
"""

import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "服务日志.txt")
STARTUP = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs\Startup")
VBS_NAME = "照片备份对比工具.vbs"
VBS_PATH = os.path.join(STARTUP, VBS_NAME)


def find_pythonw():
    """优先用包内自带的 python 的 pythonw.exe（保证换机器也能用）"""
    cands = [
        os.path.join(BASE_DIR, "python", "pythonw.exe"),
        os.path.join(BASE_DIR, "python", "Scripts", "pythonw.exe"),
        os.path.join(sys.prefix, "Scripts", "pythonw.exe"),
        os.path.join(sys.prefix, "pythonw.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def write_vbs():
    pw = find_pythonw()
    if not pw:
        print("✘ 找不到 pythonw.exe，无法安装开机自启")
        return None
    app_py = os.path.join(BASE_DIR, "app.py")
    # 注意引号写法：VBScript 字符串里要用 "" 表示一个字面引号。
    # 最终要拼出的命令行是：
    #   "pythonw.exe路径" "app.py路径" --startup
    # 所以 app_py 外面包一层引号 = 写成 """ + app_py + """
    cmd = '"%s" "%s" --startup' % (pw, app_py)
    # vbs 里加 On Error 兜底：pythonw 无窗口，一出错用户完全看不到，
    # 所以把失败原因写进日志文件，方便回头排查。
    with open(VBS_PATH, "w", encoding="utf-8-sig") as f:
        f.write(
            "' 照片备份对比/补齐工具 —— 开机静默启动\n"
            "On Error Resume Next\n"
            'Set ws = CreateObject("WScript.Shell")\n'
            f'ws.CurrentDirectory = "{BASE_DIR}"\n'
            "ws.Run " + _vbs_quote(cmd) + ", 0, False\n"
            "If Err.Number <> 0 Then\n"
            f'  Set fso = CreateObject("Scripting.FileSystemObject")\n'
            f'  Set t = fso.OpenTextFile("{LOG_PATH}", 8, True)\n'
            '  t.WriteLine "[" & Now & "] [开机自启] vbs 执行失败: " & Err.Description\n'
            "  t.Close\n"
            "End If\n"
        )
    print(f"✔ 已写入启动脚本：{VBS_PATH}")
    print(f"  解释器：{pw}")
    print(f"  命令行：{cmd}")
    return VBS_PATH


def _vbs_quote(s):
    """把字符串安全地包成 VBScript 字符串字面量"""
    return '"' + s.replace('"', '""') + '"'


def remove():
    if os.path.isfile(VBS_PATH):
        os.remove(VBS_PATH)
        print(f"✔ 已删除启动脚本：{VBS_PATH}")
    else:
        print("· 本来就没有安装开机自启")
    kill_running()


def kill_running():
    """顺手停掉正在运行的服务（如果有）"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*app.py*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, text=True, timeout=20)
        print("✔ 已尝试停止正在运行的服务")
    except Exception as e:
        print(f"· 停止服务时跳过：{e}")


def status():
    print(f"启动文件夹：{STARTUP}")
    print(f"自启脚本  ：{VBS_PATH}  ->  {'已安装' if os.path.isfile(VBS_PATH) else '未安装'}")
    print(f"解释器    ：{find_pythonw() or '未找到'}")
    # 检查端口是否在监听
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 8765), timeout=1.5)
        s.close()
        print("服务状态  ：✔ 正在运行（http://127.0.0.1:8765/）")
    except Exception:
        print("服务状态  ：✘ 未运行")
    # 附带日志，便于排查"为什么没起来"
    print("\n--- 最近日志 ---")
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for l in lines[-12:]:
            print("  " + l.rstrip())
    except OSError:
        print("  （还没有日志文件）")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    if cmd == "install":
        if not os.path.isdir(STARTUP):
            print(f"✘ 找不到启动文件夹：{STARTUP}")
            return
        write_vbs()
        print("\n已装好开机自启。验证方法：")
        print("  1) 注销再登录，或直接重启电脑")
        print("  2) 浏览器打开 http://127.0.0.1:8765/ 应该就能直接用")
        print("\n想现在就启动（不用等重启），请双击 启动.bat")
    elif cmd == "remove":
        remove()
    elif cmd == "status":
        status()
    else:
        print("用法：python install_autostart.py [install|remove|status]")


if __name__ == "__main__":
    main()
