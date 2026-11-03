"""Read-only, bounded live polling with transactional local checkpoints and change feed."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from cipher_reader import Connection, decode_content
from db_export import ExportError
from message_query import APP_ID, prepare_search

MESSAGE_TABLE = re.compile(r"Msg_[0-9a-fA-F]{32}\Z")
MESSAGE_FILE = re.compile(r"(?:biz_)?message_\d+\.db\Z")
MESSAGE_METADATA = {"TimeStamp", "wcdb_builtin_compression_record", "Name2Id"}
BATCH = 500
TAIL = 50
RECONCILE_SECONDS = 600


def json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def packed(row):
    return json.dumps({k: json_value(v) for k, v in row.items()}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def fingerprint(path):
    stat = path.stat()
    with path.open("rb") as source:
        header = source.read(16)
    # Encrypted DB's salt is stable across checkpoints, and changes on recreation.
    return hashlib.sha256(f"{stat.st_dev}:{stat.st_ino}:".encode() + header).hexdigest()


class Source:
    def __init__(self, path, secret):
        self.path, self.generation = path, fingerprint(path)
        self.db = Connection(path, secret)
        self.version = None
        self.backlog = True
        self.names = {}
        self.conversations = {}

    def refresh_names(self):
        names = self.db.execute("SELECT rowid AS id, user_name FROM Name2Id")
        self.names = {r["id"]: r["user_name"] for r in names if isinstance(r["user_name"], str)}
        self.conversations = {"Msg_" + hashlib.md5(name.encode()).hexdigest(): name
                              for name in self.names.values()}

    def close(self):
        self.db.close()


class SyncEngine:
    def __init__(self, account, output, secret, *, batch=BATCH, reconcile_seconds=RECONCILE_SECONDS):
        self.account = Path(account).resolve()
        self.output = Path(output).resolve()
        if self.output == self.account or self.account in self.output.parents:
            raise ExportError("同步结果必须保存在微信账号目录之外。")
        self.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not self.output.exists():
            with self.output.open("xb"):
                pass
            self.output.chmod(0o600)
        self.store = sqlite3.connect(self.output, timeout=5)
        self.store.row_factory = sqlite3.Row
        identifier = self.store.execute("PRAGMA application_id").fetchone()[0]
        tables = self.store.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if identifier not in (0, APP_ID) or (identifier == 0 and tables):
            self.store.close()
            raise ExportError("结果路径已有其他数据库，未覆盖；请使用本工具专用的结果目录。")
        self.output.chmod(0o600)
        self.store.execute(f"PRAGMA application_id={APP_ID}")
        # Keep our output in DELETE mode: system SQLite may predate the WAL-reset fix.
        self.store.execute("PRAGMA journal_mode=DELETE")
        self.store.execute("PRAGMA synchronous=FULL")
        self.store.executescript("""
          CREATE TABLE IF NOT EXISTS sources (
            source TEXT PRIMARY KEY, generation TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS checkpoints (
            source TEXT, generation TEXT, table_name TEXT, high_id INTEGER NOT NULL DEFAULT -1,
            scan_id INTEGER NOT NULL DEFAULT -1, next_scan REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(source,generation,table_name));
          CREATE TABLE IF NOT EXISTS contacts (
            username TEXT PRIMARY KEY, display_name TEXT, nickname TEXT, remark TEXT);
          CREATE TABLE IF NOT EXISTS messages (
            source TEXT, generation TEXT, table_name TEXT, local_id INTEGER,
            server_id INTEGER, conversation TEXT, sender TEXT, create_time INTEGER,
            message_type INTEGER, content TEXT, decode_status TEXT, raw_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            first_seen REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(source,generation,table_name,local_id));
          CREATE TABLE IF NOT EXISTS changes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL,
            operation TEXT NOT NULL, source TEXT, generation TEXT, table_name TEXT,
            local_id INTEGER, payload_json TEXT NOT NULL);
          CREATE VIEW IF NOT EXISTS current_messages AS
            SELECT m.*, COALESCE(c.display_name, m.sender) AS sender_name,
                   COALESCE(g.display_name, m.conversation) AS conversation_name
            FROM messages m JOIN sources s ON m.source=s.source AND m.generation=s.generation
            LEFT JOIN contacts c ON c.username=m.sender
            LEFT JOIN contacts g ON g.username=m.conversation WHERE m.deleted=0;
        """)
        prepare_search(self.store)
        self.secret, self.batch, self.reconcile_seconds = secret, batch, reconcile_seconds
        self.sources = {}
        self.last_contacts = 0
        self.contact_error = None
        self.closed = False

    def open_source(self, path):
        key = str(path.relative_to(self.account))
        generation = fingerprint(path)
        source = self.sources.get(key)
        if source is None or source.generation != generation:
            if source:
                source.close()
                del self.sources[key]
            source = Source(path, self.secret)
            self.sources[key] = source
        return key, source

    def refresh_contacts(self, now):
        if now - self.last_contacts < 60:
            return
        path = self.account / "contact/contact.db"
        if not path.exists():
            return
        self.last_contacts = now
        try:
            with Connection(path, self.secret) as db:
                rows = db.execute("SELECT username, nick_name, remark FROM contact")
            with self.store:
                for row in rows:
                    username = row["username"]
                    if not isinstance(username, str):
                        continue
                    nickname, _ = decode_content(row["nick_name"])
                    remark, _ = decode_content(row["remark"])
                    self.store.execute("INSERT OR REPLACE INTO contacts VALUES(?,?,?,?)",
                                       (username, remark or nickname or username, nickname, remark))
            self.contact_error = None
        except (ExportError, OSError) as exc:
            self.contact_error = str(exc)

    def record(self, key, source, table, row, now):
        raw = packed(row)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        identity = (key, source.generation, table, row["local_id"])
        old = self.store.execute("SELECT fingerprint, deleted FROM messages WHERE source=? AND generation=?"
                                 " AND table_name=? AND local_id=?", identity).fetchone()
        if old is not None and old["fingerprint"] == digest and not old["deleted"]:
            return 0
        value = row.get("message_content")
        if value in (None, "", b""):
            value = row.get("compress_content", value)
        content, status = decode_content(value)
        payload = {
            "source": key, "generation": source.generation, "table_name": table,
            "local_id": row["local_id"], "server_id": row.get("server_id"),
            "conversation": source.conversations.get(table),
            "sender": source.names.get(row.get("real_sender_id")),
            "create_time": row.get("create_time"), "message_type": row.get("local_type"),
            "content": content, "decode_status": status, "raw": json.loads(raw),
        }
        self.store.execute("""INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
            ON CONFLICT(source,generation,table_name,local_id) DO UPDATE SET
            server_id=excluded.server_id, conversation=excluded.conversation, sender=excluded.sender,
            create_time=excluded.create_time, message_type=excluded.message_type,
            content=excluded.content, decode_status=excluded.decode_status, raw_json=excluded.raw_json,
            fingerprint=excluded.fingerprint, deleted=0, updated_at=excluded.updated_at""",
            (*identity, payload["server_id"], payload["conversation"], payload["sender"],
             payload["create_time"], payload["message_type"], content, status, raw, digest, now, now))
        operation = "insert" if old is None else "restore" if old["deleted"] else "update"
        self.store.execute("INSERT INTO changes(observed_at,operation,source,generation,table_name,local_id,"
                           "payload_json) VALUES(?,?,?,?,?,?,?)",
                           (now, operation, *identity, json.dumps(payload, ensure_ascii=False)))
        return 1

    def checkpoint(self, key, source, table):
        identity = (key, source.generation, table)
        self.store.execute("INSERT OR IGNORE INTO checkpoints(source,generation,table_name) VALUES(?,?,?)", identity)
        return identity, self.store.execute("SELECT * FROM checkpoints WHERE source=? AND generation=? AND table_name=?",
                                            identity).fetchone()

    def read_batch(self, key, source, table, now, *, reconcile=False):
        # Both SELECTs and generation checks are bounded; release source read locks per table.
        source.db.execute("BEGIN")
        try:
            with self.store:
                identity, cursor = self.checkpoint(key, source, table)
                low = cursor["scan_id" if reconcile else "high_id"]
                rows = source.db.execute(f'SELECT * FROM "{table}" WHERE local_id>? ORDER BY local_id LIMIT ?',
                                         (low, self.batch))
                changed = sum(self.record(key, source, table, row, now) for row in rows)
                end = rows[-1]["local_id"] if rows else low
                if reconcile:
                    # Only mark deletions inside the range completely examined in this snapshot.
                    high = end if len(rows) == self.batch else (1 << 63) - 1
                    existing = self.store.execute("SELECT local_id FROM messages WHERE source=? AND generation=?"
                        " AND table_name=? AND local_id>? AND local_id<=? AND deleted=0", (*identity, low, high)).fetchall()
                    found = {r["local_id"] for r in rows}
                    for old in existing:
                        if old[0] not in found:
                            item = (*identity, old[0])
                            self.store.execute("UPDATE messages SET deleted=1, updated_at=? WHERE source=? AND generation=?"
                                               " AND table_name=? AND local_id=?", (now, *item))
                            self.store.execute("INSERT INTO changes(observed_at,operation,source,generation,table_name,"
                                               "local_id,payload_json) VALUES(?,'delete',?,?,?,?,?)",
                                               (now, *item, json.dumps({"local_id": old[0]})))
                            changed += 1
                    done = len(rows) < self.batch
                    self.store.execute("UPDATE checkpoints SET scan_id=?,next_scan=? WHERE source=? AND generation=?"
                                       " AND table_name=?", (-1 if done else end,
                                                             now + self.reconcile_seconds if done else now, *identity))
                else:
                    # Recent updates/revocations generally retain local_id, so re-read a tail.
                    tail = source.db.execute(f'SELECT * FROM "{table}" ORDER BY local_id DESC LIMIT ?', (TAIL,))
                    changed += sum(self.record(key, source, table, row, now) for row in tail)
                    self.store.execute("UPDATE checkpoints SET high_id=? WHERE source=? AND generation=? AND table_name=?",
                                       (end, *identity))
                if fingerprint(source.path) != source.generation:
                    raise ExportError("读取期间数据库被替换，已回滚本批，下一轮重新连接。")
                # End source transaction before committing its associated local progress.
                source.db.execute("COMMIT")
            return changed, len(rows) == self.batch
        except BaseException:
            try:
                source.db.execute("ROLLBACK")
            except ExportError:
                pass
            raise

    def poll(self, stop=lambda: False):
        now = time.time()
        errors, changed, tables_by_source = [], 0, {}
        self.refresh_contacts(now)
        files = sorted(p for p in (self.account / "message").glob("*.db") if MESSAGE_FILE.fullmatch(p.name))
        if not files:
            errors.append({"source": "message", "error": "未找到消息数据库，等待微信同步或检查初始化账号。"})
        active = {str(p.relative_to(self.account)) for p in files}
        for key in list(self.sources):
            if key not in active:
                self.sources.pop(key).close()
        for path in files:
            if stop():
                break
            key = str(path.relative_to(self.account))
            try:
                key, source = self.open_source(path)
                version = source.db.execute("PRAGMA data_version")[0]["data_version"]
                names = {r["name"] for r in source.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                tables = sorted(name for name in names if MESSAGE_TABLE.fullmatch(name))
                tables_by_source[key] = set(tables)
                if not tables:
                    unknown = {name for name in names if name not in MESSAGE_METADATA and not name.startswith("sqlite_")}
                    if unknown:
                        errors.append({"source": key, "error": "没有识别到受支持的 Msg_ 会话表。"})
                    else:
                        # A newly created shard can contain only WCDB metadata.
                        # Continue checking its schema so the first chat is picked up later.
                        with self.store:
                            self.store.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (key, source.generation))
                        source.version, source.backlog = version, False
                    continue
                with self.store:
                    self.store.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (key, source.generation))
                if version != source.version or source.backlog:
                    source.refresh_names()
                    backlog = False
                    for table in tables:
                        if stop():
                            backlog = True
                            break
                        count, more = self.read_batch(key, source, table, now)
                        changed += count
                        backlog = backlog or more
                    source.version, source.backlog = version, backlog
            except (ExportError, OSError) as exc:
                errors.append({"source": key, "error": str(exc)})
                if key in self.sources:
                    self.sources.pop(key).close()
        # Round-robin bounded reconciliation finds older updates, low-id backfills and deletions.
        scans = self.store.execute("SELECT c.* FROM checkpoints c JOIN sources s USING(source,generation)"
                                   " WHERE next_scan<=? ORDER BY next_scan,source,table_name LIMIT 16", (now,)).fetchall()
        for cursor in scans:
            if stop():
                break
            key, table = cursor["source"], cursor["table_name"]
            source = self.sources.get(key)
            if source is None or table not in tables_by_source.get(key, set()):
                continue
            try:
                count, _ = self.read_batch(key, source, table, now, reconcile=True)
                changed += count
            except (ExportError, OSError) as exc:
                errors.append({"source": key, "error": str(exc)})
        if self.contact_error:
            errors.append({"source": "contact/contact.db", "error": self.contact_error})
        total = self.store.execute("SELECT count(*) FROM current_messages").fetchone()[0]
        event = self.store.execute("SELECT COALESCE(MAX(sequence),0) FROM changes").fetchone()[0]
        return {"last_poll": time.time(), "messages": total, "change_sequence": event,
                "changes_this_poll": changed, "sources": len(files),
                "catching_up": any(s.backlog for s in self.sources.values()), "errors": errors}

    def close(self):
        if not self.closed:
            for source in self.sources.values():
                source.close()
            self.store.close()
            self.secret = None
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
