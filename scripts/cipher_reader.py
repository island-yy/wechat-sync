"""Small SQLCipher C API binding. Secrets never travel through argv or SQL text."""
from __future__ import annotations

import ctypes as C
from pathlib import Path
import sys

from db_export import ExportError

LIBRARY = Path(__file__).resolve().parent / "native/libwechat_reader.dylib"
_library = None


class DatabaseError(ExportError):
    def __init__(self, code):
        self.code = code
        hint = {5: "数据库忙，稍后重试", 6: "数据库被锁定，稍后重试",
                8: "只读连接拒绝写入", 14: "无法打开数据库，请检查路径和访问权限",
                26: "密钥或加密格式不匹配，请重新初始化"}.get(code & 255, "数据库格式或查询不兼容")
        super().__init__(f"{hint}（SQLite {code}）")


def library():
    global _library
    if _library is not None:
        return _library
    if sys.platform == "win32":
        try:
            from sqlcipher3 import dbapi2
            import zstandard
        except ImportError:
            raise ExportError("缺少 Windows 依赖，请先运行 windows/setup.ps1。") from None
        _library = dbapi2
        return _library
    try:
        lib = C.CDLL(str(LIBRARY))
    except OSError:
        raise ExportError("缺少或无法加载随包提供的数据库组件，请重新解压完整安装包。") from None
    signatures = {
        "sqlite3_open_v2": ([C.c_char_p, C.POINTER(C.c_void_p), C.c_int, C.c_char_p], C.c_int),
        "sqlite3_close_v2": ([C.c_void_p], C.c_int),
        "sqlite3_key": ([C.c_void_p, C.c_void_p, C.c_int], C.c_int),
        "sqlite3_busy_timeout": ([C.c_void_p, C.c_int], C.c_int),
        "sqlite3_prepare_v2": ([C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p), C.c_void_p], C.c_int),
        "sqlite3_step": ([C.c_void_p], C.c_int),
        "sqlite3_finalize": ([C.c_void_p], C.c_int),
        "sqlite3_bind_int64": ([C.c_void_p, C.c_int, C.c_int64], C.c_int),
        "sqlite3_bind_null": ([C.c_void_p, C.c_int], C.c_int),
        "sqlite3_bind_text": ([C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.c_void_p], C.c_int),
        "sqlite3_bind_blob": ([C.c_void_p, C.c_int, C.c_void_p, C.c_int, C.c_void_p], C.c_int),
        "sqlite3_column_count": ([C.c_void_p], C.c_int),
        "sqlite3_column_name": ([C.c_void_p, C.c_int], C.c_char_p),
        "sqlite3_column_type": ([C.c_void_p, C.c_int], C.c_int),
        "sqlite3_column_int64": ([C.c_void_p, C.c_int], C.c_int64),
        "sqlite3_column_double": ([C.c_void_p, C.c_int], C.c_double),
        "sqlite3_column_blob": ([C.c_void_p, C.c_int], C.c_void_p),
        "sqlite3_column_bytes": ([C.c_void_p, C.c_int], C.c_int),
        "ZSTD_getFrameContentSize": ([C.c_void_p, C.c_size_t], C.c_ulonglong),
        "ZSTD_decompress": ([C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t], C.c_size_t),
        "ZSTD_isError": ([C.c_size_t], C.c_uint),
    }
    for name, (args, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    _library = lib
    return lib


class Connection:
    def __new__(cls, path, secret=None, *, readonly=True):
        if sys.platform == "win32":
            return WindowsConnection(path, secret, readonly=readonly)
        return super().__new__(cls)

    def __init__(self, path, secret=None, *, readonly=True):
        self.lib = library()
        self.handle = C.c_void_p()
        flags = (1 if readonly else 2 | 4) | 0x10000  # FULLMUTEX; never immutable on live sources.
        code = self.lib.sqlite3_open_v2(str(path).encode(), C.byref(self.handle), flags, None)
        try:
            self.check(code)
            self.check(self.lib.sqlite3_busy_timeout(self.handle, 500))
            if secret is not None:
                self.check(self.lib.sqlite3_key(self.handle, secret, len(secret)))
                self.execute("PRAGMA cipher_compatibility=4")
            self.execute("PRAGMA trusted_schema=OFF")
            if readonly:
                self.execute("PRAGMA query_only=ON")
            self.execute("SELECT count(*) FROM sqlite_schema")  # Verify key before reporting success.
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(code):
        if code:
            raise DatabaseError(code)

    def execute(self, sql, params=()):
        statement = C.c_void_p()
        encoded = sql.encode()
        self.check(self.lib.sqlite3_prepare_v2(self.handle, encoded, len(encoded), C.byref(statement), None))
        try:
            for index, value in enumerate(params, 1):
                if value is None:
                    code = self.lib.sqlite3_bind_null(statement, index)
                elif isinstance(value, int):
                    code = self.lib.sqlite3_bind_int64(statement, index, value)
                elif isinstance(value, bytes):
                    code = self.lib.sqlite3_bind_blob(statement, index, value, len(value), C.c_void_p(-1))
                else:
                    raw = str(value).encode()
                    code = self.lib.sqlite3_bind_text(statement, index, raw, len(raw), C.c_void_p(-1))
                self.check(code)
            count = self.lib.sqlite3_column_count(statement)
            names = [self.lib.sqlite3_column_name(statement, i).decode() for i in range(count)]
            rows = []
            while True:
                code = self.lib.sqlite3_step(statement)
                if code == 101:
                    return rows
                if code != 100:
                    raise DatabaseError(code)
                rows.append({name: self.column(statement, i) for i, name in enumerate(names)})
        finally:
            if statement:
                self.lib.sqlite3_finalize(statement)

    def column(self, statement, index):
        kind = self.lib.sqlite3_column_type(statement, index)
        if kind == 1:
            return self.lib.sqlite3_column_int64(statement, index)
        if kind == 2:
            return self.lib.sqlite3_column_double(statement, index)
        if kind not in (3, 4):
            return None
        size = self.lib.sqlite3_column_bytes(statement, index)
        if size > 64 * 1024 * 1024:
            raise ExportError("单个字段超过 64 MiB，已停止本批读取且未推进进度。")
        value = C.string_at(self.lib.sqlite3_column_blob(statement, index), size) if size else b""
        try:
            return value.decode("utf-8") if kind == 3 else value
        except UnicodeDecodeError:
            return value  # Preserve non-UTF8 content losslessly.

    def close(self):
        if self.handle:
            self.lib.sqlite3_close_v2(self.handle)
            self.handle = C.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WindowsConnection:
    """Use the wheel's sqlite3_key binding, preserving native SQLite WAL locks."""
    def __init__(self, path, secret=None, *, readonly=True):
        self.api = library()
        self.db = None
        try:
            uri = Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
            self.db = self.api.connect(uri, uri=True, timeout=0.5, isolation_level=None)
            self.db.row_factory = self.api.Row
            self.db.text_factory = self.text
            if secret is not None:
                self.db.set_key(secret)
                self.db.execute("PRAGMA cipher_compatibility=4")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if readonly:
                self.db.execute("PRAGMA query_only=ON")
            self.db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        except BaseException as exc:
            self.close()
            if isinstance(exc, self.api.Error):
                self.raise_error(exc)
            raise

    @staticmethod
    def text(value):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value

    @staticmethod
    def raise_error(exc):
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None:
            raise DatabaseError(code) from None
        # Do not echo the SQL statement or unknown native diagnostics.
        raise ExportError("数据库读取失败，请检查密钥、权限和微信版本。") from None

    def execute(self, sql, params=()):
        try:
            cursor = self.db.execute(sql, params)
            try:
                rows = [dict(row) for row in cursor.fetchall()]
                if any(isinstance(value, (str, bytes)) and len(value) > 64 * 1024 * 1024
                       for row in rows for value in row.values()):
                    raise ExportError("单个字段超过 64 MiB，已停止本批读取且未推进进度。")
                return rows
            finally:
                cursor.close()
        except self.api.Error as exc:
            self.raise_error(exc)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def decode_content(value):
    """Return readable content and an explicit status; callers also retain raw bytes."""
    if value is None:
        return None, "empty"
    if isinstance(value, str):
        return value, "text"
    if not isinstance(value, bytes):
        return str(value), "text"
    status = "text"
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        if sys.platform == "win32":
            import zstandard
            try:
                if zstandard.frame_content_size(value) > 16 * 1024 * 1024:
                    return None, "zstd_too_large"
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=16 * 1024 * 1024)
            except zstandard.ZstdError:
                return None, "zstd_error_or_dictionary_required"
            try:
                return value.decode("utf-8"), "zstd"
            except UnicodeDecodeError:
                return None, "binary"
        lib = library()
        size = lib.ZSTD_getFrameContentSize(value, len(value))
        if size == (1 << 64) - 2:
            return None, "invalid_zstd"
        if size == (1 << 64) - 1:
            size = 16 * 1024 * 1024
        if size > 16 * 1024 * 1024:
            return None, "zstd_too_large"
        output = C.create_string_buffer(max(size, 1))
        actual = lib.ZSTD_decompress(output, size, value, len(value))
        if lib.ZSTD_isError(actual):
            return None, "zstd_error_or_dictionary_required"
        value, status = output.raw[:actual], "zstd"
    try:
        return value.decode("utf-8"), status
    except UnicodeDecodeError:
        return None, "binary"
