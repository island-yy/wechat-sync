# Windows 接入

要求 Windows 10/11 x64、x64 微信 4.x、64 位 Python 3.11+。先在本人微信中完成登录和历史同步。本实现参考 `freanocank/wechat-decrypt` 的 Windows SHA-512 捕获方案；已在 Windows 11 x64、Python 3.13.17、微信 4.1.13.63 上验证，其他版本仍需单独验收。不支持 Windows ARM64 或微信 3.x 的初始化。

## 安装与初始化

在 PowerShell 进入本技能的 `scripts/windows` 目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 init
```

`setup.ps1` 只在技能目录建立 `.venv` 并安装固定版本的 Frida、SQLCipher 和 Zstandard 预编译包，不修改全局 Python、执行策略或微信文件。此步骤需要联网下载依赖，普通查询与同步不联网。

初始化由当前桌面用户运行，权限需与微信一致。脚本会让用户选择本人账号，提示从托盘完全退出微信；确认后，从 Windows 桌面打开微信，等待“捕获已就绪”再点击登录。不要从 SSH、计划任务或后台服务启动微信替代这一步。

不强制杀微信，不注入发送逻辑，不修改数据库。捕获候选材料后先验证所选账号数据库页的 HMAC，再验证消息库只读连接；通过后才存入当前用户的 Windows 凭据管理器，并开启持续同步。不在终端、文件、日志或进程参数中输出密钥。

微信的数据目录不在“文档”中时，指定 `xwechat_files` 根目录；默认只查当前用户的文档位置，包含重定向的文档目录，不搜索其他 Windows 用户：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 init --db-root 'D:\微信数据\xwechat_files'
```

默认总等待 180 秒，可用 `--timeout 300` 调整到 30–900 秒。一次超时或失败后停止排查，不自动反复退出或重新登录。

## 日常使用

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 status --json
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 start
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 search '会议' --limit 20
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 export-jsonl
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 stop
```

也可以直接用技能目录的 `.venv\Scripts\python.exe` 调用 `scripts\wechat_sync.py`，命令和参数与 Mac 一致。服务在后台运行，关闭终端不会停止；重启电脑后运行 `start` 即可。服务停止后仍能查询已经收集的结果。

配置、状态、只读查询结果和 HTTP token 默认保存在 `%LOCALAPPDATA%\WeChat-Export\`，目录 DACL 限制到当前用户。数据库密钥保存在凭据管理器，配置文件只保存账号路径和凭据标识。两端和不同电脑各自初始化，不能复用另一台电脑的数据库密钥。

默认 HTTP 接口为 `http://127.0.0.1:8765`，与 Mac 相同，鉴权与参数见 [接口说明](api.md)。Windows 同样使用原生 SQLCipher 只读连接和 SQLite WAL 锁，不通过反复整库解密模拟实时读取。

## 排错与验证

- 找不到账号：确认 `--db-root` 是当前账号的 `xwechat_files` 根目录，且微信已落盘消息。
- 捕获失败：确认微信已完全退出、从当前桌面启动，且登录发生在捕获就绪之后；核对微信版本、x64 架构及用户权限，不自动切换到别的账号。
- SQLite 26：可能是密钥、账号或格式不匹配；先核对账号和目录，确认后重新初始化。升级微信后先用已有密钥验证，不预设密钥一定变化。
- SQLite 5/6：库正忙，下一轮重试，不强制杀微信。
- 忘记密钥：运行 `forget-key` 会先停止同步，再仅删除当前配置账号的凭据，保留已收集结果。

合成测试（不访问真实微信、不读取真实已保存凭据）：

```powershell
..\..\.venv\Scripts\python.exe -B -m unittest discover -s ..\tests -v
```

测试包括加密库读写隔离、未提交 WAL、提交后增量、断点恢复、中文查询、HTTP 鉴权、单实例和候选密钥匹配。Windows 原生运行时还会用隔离的测试凭据验证凭据管理器和私有目录权限；这不替代具体微信版本的登录捕获验收。

2026-11-03 在上述 Windows 11 / 微信 4.1.13.63 环境完成 17 项原生测试，并实测登录取密钥、复用凭据重启、手机新消息增量与中文检索、重复启动保持同一进程、停止后保留进度。状态文件读写使用跨进程锁协调，并短暂重试 Windows 文件共享冲突；空消息分库会继续等待后续会话表。
