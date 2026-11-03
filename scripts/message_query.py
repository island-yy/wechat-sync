"""One read-only query layer shared by the CLI and loopback HTTP API."""
import base64
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from db_export import ExportError

APP_ID = 0x57435359
JOIN = """FROM messages m JOIN sources s USING(source,generation)
          LEFT JOIN contacts c ON c.username=m.sender
          LEFT JOIN contacts g ON g.username=m.conversation"""
FIELDS = """m.rowid AS id, m.local_id, m.server_id, m.conversation,
            COALESCE(g.display_name,m.conversation) AS conversation_name,
            m.sender, COALESCE(c.display_name,m.sender) AS sender_name,
            m.create_time, m.message_type, m.content, m.decode_status"""


def prepare_search(db):
    """Migrate only our output. Triggers keep the external-content index transactional."""
    db.execute("BEGIN")
    try:
        for name, columns in (("time", "COALESCE(create_time,0) DESC"),
                              ("conversation", "conversation,create_time DESC"),
                              ("sender", "sender,create_time DESC")):
            db.execute(f"CREATE INDEX IF NOT EXISTS messages_{name} ON messages({columns})")
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone()
        if not exists:
            try:
                db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', "
                           "content_rowid='rowid', tokenize='trigram')")
            except sqlite3.OperationalError as exc:
                if "no such module" not in str(exc) and "no such tokenizer" not in str(exc):
                    raise
            else:
                db.execute("""CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid,content) VALUES(new.rowid,new.content); END""")
                db.execute("""CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,content)
                    VALUES('delete',old.rowid,old.content); END""")
                db.execute("""CREATE TRIGGER messages_fts_update AFTER UPDATE OF content ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,content)
                    VALUES('delete',old.rowid,old.content);
                    INSERT INTO messages_fts(rowid,content) VALUES(new.rowid,new.content); END""")
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        db.execute("PRAGMA user_version=2")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def integer(value, name, low=0, high=(1 << 63) - 1):
    try:
        result = int(str(value))
        if low <= result <= high:
            return result
    except (ValueError, TypeError):
        pass
    raise ValueError(f"{name} 必须是 {low}–{high} 的整数")


def timestamp(value):
    if str(value).isdigit():
        return integer(value, "时间")
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, OverflowError):
        raise ValueError("时间应为 Unix 秒或 ISO 日期时间，例如 2026-10-28 或 2026-10-28T09:00:00+08:00") from None


def keyword(value):
    value = str(value or "")
    if len(value) > 200 or "\x00" in value:
        raise ValueError("关键词最多 200 个字符，不能含空字符")
    return value


def like(value):
    return "%" + value.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


