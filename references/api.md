# 查询接口 · 0.4.1

命令行和 HTTP 共用一套只读查询；结果来自已收集的本地消息库，不直接操作微信。同步停止后，命令行仍可查询已有消息。

## 命令行

在技能的 `scripts` 目录执行以下命令；Windows 将 `/usr/bin/python3` 替换为技能目录的 `.venv\Scripts\python.exe`，或使用 [Windows 启动器](windows.md)。

```sh
/usr/bin/python3 wechat_sync.py                         # 启动同步和 HTTP API
/usr/bin/python3 wechat_sync.py search "会议" --limit 20
/usr/bin/python3 wechat_sync.py contacts "张三"
/usr/bin/python3 wechat_sync.py conversations "项目群"
/usr/bin/python3 wechat_sync.py messages --conversation "返回的username" --since 2026-10-01
/usr/bin/python3 wechat_sync.py message 123
/usr/bin/python3 wechat_sync.py changes --after 100 --limit 50
/usr/bin/python3 wechat_sync.py status --json
/usr/bin/python3 wechat_sync.py api-info
```

`search`、`messages`、`contacts`、`conversations`、`message`、`changes` 输出 JSON。查询失败返回非零退出码和错误提示。HTTP 参数对应同名命令行选项；`q` 对应位置参数，例如 `search "关键词"`。`stop` 保留数据和进度；`restart --port 8766` 更换端口并保存配置。`--home /路径` 可指定独立配置目录。

`export-jsonl --output /绝对路径/messages.jsonl` 指定导出成品位置，父目录必须先创建，已有文件不会覆盖。未传 `--output` 时，命令行默认导出到采集结果数据库旁。

在 Violoop 中，用户未指定位置时，使用当前运行时提醒提供的 **AI Workspace**，导出到其 `wechat-exports/<时间戳>/messages.jsonl`。先创建父目录，时间戳避免冒号，路径作为单独参数传入；Windows 的 PowerShell 示例见 [Windows 接入](windows.md)。核对实际文件位于工作区后，再向用户报告“文件”页面中的相对路径。同步缓存、配置和凭据仍使用各平台的私有存储位置。

Mac 一次性原库导出可用 `wechat_export.py --output /绝对路径/导出目录` 指定位置，目标目录必须尚不存在，由脚本创建。

## HTTP

默认地址 `http://127.0.0.1:8765`，实际地址和 token 文件路径可用 `api-info` 查看。所有接口仅支持 GET，需要 `Authorization: Bearer <token>`。token 自动生成，文件权限为 0600，与微信数据库密钥相互独立。不支持浏览器跨域请求；服务仅监听本机回环地址。

```sh
sync_dir="$HOME/Library/Application Support/WeChat-Export"
# 从文件通过标准输入传给 curl，不把 token 写入命令历史或进程参数。
printf 'Authorization: Bearer %s\n' "$(cat "$sync_dir/api.token")" |
  curl --fail-with-body -sS -H @- -G 'http://127.0.0.1:8765/v1/messages' \
    --data-urlencode 'q=会议' --data-urlencode 'limit=20'
```

| GET 路径 | 参数 | 返回 |
| --- | --- | --- |
| `/v1/status` | 无 | `running`、`state`、`last_poll`、`messages`、`change_sequence`、`errors` 等；刚启动时尚无消息计数 |
| `/v1/messages` | `q`、`conversation`、`sender`、`since`、`until`、`type`、`limit`、`cursor` | `{items: [...], next_cursor: "..." 或 null}` |
| `/v1/messages/<id>` | 无 | 单条消息，额外包含 `source`、`generation`、`table_name`、`raw` |
| `/v1/contacts` | `q`、`limit`、`offset` | `{items: [...], next_offset: 数字或 null}` |
| `/v1/conversations` | `q`、`limit`、`offset` | 同上；会话包含 `username`、`name`、`messages`、`last_message_time` |
| `/v1/changes` | `after`、`limit` | `{items: [...], next_after: 数字, has_more: 布尔值}` |

- `q` 是最多 200 字符的字面子串，支持中文，不解释通配符或全文检索语法。消息查正文，联系人查标识/昵称/备注，会话查标识/显示名。3 字符起使用可用的全文索引，短词仍可查；较老的 SQLite 自动回退为子串扫描。
- `conversation`、`sender` 是精确 `username`；先查联系人或会话获取。`type` 为来源消息类型整数。
- `since` 含边界、`until` 不含边界；接受 Unix 秒或 ISO 日期时间。无时区时使用本机时区，例如 `2026-10-28`；带时区如 `2026-10-28T09:00:00+08:00`，URL 中请编码 `+`。
- `limit` 默认 50，范围 1–200。消息按时间、id 从新到旧；继续分页时保持筛选条件不变并传回 `next_cursor`。分页期间新消息和修改可能出现，需要可靠增量消费时用 `changes`。
- 联系人和会话从 `offset=0` 开始；联系人按名称排序，会话按最近消息时间排序。`next_offset=null` 表示结束。
- 消息字段：`id`、`local_id`、`server_id`、`conversation`、`conversation_name`、`sender`、`sender_name`、`create_time`、`message_type`、`content`、`decode_status`。`id` 仅在当前账号结果库内有效；`local_id` 不能跨会话去重。默认列表不返回原始二进制字段，详情的 `raw` 保留 Base64 编码。
- 消息列表、详情和会话仅包含当前数据库代次且未标记删除的数据。内容可能是 XML，无法解码时 `content` 为 null，查看 `decode_status`。附件不在此接口范围。

增量消费从 `after=0` 开始，按序处理每条 `sequence`、`operation` 和 `message`，成功后持久化 `next_after`，再请求下一页。`has_more=false` 时等待再查。操作包括 `insert/update/delete/restore`；初次收集历史也产生 `insert`，`delete` 表示来源行消失，不等同于微信撤回。复合身份是 `(source, generation, table_name, local_id)`。序号是变更数，不是消息条数。

错误统一为 `{"error":"说明"}`：400 参数无效，401 token 缺失/错误，403 Host 或 Origin 不允许，404 接口/消息不存在，405 方法不支持，503 数据库繁忙、查询超时或不可读。单次查询约 5 秒上限，遇到 503 可缩小时间/会话范围后重试。端口被占用时用 `restart --port 8766`。

服务不记录请求地址、搜索词、聊天正文或 token；不提供写入和任意 SQL 接口。需要更新 token 时，先停止服务，将 `api.token` 移走，再启动生成新 token。
