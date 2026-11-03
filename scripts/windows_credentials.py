"""Current-user Windows Credential Manager; no plaintext key files or argv."""
import ctypes as C
from ctypes import wintypes as W

from db_export import ExportError


class Credential(C.Structure):
    _fields_ = [("Flags", W.DWORD), ("Type", W.DWORD), ("TargetName", W.LPWSTR),
                ("Comment", W.LPWSTR), ("LastWritten", W.FILETIME),
                ("CredentialBlobSize", W.DWORD), ("CredentialBlob", C.POINTER(C.c_ubyte)),
                ("Persist", W.DWORD), ("AttributeCount", W.DWORD), ("Attributes", C.c_void_p),
                ("TargetAlias", W.LPWSTR), ("UserName", W.LPWSTR)]


def current_username():
    lib = C.WinDLL("secur32", use_last_error=True)
    lib.GetUserNameExW.argtypes = [C.c_int, W.LPWSTR, C.POINTER(W.DWORD)]
    size = W.DWORD()
    lib.GetUserNameExW(2, None, C.byref(size))
    buffer = C.create_unicode_buffer(size.value)
    if not lib.GetUserNameExW(2, buffer, C.byref(size)):
        raise ExportError("无法查询当前 Windows 用户名。")
    return buffer.value


def current_sid():
    adv = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.LocalFree.argtypes = [C.c_void_p]
    adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    adv.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    token, size = W.HANDLE(), W.DWORD()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)):
        raise ExportError("无法查询当前 Windows 用户身份。")
    try:
        adv.GetTokenInformation(token, 1, None, 0, C.byref(size))
        buf = C.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, C.byref(size)):
            raise ExportError("无法查询当前 Windows 用户身份。")
        sid = C.cast(buf, C.POINTER(C.c_void_p))[0]
        text = W.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, C.byref(text)):
            raise ExportError("无法查询当前 Windows 用户身份。")
        try:
            return text.value
        finally:
            kernel.LocalFree(C.cast(text, C.c_void_p))
    finally:
        kernel.CloseHandle(token)


def protect_directory(path):
    adv = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
    adv.SetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, C.c_void_p]
    kernel.LocalFree.argtypes = [C.c_void_p]
    descriptor = C.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;{current_sid()})"
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None):
        raise ExportError("无法创建同步目录的 Windows 私有访问权限。")
    try:
        if not adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ExportError("无法限制同步目录的 Windows 访问权限；未保存密钥或聊天数据。")
    finally:
        kernel.LocalFree(descriptor)


class WindowsCredentials:
    def __init__(self):
        self.lib = C.WinDLL("advapi32", use_last_error=True)
        self.lib.CredReadW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD,
                                       C.POINTER(C.POINTER(Credential))]
        self.lib.CredWriteW.argtypes = [C.POINTER(Credential), W.DWORD]
        self.lib.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
        self.lib.CredFree.argtypes = [C.c_void_p]

    @staticmethod
    def target(account):
        return "local.wechat-export.continuous.v1/" + account

    @staticmethod
    def failure():
        return ExportError(f"无法访问 Windows 凭据管理器（系统错误 {C.get_last_error()}）。")

    def get(self, account):
        item = C.POINTER(Credential)()
        if not self.lib.CredReadW(self.target(account), 1, 0, C.byref(item)):
            if C.get_last_error() == 1168:
                raise ExportError("Windows 凭据管理器中没有此账号的密钥，请运行 wechat_sync.py init。")
            raise self.failure()
        try:
            return C.string_at(item.contents.CredentialBlob, item.contents.CredentialBlobSize)
        finally:
            self.lib.CredFree(item)

    def put(self, account, secret):
        data = (C.c_ubyte * len(secret)).from_buffer_copy(secret)
        item = Credential(Type=1, TargetName=self.target(account), CredentialBlobSize=len(secret),
                          CredentialBlob=data, Persist=2, UserName="wechat-sync")
        try:
            if not self.lib.CredWriteW(C.byref(item), 0):
                raise self.failure()
        finally:
            C.memset(data, 0, len(data))

    def delete(self, account):
        if not self.lib.CredDeleteW(self.target(account), 1, 0) and C.get_last_error() != 1168:
            raise self.failure()
