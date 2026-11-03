"""Synthetic encrypted data only; never access a user's WeChat or saved credentials."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import cipher_reader as reader
from db_export import ExportError
from keychain_store import account_id
from message_query import Messages
from runtime_support import file_lock
from sync_engine import SyncEngine
import wechat_sync as controller
import windows_init
import runtime_support

try:
    from sqlcipher3 import dbapi2
    import zstandard
except ImportError:
    dbapi2 = zstandard = None

SECRET = bytes(range(32))
CHAT = "wxid_synthetic"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()


def source_fixture(root):
    account = root / "微信测试账号/db_storage"
    (account / "message").mkdir(parents=True)
    (account / "contact").mkdir()
    path = account / "message/message_0.db"
    with reader.Connection(path, SECRET, readonly=False) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE Name2Id(user_name TEXT)")
        db.execute("INSERT INTO Name2Id VALUES(?)", (CHAT,))
        db.execute(f'CREATE TABLE "{TABLE}" (local_id INTEGER PRIMARY KEY,server_id INTEGER,'
                   'real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,message_content)')
        db.execute(f'INSERT INTO "{TABLE}" VALUES(1,1001,1,1793664000,1,?)', ("你好，明天开会",))
    with reader.Connection(account / "contact/contact.db", SECRET, readonly=False) as db:
        db.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)")
        db.execute("INSERT INTO contact VALUES(?,?,?)", (CHAT, "联系人", "项目联系人"))
    return account, path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechat-synthetic-")
        self.root = Path(self.temporary.name)
        self.account, self.path = source_fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()


class SyncTests(Fixture):
    def test_empty_shard_becomes_a_chat_and_unknown_schema_still_errors(self):
        shard = self.account / "message/message_1.db"
        with reader.Connection(shard, SECRET, readonly=False) as db:
            db.execute("CREATE TABLE TimeStamp(value INTEGER)")
            db.execute("CREATE TABLE wcdb_builtin_compression_record(value INTEGER)")
        with SyncEngine(self.account, self.root / "empty-shard/messages.sqlite3", SECRET) as engine:
            state = engine.poll()
            self.assertEqual(state["errors"], [])
            self.assertFalse(state["catching_up"])
            with reader.Connection(shard, SECRET, readonly=False) as db:
                db.execute("CREATE TABLE Name2Id(user_name TEXT)")
                db.execute("INSERT INTO Name2Id VALUES(?)", (CHAT,))
                db.execute(f'CREATE TABLE "{TABLE}" (local_id INTEGER PRIMARY KEY,server_id INTEGER,'
                           'real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,message_content)')
                db.execute(f'INSERT INTO "{TABLE}" VALUES(1,1003,1,1793664002,1,?)', ("首条消息",))
            state = engine.poll()
            self.assertEqual(state["errors"], [])
            self.assertEqual(state["messages"], 2)
            unknown = self.account / "message/message_2.db"
            with reader.Connection(unknown, SECRET, readonly=False) as db:
                db.execute("CREATE TABLE unsupported_messages(content TEXT)")
            state = engine.poll()
            self.assertTrue(any("message_2.db" in item["source"] for item in state["errors"]))

    def test_status_replacement_after_a_brief_external_reader(self):
        path = self.root / "外部读取.json"
        controller.write_json(path, {"version": 1})
        ready = threading.Event()
        def observe():
            with path.open("r", encoding="utf-8") as source:
                ready.set()
                time.sleep(0.06)
                source.read()
        observer = threading.Thread(target=observe)
        observer.start()
        self.assertTrue(ready.wait(1))
        try:
            controller.write_json(path, {"version": 2})
        finally:
            observer.join(timeout=2)
        self.assertEqual(controller.read_json(path), {"version": 2})

    def test_status_replacement_with_a_reader_still_open(self):
        path = self.root / "共享状态.json"
        controller.write_json(path, {"version": 1})
        done, errors = threading.Event(), []
        def update():
            try:
                controller.write_json(path, {"version": 2})
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        with runtime_support.json_access(path), path.open("r", encoding="utf-8") as source:
            writer = threading.Thread(target=update)
            writer.start()
            if runtime_support.WINDOWS:
                self.assertFalse(done.wait(0.25))
            self.assertEqual(json.load(source), {"version": 1})
        writer.join(timeout=3)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(controller.read_json(path), {"version": 2})

    def test_windows_status_replace_retries_only_transient_sharing(self):
        path = self.root / "status.json"
        real_replace = os.replace
        calls = []
        def busy_once(source, destination):
            calls.append(source)
            if len(calls) == 1:
                error = PermissionError("synthetic sharing violation")
                error.winerror = 32
                raise error
            return real_replace(source, destination)
        with patch.object(runtime_support, "WINDOWS", True), \
                patch.object(controller, "json_access", return_value=contextlib.nullcontext()), \
                patch.object(runtime_support.os, "replace", side_effect=busy_once), \
                patch.object(runtime_support.time, "sleep"):
            controller.write_json(path, {"state": "running"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(controller.read_json(path), {"state": "running"})
        for winerror, attempts in ((5, 10), (87, 1)):
            denied = PermissionError("synthetic persistent failure")
            denied.winerror = winerror
            with patch.object(runtime_support, "WINDOWS", True), \
                    patch.object(controller, "json_access", return_value=contextlib.nullcontext()), \
                    patch.object(runtime_support.time, "sleep"), \
                    patch.object(runtime_support.os, "replace", side_effect=denied) as replacement:
                with self.assertRaises(PermissionError):
                    controller.write_json(path, {"state": "new"})
                self.assertEqual(replacement.call_count, attempts)
        self.assertEqual(controller.read_json(path), {"state": "running"})

    def test_config_utf8_with_windows_legacy_default_encoding(self):
        path = self.root / "配置.json"
        value = {"account": "C:/用户/测试/微信数据", "state": "正在运行"}
        controller.write_json(path, value)
        original_open = Path.open
        def legacy_default(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            if "b" in mode:
                return original_open(path, mode, buffering)
            if encoding in (None, "locale"):
                encoding = "cp936"
            return original_open(path, mode, buffering, encoding=encoding, errors=errors, newline=newline)
        with patch.object(Path, "open", legacy_default):
            self.assertEqual(controller.read_json(path), value)

    def test_key_rejection_and_readonly(self):
        with self.assertRaises(ExportError):
            reader.Connection(self.path, b"x" * 32)
        with reader.Connection(self.path, SECRET) as db:
            with self.assertRaises(ExportError):
                db.execute(f'DELETE FROM "{TABLE}"')

    def test_wal_commit_rollback_and_resume(self):
        output = self.root / "output/messages.sqlite3"
        with SyncEngine(self.account, output, SECRET, reconcile_seconds=0) as engine:
            self.assertEqual(engine.poll()["messages"], 1)
            with reader.Connection(self.path, SECRET, readonly=False) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("BEGIN")
                writer.execute(f'INSERT INTO "{TABLE}" VALUES(2,1002,1,1793664001,1,?)', ("未提交",))
                self.assertEqual(engine.poll()["messages"], 1)
                writer.execute("ROLLBACK")
                writer.execute(f'INSERT INTO "{TABLE}" VALUES(2,1002,1,1793664001,1,?)', ("已提交",))
                self.assertEqual(engine.poll()["messages"], 2)
                writer.execute(f'UPDATE "{TABLE}" SET message_content=? WHERE local_id=1', ("会议改期",))
                engine.poll()
        with SyncEngine(self.account, output, SECRET) as engine:
            self.assertEqual(engine.poll()["messages"], 2)
        results = Messages(output).query("messages", {"q": "会议改期"})
        self.assertEqual(len(results["items"]), 1)
        self.assertEqual(results["items"][0]["sender_name"], "项目联系人")
        changes = Messages(output).query("changes", {"after": "0"})
        self.assertIn("update", [c["operation"] for c in changes["items"]])

    def test_single_instance_lock(self):
        path = self.root / "lock"
        handle = file_lock(path)
        self.assertIsNotNone(handle)
        try:
            code = "from runtime_support import file_lock; from pathlib import Path; import sys; " \
                   "h=file_lock(Path(sys.argv[1])); sys.exit(3 if h else 0)"
            child = subprocess.run([sys.executable, "-c", code, str(path)], cwd=SCRIPTS)
            self.assertEqual(child.returncode, 0)
        finally:
            handle.close()
        with file_lock(path):
            pass

    def test_worker_lifecycle_and_authenticated_api(self):
        home = self.root / "state"
        identifier = account_id(self.account)
        output = home / "accounts" / identifier[:16] / "messages.sqlite3"
        controller.write_json(home / "config.json", {"format": 1, "account": str(self.account),
            "keychain_account": identifier, "output": str(output), "interval": 1, "api_port": 0})
        instance = uuid.uuid4().hex
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "wechat_sync.py"), "worker",
            "--home", str(home), "--instance", instance], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        proc.stdin.write(SECRET)
        proc.stdin.close()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = controller.get_status(home)
                if state.get("last_poll"):
                    break
                if proc.poll() is not None:
                    self.fail("Synthetic worker exited before its first poll")
                time.sleep(0.05)
            else:
                self.fail("Synthetic worker did not become ready")
            self.assertEqual(state["errors"], [])
            token = (home / "api.token").read_text().strip()
            address = state["api_url"] + "/v1/messages?" + urlencode({"q": "你好"})
            with self.assertRaises(HTTPError) as error:
                urlopen(address)
            self.assertEqual(error.exception.code, 401)
            request = Request(address, headers={"Authorization": "Bearer " + token})
            with urlopen(request) as response:
                self.assertEqual(len(json.load(response)["items"]), 1)
            request.add_header("Origin", "https://example.invalid")
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            duplicate = subprocess.run([sys.executable, str(SCRIPTS / "wechat_sync.py"), "worker",
                "--home", str(home), "--instance", uuid.uuid4().hex], input=b"",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(controller.get_status(home)["instance"], instance)
        finally:
            (home / "stop.request").touch()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log = proc.stdout.read()
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(controller.running(home))
        self.assertNotIn(SECRET.hex().encode(), log)
        self.assertNotIn("你好".encode(), log)
        self.assertEqual(controller.get_status(home)["state"], "stopped")


@unittest.skipUnless(dbapi2 is not None, "Windows SQLCipher wheel is not installed")
class WindowsReaderTests(Fixture):
    def test_windows_adapter_reads_encrypted_wal_and_rejects_writes(self):
        with patch.object(reader, "library", return_value=dbapi2):
            with reader.WindowsConnection(self.path, SECRET) as db:
                self.assertEqual(db.execute(f'SELECT message_content FROM "{TABLE}"')[0]["message_content"],
                                 "你好，明天开会")
                with self.assertRaises(ExportError):
                    db.execute(f'DELETE FROM "{TABLE}"')
                self.assertEqual(db.execute("SELECT CAST(x'ff' AS TEXT) AS content"), [{"content": b"\xff"}])
            with self.assertRaises(ExportError):
                reader.WindowsConnection(self.path, b"x" * 32)
            with reader.WindowsConnection(self.path, SECRET, readonly=False) as writer, \
                    reader.WindowsConnection(self.path, SECRET) as db:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("BEGIN")
                writer.execute(f'INSERT INTO "{TABLE}" VALUES(2,1002,1,1793664001,1,?)', ("事务",))
                self.assertEqual(len(db.execute(f'SELECT * FROM "{TABLE}"')), 1)
                writer.execute("COMMIT")
                self.assertEqual(len(db.execute(f'SELECT * FROM "{TABLE}"')), 2)

    def test_same_engine_with_windows_reader(self):
        output = self.root / "Windows结果/messages.sqlite3"
        with patch.object(reader, "sys", types.SimpleNamespace(platform="win32")), \
                patch.object(reader, "library", return_value=dbapi2):
            with SyncEngine(self.account, output, SECRET) as engine:
                state = engine.poll()
                self.assertEqual(state["errors"], [])
                self.assertEqual(state["messages"], 1)
        self.assertEqual(len(Messages(output).query("messages", {"q": "你好"})["items"]), 1)

    def test_zstd_limit_and_unicode(self):
        with patch.object(reader, "sys", types.SimpleNamespace(platform="win32")):
            raw = zstandard.ZstdCompressor().compress("中文压缩内容".encode())
            self.assertEqual(reader.decode_content(raw), ("中文压缩内容", "zstd"))
            huge = zstandard.ZstdCompressor().compress(b"a" * (17 * 1024 * 1024))
            self.assertEqual(reader.decode_content(huge), (None, "zstd_too_large"))
            self.assertIsNone(reader.decode_content(b"\x28\xb5\x2f\xfd\x00")[0])


class WindowsCaptureTests(Fixture):
    def device(self, values):
        class Script:
            def on(self, _, callback): self.callback = callback
            def load(self):
                self.callback({"type": "send", "payload": {"kind": "ready"}}, None)
                for value in values:
                    self.callback({"type": "send", "payload": {"kind": "candidate", "value": value}}, None)
        class Session:
            detached = False
            def on(self, *_): pass
            def create_script(self, _): return Script()
            def detach(self): self.detached = True
        session = Session()
        return types.SimpleNamespace(attach=lambda _: session), session

    def test_verified_candidate_and_no_key_logging(self):
        device, session = self.device(["not-a-key", (b"x" * 32).hex(), SECRET.hex()])
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            key = windows_init.capture(device, 12345, [self.path], 2)
        self.assertEqual(key, SECRET)
        self.assertTrue(session.detached)
        self.assertNotIn(SECRET.hex(), captured.getvalue())

    def test_mismatched_candidate_does_not_succeed(self):
        device, session = self.device([(b"x" * 32).hex()])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ExportError):
            windows_init.capture(device, 12345, [self.path], 0.2)
        self.assertTrue(session.detached)

    def test_process_selection_and_custom_data_root(self):
        output = '"Weixin.exe","123","Console","1","10,000 K"\n' \
                 '"Weixin.exe","456","Console","1","40,000 K"\n' \
                 '"Other.exe","789","Console","1","30,000 K"\n'
        self.assertEqual(windows_init.parse_processes(output), [456])
        self.assertEqual(windows_init.account_paths(self.account.parents[1]), [self.account.resolve()])
        with self.assertRaises(ExportError):
            windows_init.account_paths(self.root / "absent")

    def test_launcher_exit_then_main_process_and_no_retry_after_ready_failure(self):
        frida = types.SimpleNamespace(**{
            name: type(name, (Exception,), {}) for name in (
                "ProcessNotFoundError", "PermissionDeniedError", "InvalidOperationError",
                "TransportError", "TimedOutError", "NotSupportedError", "ProcessNotRespondingError",
            )
        })
        with patch.object(windows_init, "processes", side_effect=[[123], [456]]), \
                patch.object(windows_init, "capture", side_effect=[
                    windows_init.CaptureTargetExited("launcher"), SECRET]) as capture:
            self.assertEqual(windows_init.capture_started_wechat(None, "test", [self.path], 1, frida), SECRET)
            self.assertEqual([call.args[1] for call in capture.call_args_list], [123, 456])
        with patch.object(windows_init, "processes", return_value=[456]), \
                patch.object(windows_init, "capture", side_effect=ExportError("capture failed")) as capture:
            with self.assertRaises(ExportError):
                windows_init.capture_started_wechat(None, "test", [self.path], 1, frida)
            self.assertEqual(capture.call_count, 1)


@unittest.skipUnless(sys.platform == "win32", "Requires native Windows credential and DACL APIs")
class WindowsNativeTests(unittest.TestCase):
    def test_isolated_credential_roundtrip_and_private_directory(self):
        from windows_credentials import WindowsCredentials, current_sid, current_username, protect_directory
        self.assertTrue(current_sid().startswith("S-1-"))
        self.assertIn("\\", current_username())
        with tempfile.TemporaryDirectory() as directory:
            protect_directory(directory)
            target = "synthetic-test-" + uuid.uuid4().hex
            store = WindowsCredentials()
            try:
                store.put(target, SECRET)
                self.assertEqual(store.get(target), SECRET)
                store.put(target, b"x" * 32)
                self.assertEqual(store.get(target), b"x" * 32)
            finally:
                store.delete(target)
            with self.assertRaises(ExportError):
                store.get(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
