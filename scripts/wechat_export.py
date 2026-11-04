#!/usr/bin/env python3
"""Interactive, standalone macOS WeChat database exporter."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from db_export import CommonCrypto, ExportError, export_database, snapshot_databases

VERSION = "0.3.0"
BUNDLE_ID = "com.tencent.xinWeChat"
CODE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_ROOT = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"


def say(message):
    print(message, flush=True)


def run(args, timeout=60):
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as exc:
        # Never echo subprocess output: debugger output can contain key material.
        raise ExportError(f"{Path(str(args[0])).name} 执行失败（退出码 {exc.returncode}）。") from None
    except subprocess.TimeoutExpired:
        raise ExportError(f"{Path(str(args[0])).name} 超时，请检查系统授权提示。") from None


def app_info(path):
    try:
        with (path / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            raise ExportError("所选应用不是 macOS 微信。")
        executable = info["CFBundleExecutable"]
        if Path(executable).name != executable:
            raise ExportError("应用可执行文件名无效。")
        if not (path / "Contents/MacOS" / executable).is_file():
            raise ExportError("微信应用缺少可执行文件。")
        return info
    except (OSError, KeyError, plistlib.InvalidFileException):
        raise ExportError("无法读取微信应用；请用 --app 指定实际的 .app 路径。") from None


def find_app(explicit):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path, app_info(path)
    for directory in (Path("/Applications"), Path.home() / "Applications"):
        for name in ("WeChat.app", "Weixin.app", "微信.app"):
            path = directory / name
            if path.exists():
                return path.resolve(), app_info(path)
    raise ExportError("没有找到微信，请先安装桌面版微信，或用 --app 指定位置。")


def app_pids(executable, *, app=None):
    result = subprocess.run(["/usr/bin/pgrep", "-x", executable], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ExportError("无法检查微信进程。")
    pids = [int(p) for p in result.stdout.split()]
    if app is not None:
        prefix = str(Path(app).resolve()) + "/"
        pids = [pid for pid in pids if process_path(pid).startswith(prefix)]
    return pids


def process_path(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True)
    return result.stdout.strip()


def wait_closed(executable, timeout=15, *, app=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not app_pids(executable, app=app):
            return True
        time.sleep(0.25)
    return not app_pids(executable, app=app)


def quit_app(path, executable):
    pids = app_pids(executable, app=path)
    if not pids:
        return
    # Both copies share a bundle ID. Target the live process so LaunchServices
    # cannot route the quit request to the other copy or launch an application.
    script = '''use framework "AppKit"
on run argv
    set targetPid to (item 1 of argv) as integer
    set targetApp to current application's NSRunningApplication's runningApplicationWithProcessIdentifier:targetPid
    if targetApp is missing value then return
    targetApp's terminate()
end run'''
    for pid in pids:
        subprocess.run(["/usr/bin/osascript", "-e", script, str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
    if wait_closed(executable, app=path):
        return
    input("请在微信中按 ⌘Q 退出（不要只关窗口），然后按回车：")
    if not wait_closed(executable, 5, app=path):
        raise ExportError("微信尚未退出；已停止，未复制正在写入的数据库。")


def preflight(args):
    if platform.system() != "Darwin":
        raise ExportError("本工具仅支持 macOS。")
    if os.geteuid() == 0:
        raise ExportError("请以普通用户运行本工具；临时微信也必须以当前用户启动。")
    app, info = find_app(args.app)
    version = info.get("CFBundleShortVersionString", "未知")
    numbers = tuple(int(n) for n in re.findall(r"\d+", version)[:3])
    if not numbers or numbers < (4, 1):
        raise ExportError(f"当前微信版本 {version}。本版只提供 4.1 系列及之后的 passphrase 路线。")
    run(["/usr/bin/xcrun", "--find", "lldb"])
    run(["/usr/bin/xcrun", "lldb", "-x", "-b", "-o", "script import hashlib, json, pathlib"])
    CommonCrypto()
    say(f"微信 {version} · {platform.machine()} · Python {platform.python_version()}")
    say(f"应用：{app}")
    if not args.db_root.exists():
        raise ExportError("没有找到微信账号目录。请先用原版微信登录，或用 --db-root 指定 xwechat_files。")
    accounts = find_accounts(args.db_root)
    if not accounts:
        raise ExportError("账号目录中没有 db_storage 数据库，请先用原版微信完成登录和同步。")
    say(f"检测到 {len(accounts)} 个本地账号目录。")
    return app, info


def find_accounts(root):
    try:
        return sorted(p for p in root.glob("*/db_storage") if p.is_dir() and any(p.rglob("*.db")))
    except PermissionError:
        raise ExportError("无法读取微信目录。请在系统设置中为终端开启完全磁盘访问权限，再重新运行。") from None


def select_account(root):
    accounts = find_accounts(root)
    if not accounts:
        raise ExportError("没有找到账号数据库，请先在原版微信登录并同步。")
    say("请选择要同步或导出的账号；稍后在临时微信中登录同一账号：")
    for number, path in enumerate(accounts, 1):
        say(f"  {number}. {path.parent.name}")
    while True:
        answer = input("输入编号" + ("（回车选择 1）" if len(accounts) == 1 else "") + "：").strip()
        if not answer and len(accounts) == 1:
            return accounts[0]
        if answer.isdigit() and 1 <= int(answer) <= len(accounts):
            return accounts[int(answer) - 1]
        say("请输入列表中的编号。")


def debug_entitlements(original):
    entitlements = dict(original)
    # Identity claims require the vendor's certificate/provisioning. Keeping them
    # with an ad-hoc signature makes AMFI reject the executable with error -424.
    for key in ("com.apple.application-identifier", "application-identifier",
                "com.apple.developer.team-identifier", "keychain-access-groups"):
        entitlements.pop(key, None)
    entitlements["com.apple.security.get-task-allow"] = True
    return entitlements


def make_debug_copy(app, workspace):
    debug_app = workspace / "WeChat-debug.app"
    say("正在创建临时微信副本；应用较大时需要稍等……")
    run(["/usr/bin/ditto", app, debug_app], timeout=300)
    result = run(["/usr/bin/codesign", "-d", "--entitlements", ":-", debug_app])
    try:
        entitlements = debug_entitlements(plistlib.loads(result.stdout))
    except plistlib.InvalidFileException:
        raise ExportError("无法读取原应用的 entitlements，未继续重签名。") from None
    entitlements_path = workspace / "debug-entitlements.plist"
    entitlements_path.write_bytes(plistlib.dumps(entitlements))
    # Do not use --deep for signing: it overwrites nested helpers' sandbox-inherit
    # entitlements with the main app's entitlements and causes helper crashes.
    # Preserve all nested vendor signatures; only the main process needs debugging.
    run(["/usr/bin/codesign", "--force", "--sign", "-", "--options", "0",
         "--entitlements", entitlements_path, debug_app], timeout=180)
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", debug_app])
    check = run(["/usr/bin/codesign", "-d", "--entitlements", ":-", debug_app])
    if plistlib.loads(check.stdout).get("com.apple.security.get-task-allow") is not True:
        raise ExportError("临时副本未获得调试 entitlement。")
    flags = run(["/usr/bin/codesign", "-d", "-v", debug_app]).stderr
    match = re.search(rb"flags=0x([0-9a-fA-F]+)", flags)
    if match and int(match.group(1), 16) & 0x10000:
        raise ExportError("临时副本仍启用了 Hardened Runtime。")
    return debug_app


def capture(pid, account, workspace, timeout, *, executable=None):
    # Prefer representative message/contact DBs, without collecting unrelated accounts.
    databases = sorted(account.rglob("*.db"), key=lambda p: ("message" not in str(p), str(p)))
    databases = [p for p in databases if p.stat().st_size >= 4096]
    if not databases:
        raise ExportError("当前账号没有可用于验证的数据库。")
    # Bound time spent while the target is stopped; full validation happens offline later.
    candidates = databases[:3]
    config = workspace / "capture.json"
    config.write_text(json.dumps({"pid": pid, "executable": str(executable) if executable else None,
                                  "timeout": timeout,
                                  "databases": [str(p) for p in candidates]}))
    commands = workspace / "capture.lldb"
    commands.write_text(
        "settings set interpreter.prompt-on-quit false\n"
        "settings set target.skip-prologue false\n"
        "script import sys; sys.path.insert(0, " + repr(str(CODE_DIR)) + "); "
        "import capture_driver; capture_driver.run(lldb.debugger, " + repr(str(config)) + ")\n"
        "quit\n")
    say("正在设置捕获断点并启动临时微信……" if executable else "正在连接测试进程……")
    proc = subprocess.Popen([
        "/usr/bin/xcrun", "lldb", "-x", "-b", "-s", str(commands)
    ], cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    secret = None
    error = "driver"
    stops = set()
    buffer = b""
    deadline = time.monotonic() + timeout + 45
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not readable:
                continue
            block = os.read(proc.stdout.fileno(), 8192)
            if not block:
                break
            buffer += block
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if line == "WCDB_READY":
                    say(f"捕获已就绪。请在临时微信中完成一次登录，无需先退出再登录（限时 {timeout} 秒）。"
                        if executable else f"已连接，等待测试进程调用（限时 {timeout} 秒）。")
                elif re.fullmatch(r"WCDB_SECRET:[0-9a-f]{64}", line):
                    secret = bytes.fromhex(line.split(":", 1)[1])
                elif line.startswith("WCDB_ERROR:"):
                    error = line.split(":", 1)[1]
                elif re.fullmatch(r"WCDB_STOP:(signal|exception|reason):[0-9]{1,5}", line):
                    _, kind, number = line.split(":")
                    if kind == "signal":
                        try:
                            number = signal.Signals(int(number)).name
                        except ValueError:
                            pass
                    if len(stops) < 8:
                        stops.add(f"{kind}={number}")
                elif re.fullmatch(r"WCDB_SITE:[A-Za-z0-9_.+-]{1,80}:[0-9a-f]{1,16}", line) and len(stops) < 8:
                    _, module, offset = line.split(":")
                    stops.add(f"位置={module}+0x{offset}")
            # Ignore all other LLDB output, including possible memory displays.
            if len(buffer) > 65536:
                buffer = b""
        else:
            error = "timeout"
    finally:
        if proc.poll() is None:
            proc.terminate()  # The driver's finally detaches its target.
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise ExportError("调试器尚未退出，请手动退出临时微信及本次调试器后再重试。") from None
        proc.stdout.close()
    if secret is None or proc.returncode != 0:
        reasons = {
            "attach": "系统拒绝连接调试器。请检查开发者工具授权；本工具不会关闭 SIP。",
            "launch": "调试器无法启动临时微信。请检查系统的开发者工具授权提示。",
            "symbol": "此微信版本没有可用的密钥派生符号，当前方案不兼容。",
            "timeout": "等待超时或候选密钥验证失败。请确认登录了所选账号；此版本也可能不兼容。",
            "detach": "调试器未能正常脱离，请手动退出临时微信。",
            "exited": "临时微信在捕获期间退出。",
            "unexpected_stop": "临时微信出现非预期调试停止（" + ", ".join(sorted(stops) or ["原因未知"]) + "），已尝试脱离。",
            "continue": "调试器无法恢复临时微信运行，已尝试脱离。",
            "cancelled": "操作已取消。",
        }
        raise ExportError(reasons.get(error, "调试器执行失败；没有获得经过验证的密钥。"))
    say("密钥已通过数据库 HMAC 验证，调试器已脱离。")
    return secret


def export_all(snapshot, output, secret, version):
    output.mkdir(parents=True, mode=0o700)
    report = {"tool_version": VERSION, "wechat_version": version,
              "created_at": datetime.now().astimezone().isoformat(), "databases": []}
    crypto = CommonCrypto()
    for source in sorted(snapshot.rglob("*.db")):
        relative = source.relative_to(snapshot)
        entry = {"path": str(relative)}
        say(f"正在验证并导出：{relative}")
        try:
            if source.stat().st_size == 0 and not Path(str(source) + "-wal").exists():
                entry.update(status="skipped_empty")
            else:
                entry.update(export_database(source, output / "databases" / relative, secret, crypto))
                entry.update(status="ok", integrity_check="ok")
        except (ExportError, OSError, ValueError, sqlite3.Error) as exc:
            entry.update(status="failed", error=str(exc))
            say(f"  未导出：{exc}")
        report["databases"].append(entry)
    ok = sum(e["status"] == "ok" for e in report["databases"])
    failed = sum(e["status"] == "failed" for e in report["databases"])
    report.update(successful=ok, failed=failed, complete=failed == 0 and ok > 0)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    say(f"\n已验证导出 {ok} 个数据库，失败 {failed} 个。\n结果：{output}")
    return 0 if report["complete"] else 2


def main():
    parser = argparse.ArgumentParser(description="微信本地数据库导出助手（macOS，交互式）")
    parser.add_argument("--check", action="store_true", help="只检查环境，不退出或修改应用")
    parser.add_argument("--setup-sync", action="store_true", help="获取并保存密钥，初始化持续同步")
    parser.add_argument("--sync-home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--app", help="微信 .app 路径")
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT, help="xwechat_files 目录")
    parser.add_argument("--output", type=Path, help="新的结果目录（必须不存在）")
    parser.add_argument("--timeout", type=int, default=240, help="扫码登录等待秒数，30–900")
    args = parser.parse_args()
    args.db_root = args.db_root.expanduser().resolve()
    if not 30 <= args.timeout <= 900:
        parser.error("--timeout 必须为 30–900 秒")
    os.umask(0o077)
    # Avoid keys appearing in an operating-system core dump.
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    workspace, debug_app, lock = None, None, None
    original_running = False
    app, info = None, None
    result = 1
    try:
        say(f"微信本地数据库导出助手 {VERSION}\n")
        app, info = preflight(args)
        if args.check:
            say("基础环境检查通过；实际调试权限和微信版本兼容性需要运行时验证。")
            return 0
        output = (args.output or (Path.home() / "WeChat-Exports" /
                  datetime.now().strftime("%Y%m%d-%H%M%S-%f"))).expanduser().resolve()
        if output.exists() or output.is_relative_to(args.db_root) or output.is_relative_to(app):
            raise ExportError("请选择不存在、且位于微信应用和账号目录之外的输出目录。")
        cache = Path.home() / "Library/Caches/wechat-export"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (cache / "run.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportError("导出或持续同步正在运行，请先完成导出，或双击“停止持续同步.command”。") from None
        say("\n本次会退出微信、运行临时调试副本，并请你扫码登录一次；副本仍使用本机账号数据。")
        say("只读取你选择的账号。结果是未加密的 SQLite 数据库，不含图片/语音附件。")
        if args.setup_sync:
            from wechat_sync import HOME, configure
            from cipher_reader import library
            library()  # Fail before disrupting WeChat if the bundled runtime is missing.
            say("持续同步会把密钥保存到 macOS 登录钥匙串，后台只读收集本机消息；本工具不上传数据。")
        else:
            say("密钥不保存到文件；本工具不上传数据。微信登录本身仍需联网。")
        input("确认这是你自己的账号，准备开始时按回车；取消请按 Ctrl+C：")
        executable = info["CFBundleExecutable"]
        account = select_account(args.db_root)
        original_running = bool(app_pids(executable, app=app))
        quit_app(app, executable)
        workspace = Path(tempfile.mkdtemp(prefix="run-", dir=cache))
        debug_app = make_debug_copy(app, workspace)
        debug_binary = debug_app / "Contents/MacOS" / executable
        if app_pids(executable):
            raise ExportError("已有微信进程，请先退出后重试。")
        secret = capture(None, account, workspace, args.timeout, executable=debug_binary)
        if args.setup_sync:
            try:
                configure(account, secret, home=(args.sync_home or HOME).expanduser().resolve())
                original_running = True
                say("账号配置已保存；正在退出临时副本并打开正常微信。")
                result = 0
            finally:
                secret = None
        else:
            say("正在正常退出临时微信，以生成一致的数据库副本……")
            quit_app(debug_app, executable)
            if app_pids(executable):
                raise ExportError("微信仍在运行，无法创建静态快照。")
            snapshot = workspace / "snapshot"
            say("正在复制本地数据库……")
            snapshot_databases(account, snapshot)
            try:
                result = export_all(snapshot, output, secret, info.get("CFBundleShortVersionString", "未知"))
            finally:
                secret = None  # Python cannot guarantee secure erasure of immutable bytes.
    except (ExportError, OSError, subprocess.TimeoutExpired) as exc:
        say(f"\n未完成：{exc}")
        result = 1
    except (KeyboardInterrupt, EOFError):
        say("\n已取消，正在清理本次临时文件……")
        result = 130
    finally:
        safe_to_remove = True
        if debug_app and info:
            executable = info["CFBundleExecutable"]
            if app_pids(executable, app=debug_app):
                try:
                    quit_app(debug_app, executable)
                except (Exception, KeyboardInterrupt):
                    safe_to_remove = False
                    result = 1
                    say(f"临时微信仍在运行，请手动退出后删除：{workspace}")
        if workspace and safe_to_remove:
            shutil.rmtree(workspace, ignore_errors=True)
        if original_running and app and safe_to_remove:
            if not app_pids(info["CFBundleExecutable"], app=app):
                opened = subprocess.run(["/usr/bin/open", "-n", str(app)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if opened.returncode:
                    result = 1
                    say("未能重新打开正式微信，请手动从应用程序目录启动。")
        if lock:
            lock.close()
    return result


if __name__ == "__main__":
    sys.exit(main())
