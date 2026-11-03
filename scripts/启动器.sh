#!/bin/bash
set -u
umask 077
cd -- "$(dirname -- "$0")" || exit 1
if [ "$(uname -s)" != "Darwin" ]; then
  echo "此工具仅支持 macOS。"
  exit 1
fi
if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  echo "首次使用需要 Apple Command Line Tools（提供 Python 和 LLDB）。"
  echo "按回车打开 Apple 安装窗口；安装完成后，请再次双击本文件。"
  read -r _
  /usr/bin/xcode-select --install
  exit 1
fi
python_bin=""
for candidate in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    python_bin="$candidate"
    break
  fi
done
if [ -z "$python_bin" ]; then
  echo "需要 Python 3.9 或以上版本。请先安装或更新 Apple Command Line Tools。"
  read -r -p "按回车关闭窗口……" _
  exit 1
fi
"$python_bin" "$@"
result=$?
echo
case "$result:${1:-}:${2:-}" in
  0:wechat_sync.py:init|0:wechat_sync.py:start) ;;
  *) read -r -p "按回车结束并关闭本次启动窗口……" _ ;;
esac
if [ "${TERM_PROGRAM:-}" = "Apple_Terminal" ] && [ -t 0 ]; then
  "$python_bin" - "$(tty)" <<'PY'
import subprocess
import sys

# Wait until the launch shell exits. Never close a working shell or other tabs.
script = '''on run argv
    repeat 20 times
        tell application "Terminal"
            repeat with w in windows
                if (count tabs of w) is 1 then
                    set t to tab 1 of w
                    if tty of t is item 1 of argv then
                        if not busy of t and processes of t is {} then
                            close w
                            return
                        end if
                    end if
                end if
            end repeat
        end tell
        delay 0.5
    end repeat
end run'''
subprocess.Popen(["/usr/bin/osascript", "-e", script, sys.argv[1]],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, start_new_session=True)
PY
fi
exit "$result"
