# wb2codex

把 **WorkBuddy** 的本地会话迁移进 **Codex**。

Migrate your local WorkBuddy conversation history into Codex — including the undocumented paginated storage format.

---

## 为什么需要它

WorkBuddy 把每个会话存成明文 JSONL：`~/.workbuddy/projects/<cwd转义>/<sessionId>.jsonl`。

Codex 从 0.142 起换成了 **paginated 存储**：

| 存储 | 作用 |
|---|---|
| `~/.codex/state_5.sqlite` → `threads` | 会话索引 |
| `~/.codex/thread_history_1.sqlite` → `thread_items` / `thread_turns` / `projection_state` | 真正的对话内容 |
| `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 投影源 |

而且 `external_migration` 这个官方 import 开关在 0.142 里被标记为 **`removed`**：

```console
$ codex features list | grep external_migration
external_migration    removed    false
```

所以没有官方通道可用。这个工具直接生成 Codex 认的三种数据，把会话接进去。

## 安装

**推荐 pipx**（隔离环境 + 全局命令）：

```bash
pipx install git+https://github.com/xmgzxmgz/wb2codex
```

用 uv 更快：

```bash
uv tool install git+https://github.com/xmgzxmgz/wb2codex
```

不想装工具，直接跑源码也行：

```bash
git clone https://github.com/xmgzxmgz/wb2codex
cd wb2codex
python3 -m pip install -e .
```

零第三方依赖，只用 Python 标准库（3.9+）。整个工具就是三个 `.py` 文件，想审计直接看 `src/wb2codex/`。

## 用法

```bash
# 1. 看看有哪些会话
wb2codex list --limit 20

# 2. 导出成 Codex rollout 格式
wb2codex export --out ./codex-import

# 3. 先预演，不写库
wb2codex import ./codex-import --dry-run

# 4. 正式导入（会自动备份两个数据库）
wb2codex import ./codex-import
```

**导入前请完全退出 Codex / ChatGPT 桌面版**，否则它内存里的旧状态可能覆盖写入结果。

完成后再打开 Codex，会话列表里就能看到导入的对话。

### 命令说明

| 命令 | 作用 |
|---|---|
| `list` | 列出 WorkBuddy 本地会话（时间 / 消息数 / 模型 / 标题） |
| `export` | 转成 Codex rollout；支持 `--since` `--limit` |
| `import <目录>` | 批量投影进 Codex |
| `import <文件>` | 只投影单个会话 |
| `import ... --dry-run` | 只统计，不写任何文件或数据库 |

## 它是怎么工作的

`export` 阶段把 WorkBuddy 的行映射成 Codex rollout 事件流：

| WorkBuddy | Codex rollout |
|---|---|
| `message` (role=user) | `response_item` → `message` / `input_text` |
| `message` (role=assistant) | `response_item` → `message` / `output_text` |
| `function_call` | `response_item` → `function_call` |
| `function_call_result` | `response_item` → `function_call_output` |

`import` 阶段要同时满足 Codex 的几条**未公开约束**，缺一条就白屏或报 `no rollout found`：

1. `session_meta` 的枚举必须合法 —— `source="cli"`、`originator="Codex Desktop"`、`cli_version` 用真实版本号。写 `workbuddy` 会被映射成 `unknown`，然后整条会话被判定不可信。
2. rollout 行里**不能有 `function_call`**。Codex 的投影器只认自己的 `exec_command` / `apply_patch` / MCP 协议，遇到外部工具名会让整段投影失败并把 items 清空。
3. `ordinal` 必须从 0 严格连续。
4. 必须包含一行 `event_msg` / `thread_settings_applied` —— 这是 Codex 标记「此会话已初始化」的凭据。
5. `projection_state = (文件总字节数, 行数)`。注意第二个字段是**行数**，不是最后一行 ordinal。
6. `thread_items.turn_id` 固定为 `external-import-turn-1`；`mcpToolCall` / `fileChange` 的 `item_id` 用 `call_id`，其余类型用 `item-N`。

工具会自动完成 1–6，并校验每个会话的 `offset == 文件字节数` 且 `ordinal == 行数`。

## 已知限制

- **工具调用不会导入。** Codex 的投影器不认外部工具名，硬塞会让整条会话损坏。因此导入后保留的是**完整对话文本**（每句提问、每段回答），但不含命令执行记录和文件 diff。
- **导入的会话不建议续聊。** 一旦续写，Codex 会追加新行并重新投影，手写数据的结构可能与它重算的结果不一致。需要接着干活，建议新开一个会话，把要点贴过去。
- **只处理本地会话。** 不做任何网络请求，不读取账号数据。
- 会话 JSONL 解析失败的文件会被跳过（通常含非法控制字符），不影响其它会话。

## 安全说明

- 全程离线，无网络请求。
- 首次写入前自动备份 `state_5.sqlite` 与 `thread_history_1.sqlite`（同目录 `*.bak-*`）。
- 只新增 / 更新自己导入的 thread，不触碰 Codex 原有会话。
- 回滚：删除对应 `threads` 记录及其在 `thread_items` / `thread_turns` / `projection_state` 中的行即可，或直接用备份覆盖。

## 免责声明

本项目通过逆向 Codex 的本地存储格式实现，属于非官方方案。Codex 的内部结构可能随时变化，导致工具失效或数据异常。**使用前请自行备份 `~/.codex`。** 作者不对数据丢失或任何间接损失负责。

与 OpenAI、Anthropic、腾讯均无关联。

## License

MIT
