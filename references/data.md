# 数据与排错

## 查询消息

优先使用命令行或 [HTTP API](api.md)；以下是直接读取结果库时的字段说明。

从 `config.json` 的 `output` 字段取得实际结果数据库路径，使用 SQLite `mode=ro` 打开。不要为查询结果重新读取系统凭据或连接微信进程。

| 表或视图 | 用途 |
| --- | --- |
| `current_messages` | 当前来源数据库代次中未标记删除的消息，附联系人显示名称 |
| `messages` | 已收集记录，包含历史代次和删除标记 |
| `changes` | 按 `sequence` 单调递增的变更流 |
| `contacts` | 账号标识、备注和昵称 |
| `checkpoints` | 采集器私有进度，不要改动 |

消息常用字段：`source`、`generation`、`table_name`、`local_id`、`server_id`、`conversation`、`conversation_name`、`sender`、`sender_name`、`create_time`、`message_type`、`content`、`decode_status`、`raw_json`。

`create_time` 是来源的 Unix 秒时间；展示时转换到用户时区。`message_type` 保留来源整数，不要将所有类型都当作文本。`content` 可能是 XML；未能解码时查看 `decode_status`。`raw_json` 中二进制字段以 `{"encoding":"base64","data":"..."}` 保存。用参数化 SQL 筛选，并只读取用户所需范围。

消息在本库的身份是 `(source, generation, table_name, local_id)`；不能仅凭 `local_id` 跨会话去重。数据库重建会产生新代次。

增量消费者自行记录最后处理的序号：

```sql
SELECT sequence, operation, source, generation, table_name, local_id, payload_json
FROM changes WHERE sequence > ? ORDER BY sequence LIMIT 500;
```

`operation` 包括 `insert/update/delete/restore`。`delete` 表示补扫时发现来源行不存在，不等同于微信撤回。初次读取已有历史也会生成 `insert`。最近 50 条会回读；历史补扫每轮最多 16 批，每张表完成后至少间隔 10 分钟再扫，旧消息变化的发现可能更晚。

## 排错

- 未初始化、系统凭据缺少密钥：Mac 在 Terminal 打开初始化入口，Windows 运行 `init`，由用户完成授权及登录操作。
- SQLite 26／密钥不匹配：检查选择的账号；需要时重新初始化一次。不要关闭校验或将运行中的数据库当作 immutable。
- 无法读取微信目录：根据系统提示给实际运行终端授予所需文件访问权限；不要关闭 SIP。
- 状态显示运行但有错误：按 `status.json` 的 `errors` 说明处理，未读到消息不能报告同步成功。
- 新消息尚未出现：检查正常微信是否已登录并同步、`last_poll` 是否更新、`catching_up` 和错误状态。电脑休眠或微信离线时不能实时读取。
- SQLite 5/6：数据库繁忙，程序会下一轮重试；反复发生时检查状态，不强制杀微信。
- 停止请求尚未完成：等待当前批次退出，再检查状态；不要凭 PID 强制杀进程。
- 非预期调试停止：保留括号中的 `signal` / `exception` / `reason` 编号和模块位置；0.2.3 改为在第一次登录前设置断点，由当前用户的调试器启动临时副本，不忽略未知异常。

脚本只处理 `message/message_*.db`、`message/biz_message_*.db` 中受支持的会话表。其他消息格式、自定义压缩字典、附件解析未覆盖。正常运行使用只读原生 SQLCipher 连接；不会修改微信消息表或更换其密钥。

## 版本与验证

工具 0.4.1；Mac 随包 SQLCipher 4.19.0 + Zstandard 1.5.7，arm64/x86_64，固定来源及校验值见 `scripts/native/BUILD.json`。Windows 使用 `scripts/windows/requirements.txt` 固定版本的预编译包；安装与兼容边界见 [Windows 接入](windows.md)。许可与来源见 `scripts/THIRD_PARTY_NOTICES.md`。

已验证合成加密库的并发 WAL、事务回滚、补扫、断点续读和后台启停，并用微信 4.1.13 导出数据验证解析。已在本机微信 4.1.13 上完成扫码初始化和持续收集；其他微信版本及 Intel 实机仍需单独验收。

Mac 微信 4.1.15 已完成真实登录密钥捕获、原生只读采集和 JSONL 导出验证。临时副本按进程 ID 请求系统正常退出；清理或恢复正式微信失败时返回失败，保留仍在使用的副本。

Windows 11 x64 / Python 3.13.17 / 微信 4.1.13.63 已完成 17 项原生测试、真实登录密钥捕获、消息库只读连接、后台启停和鉴权查询验证；手机发来的新消息可增量收集并按中文关键词查询，重复启动保持同一进程。仅含已知 WCDB 元数据的空分库正常等待首条消息，未知表结构仍返回错误。