class Messages:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def connect(self):
        if not self.path.exists():
            raise ExportError("尚未生成消息库，请先启动同步。")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            db.close()
            raise ExportError("不是本工具的消息库。")
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
        return db

    def query(self, resource, parameters=None):
        p = {k: v for k, v in (parameters or {}).items() if v is not None}
        allowed = {
            "messages": {"q", "conversation", "sender", "since", "until", "type", "limit", "cursor"},
            "contacts": {"q", "limit", "offset"},
            "conversations": {"q", "limit", "offset"},
            "changes": {"after", "limit"},
        }
        if resource not in allowed:
            raise KeyError(resource)
        if p.keys() - allowed[resource]:
            raise ValueError("未知参数：" + ", ".join(sorted(p.keys() - allowed[resource])))
        limit = integer(p.get("limit", 50), "limit", 1, 200)
        q = keyword(p.get("q"))
        with closing(self.connect()) as db:
            if resource == "messages":
                return self.messages(db, p, q, limit)
            if resource == "changes":
                after = integer(p.get("after", 0), "after")
                rows = [dict(r) for r in db.execute("SELECT * FROM changes WHERE sequence>? ORDER BY sequence LIMIT ?",
                                                   (after, limit + 1))]
                for row in rows:
                    row["message"] = json.loads(row.pop("payload_json"))
                    row["message"].pop("raw", None)
                items = rows[:limit]
                return {"items": items, "has_more": len(rows) > limit,
                        "next_after": items[-1]["sequence"] if items else after}
            offset = integer(p.get("offset", 0), "offset", 0, 1000000)
            pattern = like(q)
            if resource == "contacts":
                sql = """SELECT username,display_name,nickname,remark FROM contacts
                    WHERE username LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!'
                    OR nickname LIKE ? ESCAPE '!' OR remark LIKE ? ESCAPE '!'
                    ORDER BY display_name,username LIMIT ? OFFSET ?"""
                values = (pattern, pattern, pattern, pattern, limit + 1, offset)
            else:
                sql = """SELECT m.conversation AS username, COALESCE(g.display_name,m.conversation) AS name,
                    count(*) AS messages, max(m.create_time) AS last_message_time """ + JOIN + """
                    WHERE m.deleted=0 AND m.conversation IS NOT NULL AND
                    (m.conversation LIKE ? ESCAPE '!' OR g.display_name LIKE ? ESCAPE '!')
                    GROUP BY m.conversation ORDER BY last_message_time DESC,m.conversation LIMIT ? OFFSET ?"""
                values = (pattern, pattern, limit + 1, offset)
            rows = [dict(r) for r in db.execute(sql, values)]
            return {"items": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    def messages(self, db, p, q, limit):
        conditions, values = ["m.deleted=0"], []
        indexed = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone())
        if q:
            if indexed and len(q) >= 3:
                conditions.append("m.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
                values.append('"' + q.replace('"', '""') + '"')
            else:
                conditions.append("m.content LIKE ? ESCAPE '!'")
                values.append(like(q))
        for name in ("conversation", "sender"):
            if p.get(name):
                conditions.append(f"m.{name}=?")
                values.append(keyword(p[name]))
        for name, operator in (("since", ">="), ("until", "<")):
            if name in p:
                conditions.append(f"m.create_time{operator}?")
                values.append(timestamp(p[name]))
        if "since" in p and "until" in p and timestamp(p["since"]) >= timestamp(p["until"]):
            raise ValueError("since 必须早于 until")
        if "type" in p:
            conditions.append("m.message_type=?")
            values.append(integer(p["type"], "type"))
        if p.get("cursor"):
            try:
                cursor = str(p["cursor"])
                if len(cursor) > 100:
                    raise ValueError
                point = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError
                point = [integer(x, "cursor") for x in point]
            except (ValueError, TypeError, UnicodeDecodeError):
                raise ValueError("无效的 cursor，请使用上次返回的 next_cursor") from None
            conditions.append("(COALESCE(m.create_time,0),m.rowid)<(?,?)")
            values.extend(point)
        sql = "SELECT " + FIELDS + " " + JOIN + " WHERE " + " AND ".join(conditions)
        rows = [dict(r) for r in db.execute(sql + " ORDER BY COALESCE(m.create_time,0) DESC,m.rowid DESC LIMIT ?",
                                           (*values, limit + 1))]
        items, cursor = rows[:limit], None
        if len(rows) > limit:
            last = items[-1]
            cursor = base64.urlsafe_b64encode(json.dumps([last["create_time"] or 0, last["id"]]).encode()).decode().rstrip("=")
        return {"items": items, "next_cursor": cursor}

    def detail(self, identifier):
        with closing(self.connect()) as db:
            row = db.execute("SELECT " + FIELDS + ",m.source,m.generation,m.table_name,m.raw_json " + JOIN +
                             " WHERE m.deleted=0 AND m.rowid=?", (integer(identifier, "id", 1),)).fetchone()
            if row is None:
                raise KeyError(identifier)
            item = dict(row)
            item["raw"] = json.loads(item.pop("raw_json"))
            return item
