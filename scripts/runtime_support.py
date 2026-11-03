"""Small OS boundary for storage, locks and background workers."""
import errno
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

WINDOWS = sys.platform == "win32"


def data_home():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "WeChat-Export"
    return Path.home() / "Library/Application Support/WeChat-Export"


def cache_home():
    return data_home() / "cache" if WINDOWS else Path.home() / "Library/Caches/wechat-export"


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if WINDOWS:
        from windows_credentials import protect_directory
        # chmod(0600) on Windows only changes the read-only flag, not the DACL.
        protect_directory(path)
    else:
        path.chmod(0o700)
    return path


def file_lock(path):
    """Return a held nonblocking lock, or None when another process owns it."""
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    handle = path.open("a+b")
    try:
        if WINDOWS:
            import msvcrt
            if not path.stat().st_size:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None
        raise
    return handle


def background_options():
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def disable_core_dumps():
    if not WINDOWS:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@contextmanager
def json_access(path):
    """Serialize Windows JSON readers/writers before replacing an open target."""
    if not WINDOWS:
        yield
        return
    path = Path(path)
    deadline = time.monotonic() + 2
    handle = file_lock(path.with_name(path.name + ".lock"))
    while handle is None:
        if time.monotonic() >= deadline:
            raise TimeoutError("同步状态文件正忙，请稍后重试。")
        time.sleep(0.01)
        handle = file_lock(path.with_name(path.name + ".lock"))
    with handle:
        yield


def replace_file(source, destination):
    # Windows readers may briefly deny replacement while opening status/config.
    # MoveFileEx also reports an open target as access denied (5). Retry briefly;
    # persistent access denial still raises and preserves the previous file.
    for attempt in range(10):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if not WINDOWS or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                raise
            time.sleep(0.02)


def init_hint():
    return "运行 wechat_sync.py init" if WINDOWS else "双击“初始化持续同步.command”"
