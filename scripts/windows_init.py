"""Interactive, current-user Windows bootstrap; shared read-only sync after capture."""
import csv
import io
import os
from pathlib import Path
import platform
import queue
import re
import struct
import subprocess
import threading
import time

from cipher_reader import library
from db_export import ExportError, candidate_matches
from runtime_support import private_directory

HOOK = Path(__file__).parent / "windows/capture_key.js"


class CaptureTargetExited(ExportError):
    """A launcher exited before any capture hook became ready."""


def account_paths(root=None):
    if root is None:
        import winreg
        documents = Path.home() / "Documents"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                documents = Path(os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0]))
        except FileNotFoundError:
            pass
        root = documents / "xwechat_files"
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ExportError("没有找到 xwechat_files；请用 --db-root 指定微信设置中的数据目录。")
    accounts = sorted(p.resolve() for p in root.glob("*/db_storage")
                      if (p / "message").is_dir() and any((p / "message").glob("message_*.db")))
    if not accounts:
        raise ExportError("该目录没有消息数据库，请先用微信登录并同步。")
    return accounts


def select_account(accounts):
    print("请选择本人的账号，随后在微信中登录同一账号：", flush=True)
    for i, path in enumerate(accounts, 1):
        print(f"  {i}. {path.parent.name}", flush=True)
    answer = input("输入编号" + ("（回车选择 1）" if len(accounts) == 1 else "") + "：").strip()
    if not answer and len(accounts) == 1:
        return accounts[0]
    if answer.isdigit() and 1 <= int(answer) <= len(accounts):
        return accounts[int(answer) - 1]
    raise ExportError("没有选择有效账号，初始化已停止。")


def parse_processes(text):
    result = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 5 and row[0].casefold() == "weixin.exe" and row[1].isdigit():
            memory = re.sub(r"[^0-9]", "", row[4])
            if int(memory or 0) > 20 * 1024:
                result.append((int(memory), int(row[1])))
    return [pid for _, pid in sorted(result, reverse=True)]


def processes(username):
    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FI",
                             "USERNAME eq " + username, "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, errors="replace", timeout=2)
    if result.returncode:
        raise ExportError("无法查询当前 Windows 用户的微信进程。")
    return parse_processes(result.stdout)


def capture(device, pid, databases, timeout):
    candidates = queue.Queue(maxsize=64)
    stopped, ready = threading.Event(), threading.Event()
    failed = threading.Event()

    def on_message(message, _data):
        if message.get("type") == "error":
            failed.set()
            return
        payload = message.get("payload")
        if message.get("type") != "send" or not isinstance(payload, dict):
            return
        if payload.get("kind") == "ready":
            ready.set()
        elif payload.get("kind") == "error":
            failed.set()
        elif payload.get("kind") == "candidate":
            value = payload.get("value", "")
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                try:
                    candidates.put_nowait(bytes.fromhex(value))
                except queue.Full:
                    failed.set()

    session = device.attach(pid)
    session.on("detached", lambda *_: stopped.set())
    try:
        script = session.create_script(HOOK.read_text(encoding="utf-8"))
        script.on("message", on_message)
        script.load()
        announced, seen = False, set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not failed.is_set():
            if ready.is_set() and not announced:
                print("捕获已就绪；请在微信登录页进入所选账号。", flush=True)
                announced = True
            try:
                candidate = candidates.get(timeout=0.1)
            except queue.Empty:
                if stopped.is_set():
                    break
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            # Full SQLCipher page HMAC, not a few plausible plaintext header bytes.
            if candidate_matches(candidate, databases):
                return candidate
        if stopped.is_set() and not ready.is_set():
            raise CaptureTargetExited("启动进程已退出，继续等待微信主进程。")
        raise ExportError("未取得匹配所选账号的密钥；请检查微信版本、桌面登录顺序和调试权限。")
    except Exception as exc:
        if stopped.is_set() and not ready.is_set() and not isinstance(exc, CaptureTargetExited):
            raise CaptureTargetExited("启动进程已退出，继续等待微信主进程。") from None
        raise
    finally:
        try:
            session.detach()
        except Exception:
            if not stopped.is_set():
                raise ExportError("调试器未正常脱离，请手动退出微信后再试。") from None


def capture_started_wechat(device, username, databases, timeout, frida):
    deadline = time.monotonic() + timeout
    exited = set()
    while time.monotonic() < deadline:
        pids = [pid for pid in processes(username) if pid not in exited]
        if not pids:
            time.sleep(0.04)
            continue
        pid = pids[0]
        try:
            return capture(device, pid, databases, max(0.1, deadline - time.monotonic()))
        except (CaptureTargetExited, frida.ProcessNotFoundError):
            exited.add(pid)
        except (frida.PermissionDeniedError, frida.InvalidOperationError, frida.TransportError,
                frida.TimedOutError, frida.NotSupportedError, frida.ProcessNotRespondingError):
            raise ExportError("无法捕获微信密钥；请确认当前桌面用户与微信权限一致，且版本兼容。") from None
    raise ExportError("等待微信主进程超时，未保存或替换密钥。")


def initialize(home, *, root=None, timeout=180):
    if not 30 <= timeout <= 900:
        raise ExportError("--timeout 必须为 30–900 秒。")
    if struct.calcsize("P") != 8 or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ExportError("Windows 初始化目前支持 x64 微信和 64 位 Python 3.11+。")
    try:
        import frida
    except ImportError:
        raise ExportError("缺少 Frida，请先运行 windows/setup.ps1。") from None
    library()  # Check native dependencies before asking the user to restart WeChat.
    account = select_account(account_paths(root))
    databases = sorted((account / "message").glob("message_*.db"))[:3]
    private_directory(home)
    from windows_credentials import current_username
    username = current_username()
    print("本次需监听微信登录过程获取数据库密钥，只保存在 Windows 凭据管理器。", flush=True)
    print("请通过微信托盘菜单完全退出微信；不会强制结束微信或修改它的文件。", flush=True)
    input("完全退出后按回车，取消请按 Ctrl+C：")
    if processes(username):
        raise ExportError("微信尚未退出；本次未进行捕获，请从托盘退出后重试。")
    device = frida.get_local_device()
    print("请现在从 Windows 桌面打开微信，等待“捕获已就绪”后再点击登录。", flush=True)
    secret = capture_started_wechat(device, username, databases, timeout, frida)
    try:
        from wechat_sync import configure
        configure(account, secret, home=home)
    finally:
        secret = None
