"""Login Keychain storage through Security.framework; no secrets in process arguments."""
import ctypes as C
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys

from db_export import ExportError

SERVICE = b"local.wechat-export.continuous.v1"


def account_id(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()


class Keychain:
    def __new__(cls):
        if sys.platform == "win32":
            from windows_credentials import WindowsCredentials
            return WindowsCredentials()
        return super().__new__(cls)

    def __init__(self):
        self.lib = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.lib.SecKeychainFindGenericPassword.argtypes = [
            C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p,
            C.POINTER(C.c_uint32), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
        self.lib.SecKeychainAddGenericPassword.argtypes = [
            C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p,
            C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p]
        self.lib.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]
        self.lib.SecKeychainItemDelete.argtypes = [C.c_void_p]

    @staticmethod
    def check(code):
        if code:
            raise ExportError(f"无法访问登录钥匙串（系统错误 {code}）。请解锁钥匙串并允许本工具访问后重试。")

    @contextmanager
    def item(self, account):
        name = account.encode()
        size, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        code = self.lib.SecKeychainFindGenericPassword(
            None, len(SERVICE), SERVICE, len(name), name, C.byref(size), C.byref(data), C.byref(item))
        if code == -25300:
            yield None, None
            return
        self.check(code)
        try:
            yield C.string_at(data, size.value), item
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)
            self.cf.CFRelease(item)

    def get(self, account):
        with self.item(account) as (secret, _):
            if secret is None:
                raise ExportError("钥匙串中没有此账号的密钥，请双击“初始化持续同步.command”。")
            return secret

    def put(self, account, secret):
        with self.item(account) as (_, item):
            if item:
                self.check(self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(secret), secret))
            else:
                name = account.encode()
                self.check(self.lib.SecKeychainAddGenericPassword(
                    None, len(SERVICE), SERVICE, len(name), name, len(secret), secret, None))

    def delete(self, account):
        with self.item(account) as (_, item):
            if item:
                self.check(self.lib.SecKeychainItemDelete(item))
