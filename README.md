# wechat-sync

在 macOS 和 Windows 上同步、搜索和查询自己的本地微信聊天记录。既可以独立运行，也可以作为 Agent Skill 使用，不依赖 Violoop。

支持关键词、联系人、会话及时间筛选，持续增量同步、本机 HTTP API 和 JSONL 导出。仅处理当前电脑已落盘的消息；不发送消息，不下载或解析图片、语音、视频附件。

## 平台与兼容范围

| 平台 | 初始化要求 | 已实测 |
| --- | --- | --- |
| macOS | macOS 11+、微信 4.1 及以上、Python 3.9+、Apple Command Line Tools | Apple Silicon，微信 4.1.13 |
| Windows | Windows 10/11 x64、x64 微信 4.x、64 位 Python 3.11+ | Windows 11 x64，Python 3.13.17，微信 4.1.13.63 |

Mac 随包数据库组件包含 arm64 和 x86_64，Intel 实机尚未验收。Windows 不支持 ARM64 或微信 3.x 初始化。上述范围不代表所有版本都已测试；微信改变密钥派生或数据库结构后可能需要适配。

## 下载与初始化

```sh
git clone https://github.com/island-yy/wechat-sync.git
cd wechat-sync
```

也可以使用 GitHub 的 Download ZIP，解压后进入仓库目录。所有命令均在安装微信的电脑上，以当前登录用户执行。

### macOS

首次使用，在 Terminal 中运行：

```sh
open scripts/开始持续同步.command
```

选择本人的微信账号，等待“捕获已就绪”后，在临时微信副本中扫码登录，按系统提示允许开发者工具访问。成功后密钥保存到登录钥匙串，同步在后台运行。只重签临时副本，不修改正式微信，不需要关闭 SIP。

### Windows

先安装 64 位 Python 3.11+，再从 PowerShell 运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run.ps1 init
```

安装脚本只在仓库内创建 `.venv` 并下载固定版本依赖。按提示从托盘完全退出微信，再从桌面打开；等待“捕获已就绪”后登录。验证成功后密钥保存到当前用户的 Windows 凭据管理器。

自定义微信数据目录、权限和故障处理见 [Windows 接入](references/windows.md)。

## 查询与管理

Mac 使用以下命令；Windows 将 `python3 scripts/wechat_sync.py` 替换为 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run.ps1`：

```sh
python3 scripts/wechat_sync.py status --json
python3 scripts/wechat_sync.py search '会议' --limit 20
python3 scripts/wechat_sync.py contacts '名字'
python3 scripts/wechat_sync.py conversations '项目群'
python3 scripts/wechat_sync.py messages --conversation '查询返回的username' --since 2026-10-01
python3 scripts/wechat_sync.py changes --after 0 --limit 50
python3 scripts/wechat_sync.py export-jsonl
python3 scripts/wechat_sync.py stop
python3 scripts/wechat_sync.py start
```

查询返回 JSON，服务停止后仍可查询已收集结果。`start` 复用保存的密钥，重复启动保持单实例；仅在密钥失效或更换账号时重新初始化。

默认 HTTP API 监听 `127.0.0.1:8765`，所有接口要求 Bearer token。参数、鉴权和增量消费见 [接口说明](references/api.md)，字段和数据边界见 [数据与排错](references/data.md)。

## 作为 Agent Skill 使用

将完整仓库放入所用 Agent 的技能目录，保留 `SKILL.md`、`scripts/`、`references/` 和原生组件。让模型读取 [SKILL.md](SKILL.md)，即可按平台选择初始化和查询入口。

首次登录和系统授权由用户完成。后续查询与同步复用已有凭据；模型无需读取或接收数据库密钥。

## 数据存储

- Mac：`~/Library/Application Support/WeChat-Export/`
- Windows：`%LOCALAPPDATA%\WeChat-Export\`

数据库密钥存放在系统凭据库，配置文件不保存密钥。微信数据库通过原生 SQLCipher 只读连接访问；查询结果保存在本机，不上传聊天内容。HTTP token 与数据库密钥相互独立。

## 测试

在仓库目录运行合成加密数据测试，不读取真实微信或已保存的真实凭据：

```sh
python3 -B -m unittest discover -s scripts/tests -v
```

Windows 使用 `.venv\Scripts\python.exe` 代替 `python3`。缺少相应平台或可选测试依赖时，相关测试会跳过。已完成 Windows 原生测试及真实登录、手机新消息增量、中文搜索、凭据复用、单实例和停止测试；完整范围见 [数据与排错](references/data.md)。

## 许可证与来源

本项目脚本和文档采用 [MIT License](LICENSE)。第三方代码与依赖保留各自许可证，来源及完整声明见 [第三方声明](scripts/THIRD_PARTY_NOTICES.md)。Mac 数据库组件的源码地址、固定版本、构建参数和校验值见 [BUILD.json](scripts/native/BUILD.json)。
