---
name: wechat-sync
description: "同步、搜索和查询用户自己的本地微信聊天记录，支持关键词、联系人、时间筛选、增量消息、本机 HTTP API 和 JSONL 导出。初始化适用于 macOS 微信 4.1 及以上或 Windows x64 微信 4.x；不用于发送消息。"
---

# 微信同步与查询

在安装微信的电脑上，以当前登录用户运行随包脚本。下文 `<skill>` 是本文件所在目录。

## 平台与版本范围

以下要求适用于初始化和持续同步；查询已经收集的结果不要求微信正在运行。

| 平台 | 微信兼容范围 | 运行环境 |
| --- | --- | --- |
| macOS | 微信 4.1 及以上；不支持微信 3.x、4.0 初始化 | macOS 11+、Python 3.9+、Apple Command Line Tools，无需 pip 或 Homebrew |
| Windows | x64 微信 4.x；不支持微信 3.x、Windows ARM64 初始化 | Windows 10/11 x64、64 位 Python 3.11+；首次使用先按 [Windows 接入](references/windows.md) 安装依赖 |

已实测 Mac 微信 4.1.13、4.1.15，以及 Windows 11 x64 微信 4.1.13.63。没有锁定这些具体小版本，但范围内其他版本仍可能需要适配密钥派生或数据库结构；不能仅凭版本号宣称已验证兼容。完整验证范围见 [数据与排错](references/data.md)。

## 查询与操作入口

统一入口：Mac 用 `/usr/bin/python3 "<skill>/scripts/wechat_sync.py" <命令>`；Windows 用 `& "<skill>/.venv/Scripts/python.exe" "<skill>/scripts/wechat_sync.py" <命令>`。查询返回 JSON，只读取已收集结果，不必启动微信或重新读取密钥。

| 意图 | 命令 |
| --- | --- |
| 搜索正文 | `search "关键词" --limit 20` |
| 按时间、会话或发送者查询 | `messages --since 2026-10-01 --until 2026-11-01 --conversation <id> --sender <id>` |
| 查联系人 / 会话 | `contacts "名字"` / `conversations "名字"` |
| 消息详情 / 后续变更 | `message <id>` / `changes --after <序号>` |
| 同步及接口状态 | `status --json` / `api-info` |
| 开始 / 停止 / 重启 | `start` / `stop` / `restart`；无参数即开始 |
| 导出 JSONL | `export-jsonl`，可加 `--output /绝对路径/messages.jsonl` |
| 移除当前账号密钥 | `forget-key`；停止服务并保留结果 |

查询先根据用户问题确定范围；按人名找消息时，先查联系人或会话，使用返回的 `username` 筛选。时间按用户时区解释；消息默认从新到旧，继续读取时传回 `next_cursor`。完整参数、HTTP 鉴权和增量消费见 [接口说明](references/api.md)。

## 导出文件的位置

用户指定保存位置时优先使用该位置。在 Violoop 中未指定位置时，从当前运行时提醒取得 **AI Workspace**，将导出成品保存到该配对 PC 工作区的 `wechat-exports/<时间戳>/` 下；这就是“文件”页面浏览的位置。独立使用时可用 `--output` 指定位置，未指定时沿用命令默认位置。

- 两端均使用 `export-jsonl --output <绝对路径>`。先创建父目录，使用新的时间戳目录；时间戳用 `yyyyMMdd-HHmmss-fff` 等不含冒号的格式，兼容 Windows。路径按 PC 的操作系统拼接并作为单独参数传入，正确处理空格和中文；Windows 使用 PowerShell 的 `Join-Path`，示例见 [Windows 接入](references/windows.md)。已有文件不覆盖。
- Mac 一次性原库导出使用 `wechat_export.py --output <新的绝对目录>`，由脚本创建尚不存在的目标目录。
- 导出后核对命令结果和实际文件；保存在工作区时，报告相对路径，例如 `wechat-exports/<时间戳>/messages.jsonl`。只有文件确实位于当前 AI Workspace 下时，才说明可在“文件”页面查看。在 Violoop 中运行时未提供工作区路径时，先确认保存位置。

工作区只保存用户要查看的导出成品；同步数据库、配置、HTTP token 和密钥继续使用下述平台数据目录及系统凭据库。

## 启动与初始化

Windows 尚未建立 `.venv` 时先安装依赖，再执行 `status --json`；其他情况直接检查状态。仅查询已有结果时无需启动同步。需要收集新消息或使用 HTTP API 时，已初始化则直接 `start`，未初始化则按下述平台流程操作。重复启动不会增加实例。默认接口为 `http://127.0.0.1:8765`，用 `restart --port 8766` 换端口。

两端启动后均在后台持续同步，关闭终端可继续运行。停止后保留采集进度；电脑重启后再次运行 `start`，复用已有凭据继续采集。工具未配置系统开机自启或进程退出后自动重启。

Mac 首次使用，在 Terminal 中打开 `scripts/开始持续同步.command`：用户选择本人账号，等待“捕获已就绪”后，在临时微信中扫码登录一次，按 macOS 提示授权开发者工具。解密材料存入登录钥匙串，只重签临时副本。成功后独立启动窗口自动关闭；错误保留提示。

Windows 首次使用，按 [Windows 接入](references/windows.md) 安装依赖后运行 `init`；用户从托盘完全退出微信，再从桌面启动，等待捕获就绪后登录。密钥验证后存入当前用户的 Windows 凭据管理器，不生成明文密钥文件。

已有配置时先验证并复用密钥；密钥失效或换账号时才重新 `init`（Mac 也可打开 `scripts/初始化持续同步.command`）。不要索取或输出密码、密钥，不自动反复退出、重签微信。重启电脑不默认触发重新初始化。启动后核对 `last_poll`、`messages`、`errors`；不能仅凭命令成功就宣称收到了新消息。

## 数据与排错

同步缓存和配置位于 Mac 的 `~/Library/Application Support/WeChat-Export/` 或 Windows 的 `%LOCALAPPDATA%\WeChat-Export\`，默认约每 3 秒检查一次。仅包含当前电脑已落盘的消息；不含图片、语音、视频附件。查询范围之外的聊天、HTTP token、数据库密钥不写入日志或回复，不上传结果。

两端均支持 `export-jsonl`。Mac 另支持一次性原库导出：先停止同步，再打开 `scripts/开始导出.command`。同步异常、字段含义和来源格式见 [数据与排错](references/data.md)。
