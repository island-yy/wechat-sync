#!/usr/bin/env python3
"""One-click controller and detached local worker for continuous WeChat collection."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from cipher_reader import Connection, library
from db_export import ExportError
from keychain_store import Keychain, account_id
from local_api import LocalAPI
from message_query import Messages, integer
from sync_engine import MESSAGE_FILE, MESSAGE_TABLE, SyncEngine
from runtime_support import (WINDOWS, background_options, cache_home, data_home,
                             disable_core_dumps, file_lock, init_hint, private_directory)
from runtime_support import json_access, replace_file

HOME = data_home()
EXPORT_CACHE = cache_home()
SCRIPT = Path(__file__).resolve()
VERSION = "0.4.1"
_children = {}


def write_json(path, value):
    with json_access(path):
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            replace_file(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

def read_json(path):
    try:
        with json_access(path):
            return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def lock(home, name="worker.lock"):
    return file_lock(home / name)


def running(home):
    handle = lock(home)
    if handle is None:
        return True
    handle.close()
    return False


def config(home):
    value = read_json(home / "config.json")
    if not value or value.get("format") != 1:
        raise ExportError("尚未初始化，请" + init_hint() + "。")
    account = Path(value["account"]).resolve()
    if value.get("keychain_account") != account_id(account):
        raise ExportError("账号配置与钥匙串标识不一致，请重新初始化。")
    output = Path(value["output"]).resolve()
    if output == account or account in output.parents:
        raise ExportError("结果目录不能位于微信账号目录中。")
    if not 1 <= value.get("interval", 0) <= 3600:
        raise ExportError("同步间隔无效，请重新初始化。")
    value["api_port"] = integer(value.get("api_port", 8765), "port", 0, 65535)
    return value


def verify_live(account, secret):
    library()
    paths = sorted(p for p in (Path(account) / "message").glob("*.db") if MESSAGE_FILE.fullmatch(p.name))
    if not paths:
        raise ExportError("所选账号没有受支持的消息库，请先让微信同步消息。")
    verified = 0
    for path in paths:
        with Connection(path, secret) as db:
            tables = db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            supported = [r["name"] for r in tables if MESSAGE_TABLE.fullmatch(r["name"])]
            for name in supported[:1]:
                db.execute(f'SELECT local_id, server_id, message_content FROM "{name}" LIMIT 1')
                db.execute("SELECT rowid, user_name FROM Name2Id LIMIT 1")
                verified += 1
    if not verified:
        raise ExportError("没有识别到受支持的消息表，尚不能启用持续同步。")
    return verified


def configure(account, secret, *, home=HOME, interval=3):
    # Called by the interactive capture workflow, before closing the live temporary client.
    home = Path(home).resolve()
    account = Path(account).resolve()
    if home == account or account in home.parents:
        raise ExportError("同步程序和结果必须保存在微信账号目录之外。")
    private_directory(home)
    guard = lock(home, "launch.lock")
    if guard is None:
        raise ExportError("持续同步正在运行，请先停止后再初始化。")
    with guard:
        if running(home):
            raise ExportError("持续同步正在运行，请先停止后再初始化。")
        count = verify_live(account, secret)
        identifier = account_id(account)
        Keychain().put(identifier, secret)
        value = {"format": 1, "account": str(account), "keychain_account": identifier,
                 "output": str(home / "accounts" / identifier[:16] / "messages.sqlite3"),
                 "interval": interval, "initialized_at": time.time()}
        write_json(home / "config.json", value)
        store = "Windows 凭据管理器" if WINDOWS else "登录钥匙串"
        print(f"已验证 {count} 个消息库的只读连接，密钥已保存到{store}。", flush=True)
        return value


def stop(home=HOME, timeout=30):
    deadline = time.monotonic() + timeout
    guard = lock(home, "launch.lock")
    while guard is None:
        if time.monotonic() >= deadline:
            raise ExportError("同步正在启动或初始化，请稍后停止。")
        time.sleep(0.2)
        guard = lock(home, "launch.lock")
    try:
        return stop_worker(home, deadline)
    finally:
        guard.close()


def stop_worker(home, deadline):
    while running(home):
        (home / "stop.request").touch(mode=0o600)
        if time.monotonic() >= deadline:
            raise ExportError("正在等待当前批次结束，停止请求已保留；稍后查看状态。没有强制终止进程。")
        time.sleep(0.2)
    for pid, process in list(_children.items()):
        if process.poll() is not None:
            process.wait()
            del _children[pid]
    print("持续同步已停止，读取进度已保留。", flush=True)


def start(home=HOME, port=None):
    private_directory(home)
    guard = lock(home, "launch.lock")
    if guard is None:
        raise ExportError("同步正在启动或初始化，请稍后检查状态。")
    # Serialize launchers. The worker owns its own lock before acknowledging startup;
    # Windows cannot use POSIX pass_fds/flock inheritance.
    process = None
    try:
        if running(home):
            print("持续同步已在运行。", flush=True)
            show_status(home)
            return
        value = config(home)
        if port is not None:
            value["api_port"] = integer(port, "port", 0, 65535)
            write_json(home / "config.json", value)
        secret = Keychain().get(value["keychain_account"])
        if len(secret) != 32:
            raise ExportError("保存的密钥格式无效，请重新初始化。")
        verify_live(value["account"], secret)
        (home / "stop.request").unlink(missing_ok=True)
        instance = uuid.uuid4().hex
        logfile = home / "worker.log"
        if logfile.exists() and logfile.stat().st_size > 128 * 1024:
            replace_file(logfile, home / "worker.previous.log")
        with logfile.open("ab", buffering=0) as log:
            process = subprocess.Popen([
                sys.executable, str(SCRIPT), "worker", "--home", str(home),
                "--instance", instance,
            ], stdin=subprocess.PIPE, stdout=log, stderr=log, cwd=home, **background_options())
            _children[process.pid] = process
            with process.stdin as pipe:
                pipe.write(secret)
            secret = None
        print("正在启动同步与查询服务；首次建立搜索索引需要稍等……", flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = read_json(home / "status.json")
            if status.get("instance") == instance and status.get("state") == "running":
                print("同步与查询服务已启动；可以关闭终端，正常使用微信。", flush=True)
                print("结果数据库：" + value["output"], flush=True)
                print("查询接口：" + status["api_url"], flush=True)
                return
            if process.poll() is not None:
                reason = "；".join(e.get("error", "") for e in status.get("errors", [])) if status.get("instance") == instance else ""
                raise ExportError("后台进程未能启动。" + (reason or "请查看同步状态和 worker.log。"))
            time.sleep(0.1)
        raise ExportError("后台进程启动超时，已请求停止；请检查同步状态。")
    except BaseException:
        if process is not None and process.poll() is None:
            if not process.stdin.closed:
                process.stdin.close()
            (home / "stop.request").touch(mode=0o600)
        raise
    finally:
        secret = None
        guard.close()


def worker(home, instance):
    stopped = False
    def request_stop(*_):
        nonlocal stopped
        stopped = True
    def should_stop():
        return stopped or (home / "stop.request").exists()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    guard = lock(home)
    if guard is None:
        raise ExportError("持续同步已在运行。")
    export_lock = None
    api = None
    status = {"state": "starting", "instance": instance, "pid": os.getpid(),
              "version": VERSION, "started_at": time.time()}
    result = 0
    try:
        value = config(home)
        secret = sys.stdin.buffer.read(32)
        if len(secret) != 32:
            raise ExportError("后台进程没有收到有效密钥。")
        if not WINDOWS:
            import fcntl
            EXPORT_CACHE.mkdir(parents=True, mode=0o700, exist_ok=True)
            export_lock = (EXPORT_CACHE / "run.lock").open("a")
            try:
                fcntl.flock(export_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ExportError("导出或初始化正在进行，请结束后再启动同步。") from None
        library()  # Load all bundled code before acknowledging startup; no runtime copy needed.
        write_json(home / "status.json", status)
        with SyncEngine(value["account"], value["output"], secret) as engine:
            secret = None
            api = LocalAPI(home, value["output"], value["api_port"], lambda: get_status(home))
            status.update(state="running", output=value["output"], api_url=api.url, errors=[])
            write_json(home / "status.json", status)
            last_errors = None
            while not should_stop():
                status.update(engine.poll(should_stop))
                write_json(home / "status.json", status)
                errors = status["errors"]
                if errors != last_errors:
                    # Only operational errors/counts, never message content or keys.
                    print(json.dumps({"time": time.time(), "errors": errors}, ensure_ascii=False), flush=True)
                    last_errors = errors
                deadline = time.monotonic() + value["interval"]
                while not should_stop() and time.monotonic() < deadline:
                    time.sleep(0.2)
        status["state"] = "stopped"
    except (ExportError, OSError, sqlite3.Error) as exc:
        status.update(state="error", errors=[{"error": str(exc)}])
        result = 1
    except BaseException as exc:
        status.update(state="error", errors=[{"error": "后台异常：" + type(exc).__name__}])
        result = 1
    finally:
        if api:
            api.close()
        status["stopped_at"] = time.time()
        write_json(home / "status.json", status)
        if export_lock:
            export_lock.close()
        guard.close()
    return result


def get_status(home=HOME):
    status = read_json(home / "status.json")
    status["running"] = running(home)
    status["initialized"] = (home / "config.json").exists()
    return status


def show_status(home=HOME):
    value, status = read_json(home / "config.json"), get_status(home)
    print("状态：" + ("运行中" if status["running"] else "已停止"), flush=True)
    if status.get("last_poll"):
        print("最近检查：" + datetime.fromtimestamp(status["last_poll"]).strftime("%Y-%m-%d %H:%M:%S"))
        print(f"已保存 {status.get('messages', 0)} 条当前消息；变更序号 {status.get('change_sequence', 0)}。")
        if status.get("catching_up"):
            print("正在分批补读历史消息，新消息会同时收集。")
    for error in status.get("errors", []):
        print("注意：" + error.get("source", "") + " " + error.get("error", ""))
    if value.get("output"):
        print("结果数据库：" + value["output"])
    if status.get("api_url") and status["running"]:
        print("查询接口：" + status["api_url"])
    print("运行日志：" + str(home / "worker.log"))


def export_jsonl(home=HOME, output=None):
    value = config(home)
    database = Path(value["output"])
    if not database.exists():
        raise ExportError("尚未生成消息数据库，请先启动持续同步。")
    destination = Path(output).expanduser().resolve() if output else (
        database.parent / ("messages-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl"))
    account = Path(value["account"]).resolve()
    if destination == account or account in destination.parents:
        raise ExportError("导出路径不能位于微信账号目录中。")
    # Snapshot our own output using SQLite backup, releasing its read lock quickly.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as source, \
            closing(sqlite3.connect(":memory:")) as snapshot:
        source.backup(snapshot)
        snapshot.row_factory = sqlite3.Row
        with destination.open("x", encoding="utf-8") as stream:
            destination.chmod(0o600)
            count = 0
            rows = snapshot.execute("SELECT * FROM current_messages ORDER BY create_time,source,table_name,local_id")
            for count, row in enumerate(rows, 1):
                item = dict(row)
                item["raw"] = json.loads(item.pop("raw_json"))
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"已导出 {count} 条消息：{destination}", flush=True)
    return destination


def forget_key(home):
    stop(home)
    Keychain().delete(config(home)["keychain_account"])
    print("已移除当前配置账号保存的密钥；已导出的消息仍保留。")


def main():
    parser = argparse.ArgumentParser(description="微信同步与查询（本地、只读；不带参数即启动）",
        epilog="示例：search '会议' --since 2026-10-01 --limit 20；contacts '名字'；status --json")
    parser.add_argument("command", nargs="?", default="start", choices=("init", "start", "restart", "stop", "status",
                        "search", "messages", "message", "contacts", "conversations", "changes", "api-info",
                        "export-jsonl", "forget-key", "worker"))
    parser.add_argument("query", nargs="?", help="搜索关键词、联系人名称或消息 id")
    parser.add_argument("--home", type=Path, default=HOME)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--db-root", type=Path, help="初始化时指定 xwechat_files 目录")
    parser.add_argument("--timeout", type=int, default=180, help="Windows 初始化等待秒数，30–900")
    parser.add_argument("--port", type=int, help="本机 HTTP 端口，默认 8765；配合 start/restart")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    for name in ("conversation", "sender", "since", "until", "type", "limit", "cursor", "offset", "after"):
        parser.add_argument("--" + name)
    parser.add_argument("--instance", help=argparse.SUPPRESS)
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    os.umask(0o077)
    disable_core_dumps()
    if WINDOWS and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    try:
        if sys.platform not in ("darwin", "win32") or (not WINDOWS and os.geteuid() == 0):
            raise ExportError("请以当前登录用户在 macOS 或 Windows 上运行本工具。")
        if args.command == "worker":
            if not args.instance or not re.fullmatch(r"[0-9a-f]{32}", args.instance):
                raise ExportError("后台进程必须由启动入口创建。")
            return worker(home, args.instance)
        if args.command in ("search", "messages", "message", "contacts", "conversations", "changes"):
            messages = Messages(config(home)["output"])
            if args.command == "message":
                result = messages.detail(args.query)
            else:
                if args.command == "search" and not args.query:
                    raise ValueError("请提供搜索关键词，例如 search '会议'")
                parameters = {name: getattr(args, name) for name in
                              ("conversation", "sender", "since", "until", "type", "limit", "cursor", "offset", "after")
                              if getattr(args, name) is not None}
                if args.query is not None:
                    parameters["q"] = args.query
                result = messages.query("messages" if args.command == "search" else args.command, parameters)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "api-info":
            state = get_status(home)
            print(json.dumps({"running": state["running"] and bool(state.get("api_url")),
                              "url": state.get("api_url"), "token_file": str(home / "api.token")}, ensure_ascii=False))
            return 0
        if args.command == "status" and args.json:
            print(json.dumps(get_status(home), ensure_ascii=False, indent=2))
            return 0
        if args.command == "restart":
            stop(home)
            args.command = "start"
        if args.command == "init" or (args.command == "start" and not (home / "config.json").exists()):
            if running(home):
                stop(home)
            if WINDOWS:
                from windows_init import initialize
                initialize(home, root=args.db_root, timeout=args.timeout)
            else:
                print("首次初始化需要扫码获取密钥；本次会将密钥保存到登录钥匙串。", flush=True)
                command = [sys.executable, str(SCRIPT.with_name("wechat_export.py")),
                           "--setup-sync", "--sync-home", str(home)]
                if args.db_root:
                    command.extend(["--db-root", str(args.db_root)])
                code = subprocess.call(command)
                if code:
                    return code
            args.command = "start"
        if args.command == "export-jsonl":
            export_jsonl(home, args.output)
        elif args.command == "start":
            start(home, args.port)
        else:
            {"stop": stop, "status": show_status, "forget-key": forget_key}[args.command](home)
        return 0
    except (ExportError, OSError, sqlite3.Error, KeyError, ValueError) as exc:
        print(f"未完成：{exc}", flush=True)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消。", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
