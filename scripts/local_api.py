"""Authenticated loopback-only JSON API. No request URLs or chat contents are logged."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from db_export import ExportError
from message_query import Messages


def api_token(home):
    path = home / "api.token"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        token = path.read_text().strip()
    else:
        token = secrets.token_hex(32)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ExportError("api.token 格式无效，请移走该文件后重新启动。")
    path.chmod(0o600)
    return token


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatLocal/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            port = self.server.server_port
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}") or self.headers.get("Origin"):
                return self.send_json(403, {"error": "仅支持本机程序访问"})
            authorization = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(authorization, self.server.authorization):
                return self.send_json(401, {"error": "需要有效的 Bearer token"})
            if len(self.path) > 4096:
                raise ValueError("请求地址过长")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ValueError("请使用相对接口路径")
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            if any(len(v) != 1 for v in query.values()):
                raise ValueError("查询参数不能重复")
            parameters = {k: v[0] for k, v in query.items()}
            resource = parsed.path.removeprefix("/v1/")
            if not parsed.path.startswith("/v1/"):
                raise KeyError(parsed.path)
            if resource == "status":
                if parameters:
                    raise ValueError("status 不接受查询参数")
                result = self.server.status()
            elif resource.startswith("messages/"):
                if parameters:
                    raise ValueError("消息详情不接受查询参数")
                result = self.server.messages.detail(resource.split("/", 1)[1])
            else:
                result = self.server.messages.query(resource, parameters)
            self.send_json(200, result)
        except KeyError:
            self.send_json(404, {"error": "接口或消息不存在"})
        except (ValueError, UnicodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.OperationalError:
            self.send_json(503, {"error": "查询繁忙或超时，请缩小时间或会话范围后重试"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (sqlite3.Error, ExportError, OSError):
            self.send_json(503, {"error": "消息库暂不可读，请检查同步状态"})

    def do_POST(self):
        self.send_json(405, {"error": "此接口仅支持 GET 查询"})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST


class LocalAPI:
    def __init__(self, home, database, port, status):
        token = api_token(home)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise ExportError(f"无法监听本机端口 {port}；可用 restart --port 8766 更换端口。{exc}") from exc
        self.server.messages = Messages(database)
        self.server.authorization = ("Bearer " + token).encode()
        self.server.status = status
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
