"""Offline SQLCipher 4 export. No access to a running application's databases.

Format reference: https://www.zetetic.net/sqlcipher/design/
WAL reference: https://sqlite.org/fileformat2.html#walformat
"""
from __future__ import annotations

import ctypes
from contextlib import closing, ExitStack
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import time

PAGE_SIZE = 4096
RESERVE = 80
MAGIC = b"SQLite format 3\x00"


class ExportError(Exception):
    pass


def derive_keys(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)
    return key, mac_key


def verify_page(page: bytes, number: int, mac_key: bytes) -> bool:
    if len(page) != PAGE_SIZE:
        return False
    start = 16 if number == 1 else 0
    data = page[start:-64] + struct.pack("<I", number)
    return hmac.compare_digest(hmac.digest(mac_key, data, "sha512"), page[-64:])


def candidate_matches(passphrase: bytes, paths: list[Path]) -> bool:
    # Re-read after login: the account may have recreated a database.
    for path in paths:
        try:
            with path.open("rb") as source:
                page = source.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE or page.startswith(MAGIC):
                continue
            _, mac_key = derive_keys(passphrase, page[:16])
            if verify_page(page, 1, mac_key):
                return True
        except OSError:
            continue
    return False


class CommonCrypto:
    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
        self.crypt = self.lib.CCCrypt
        self.crypt.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        self.crypt.restype = ctypes.c_int32

    def decrypt(self, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        output = ctypes.create_string_buffer(len(ciphertext) + 16)
        size = ctypes.c_size_t()
        status = self.crypt(1, 0, 0, key, len(key), iv, ciphertext,
                            len(ciphertext), output, len(output), ctypes.byref(size))
        if status or size.value != len(ciphertext):
            raise ExportError(f"AES 解密失败（CommonCrypto {status}）。")
        return output.raw[:size.value]


def wal_checksum(data: bytes, seed=(0, 0), endian="<"):
    if len(data) % 8:
        raise ExportError("WAL 校验数据长度错误。")
    a, b = seed
    for x, y in struct.iter_unpack(endian + "II", data):
        a = (a + x + b) & 0xFFFFFFFF
        b = (b + y + a) & 0xFFFFFFFF
    return a, b


def read_wal(path: Path):
    """Return last committed page offsets, database size, and ignored tail bytes."""
    if not path.exists() or path.stat().st_size == 0:
        return {}, None, 0
    with path.open("rb") as source:
        header = source.read(32)
        if len(header) != 32:
            raise ExportError("WAL 头不完整，请正常退出微信后重新导出。")
        magic, version, size, _, salt1, salt2, c1, c2 = struct.unpack(">8I", header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or size != PAGE_SIZE:
            raise ExportError("不支持的 WAL 格式或页大小。")
        endian = "<" if magic == 0x377F0682 else ">"
        seed = wal_checksum(header[:24], endian=endian)
        if seed != (c1, c2):
            raise ExportError("WAL 头校验失败。")
        committed, pending = {}, {}
        db_size, commit_end = None, 32
        while True:
            frame = source.read(24)
            if not frame:
                break
            if len(frame) != 24:
                break  # An incomplete tail is not a committed transaction.
            number, count, s1, s2, c1, c2 = struct.unpack(">6I", frame)
            offset = source.tell()
            page = source.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE or (s1, s2) != (salt1, salt2) or number == 0:
                break
            check = wal_checksum(frame[:8] + page, seed, endian)
            if check != (c1, c2):
                break  # SQLite also stops at the first invalid frame.
            seed = check
            pending[number] = offset
            if count:
                committed.update(pending)
                # Do not resurrect pages from before a truncate transaction.
                committed = {n: pos for n, pos in committed.items() if n <= count}
                pending.clear()
                db_size, commit_end = count, source.tell()
        return committed, db_size, path.stat().st_size - commit_end


def database_inventory(root: Path):
    result = {}
    for db in sorted(root.rglob("*.db")):
        for file in (db, Path(str(db) + "-wal"), Path(str(db) + "-journal")):
            if not file.exists():
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
                raise ExportError("数据库目录包含指向外部的链接，已停止。")
            stat = file.stat()
            if not file.is_file():
                raise ExportError("数据库路径不是普通文件。")
            result[file.relative_to(root)] = (
                stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return result


def snapshot_databases(root: Path, destination: Path, attempts=3):
    """Caller must first stop WeChat; detect any writes during the whole copy."""
    for _ in range(attempts):
        before = database_inventory(root)
        if not before:
            raise ExportError("所选账号没有数据库。")
        destination.mkdir(mode=0o700)
        try:
            for relative in before:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(root / relative, target)
                target.chmod(0o600)
            if before == database_inventory(root):
                return
        except (FileNotFoundError, PermissionError):
            pass
        shutil.rmtree(destination)
        time.sleep(0.5)
    raise ExportError("复制期间数据库仍在变化或无法读取，请退出所有微信后重试。")


def check_integrity(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        errors = db.execute("PRAGMA integrity_check").fetchmany(2)
        if errors != [("ok",)]:
            raise ExportError("SQLite 完整性检查未通过。")


def export_database(source: Path, target: Path, passphrase: bytes, crypto=None):
    """Export a frozen copy, never the live account directory."""
    if target.exists():
        raise ExportError("输出文件已存在，拒绝覆盖。")
    journal = Path(str(source) + "-journal")
    if journal.exists() and journal.stat().st_size:
        # Persistent-mode journals with a zeroed header are not hot.
        with journal.open("rb") as stream:
            if any(stream.read(28)):
                raise ExportError("存在未清理的回滚日志，请正常打开并退出微信后重试。")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(target.name + ".partial")
    if temporary.exists():
        raise ExportError("临时输出已存在，拒绝覆盖。")
    with source.open("rb") as stream:
        first = stream.read(PAGE_SIZE)
    try:
        if first.startswith(MAGIC):
            # This operates only on the frozen snapshot; SQLite incorporates its WAL.
            os.close(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
            result = {"format": "sqlite", "wal_pages": None}
        else:
            if len(first) != PAGE_SIZE or source.stat().st_size % PAGE_SIZE:
                raise ExportError("加密数据库不完整或页大小不是 4096。")
            key, mac_key = derive_keys(passphrase, first[:16])
            wal_path = Path(str(source) + "-wal")
            offsets, committed_size, ignored = read_wal(wal_path)
            count = committed_size if committed_size is not None else source.stat().st_size // PAGE_SIZE
            # Bound corrupt commit sizes before iterating or filling disk.
            if count > source.stat().st_size // PAGE_SIZE + len(offsets):
                raise ExportError("WAL 声明的数据库大小超过可恢复的页数。")
            crypto = crypto or CommonCrypto()
            with ExitStack() as stack:
                main = stack.enter_context(source.open("rb"))
                wal = stack.enter_context(wal_path.open("rb")) if offsets else None
                output = stack.enter_context(os.fdopen(
                    os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"))
                for number in range(1, count + 1):
                    stream = wal if number in offsets else main
                    stream.seek(offsets[number] if number in offsets else (number - 1) * PAGE_SIZE)
                    page = stream.read(PAGE_SIZE)
                    if not verify_page(page, number, mac_key):
                        raise ExportError(f"第 {number} 页 HMAC 校验失败；密钥、格式或数据不匹配。")
                    start = 16 if number == 1 else 0
                    plain = crypto.decrypt(key, page[-RESERVE:-64], page[start:-RESERVE])
                    if number == 1:
                        header = bytearray(MAGIC + plain)
                        if header[16:18] != b"\x10\x00" or header[20] != RESERVE:
                            raise ExportError("解密后的数据库页格式不受支持。")
                        header[18:20] = b"\x01\x01"  # Standalone rollback-mode database.
                        header[28:32] = struct.pack(">I", count)
                        header[92:96] = header[24:28]
                        plain = bytes(header)
                    output.write(plain + bytes(RESERVE))
            result = {"format": "sqlcipher4", "wal_pages": len(offsets), "ignored_wal_tail_bytes": ignored}
        temporary.chmod(0o600)
        check_integrity(temporary)
        temporary.rename(target)
        return result
    except BaseException:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
        raise
