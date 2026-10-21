# grill-mcp

需求澄清访谈引擎 MCP。宿主 AI 收到模糊需求后，用它把需求整理成**不可含糊的事实**：逐题向用户确认，把每个待拍板点收敛成「已拍板 / 还问不清 / 明确不做」三类，最后落盘成方案文件、术语表与 ADR。

## 定位：Model A（AI 分析 + MCP 访谈引擎）

MCP 本身**不做推理**，只做确定性的事：状态机 + 持久化 + 文件落盘。推理在宿主 AI 侧，用 grilling / domain-modeling 等 skill 分析需求、提炼待拍板点（decision points），再交给 MCP 执行访谈。

```
模糊需求
  → 宿主 AI 用 grilling / requirement-analysis 分析
  → 提炼 decision_points 传入 create_session
  → MCP 按依赖顺序逐题 next_question / answer
  → 四格状态：resolved（已拍板）/ pending（可问）/ fog（还问不清）/ out_of_scope（不做）
  → finalize 落盘：方案文件 + CONTEXT.md 术语 + ADR
```

## 能力

- **四格状态机**：吸收 wayfinder 的决策地图语义，每个决策点落在 `resolved / pending / fog / out_of_scope` 之一。
- **依赖排序提问**：`depends_on` 声明依赖，依赖未满足的问题进 fog，满足后自动毕业成 pending。
- **场景模板**：`medium-feature` / `large-migration` / `architecture` / `free`，对应日常开发流程。
- **推荐答案**：每个问题可带 `recommended` + `why`，帮助用户快速拍板。
- **事实落盘**：`finalize(write=True)` 输出「方案写法」格式的方案文件，并按需追加术语表（CONTEXT.md）与 ADR（docs/adr/）。

## 工具

| 工具 | 说明 |
|---|---|
| `list_scenarios` | 列出可用场景模板 |
| `create_session(requirement, scenario?, decision_points?)` | 开访谈会话 |
| `next_question(session_id)` | 取下一个待确认问题（按依赖顺序），done=true 表示闭环 |
| `answer(session_id, question_id, choice, note?)` | 记录一个回答 |
| `mark_out_of_scope(session_id, question_id, reason?)` | 把某问题标记为「明确不做」 |
| `get_status(session_id)` | 查看四格状态 |
| `finalize(session_id, write?, output_dir?)` | 收敛成事实清单，可落盘 |
| `list_sessions` | 列出所有会话（运维排查） |

`decision_points` 元素格式：

```json
{"id": "essence", "question": "本质是什么？", "options": ["A", "B"], "recommended": "A", "why": "成本低", "depends_on": ["other"], "kind": ""}
```

- `id` 唯一；`question` 必填；`options` / `recommended` / `why` / `depends_on` / `kind` 可选。
- `kind` 取值：`"term"`（落盘时进 CONTEXT.md 术语表）、`"decision"`（落盘时写 ADR）；缺省为空。`id` 为 `terms` / `decision` 时等价于 `kind="term"` / `kind="decision"`。

## 安装与启动

```bash
uv sync                                  # 安装依赖
uv run python server.py                  # stdio（默认，由 MCP 客户端拉起）
GRILL_MCP_TRANSPORT=streamable-http uv run python server.py   # HTTP 模式，便于手工调试
```

> stdio 模式是给 MCP 客户端 spawn 用的，手工前台跑会阻塞在 stdin 上；停掉用 Ctrl+D（EOF）或 `pkill -f "uv run python server.py"`。

## 集成到 agent

MCP 服务器本身只是一个进程（stdio 下是子进程，HTTP 下是独立进程）。把它注册进 agent 的 MCP 配置后，agent 启动时拉起它、发现它的 8 个工具，之后就能像调用内置工具一样调用 `create_session` / `answer` / `finalize` 等。

### Qoder（本环境，已验证）

编辑 `~/.qoder/settings.json`，在 `mcpServers` 里加一条（`command` 用绝对路径，`args` 指到本项目目录）：

```json
{
  "mcpServers": {
    "grill-mcp": {
      "command": "/Users/xuxinzhi/.local/bin/uv",
      "args": [
        "run",
        "--directory",
        "/Users/xuxinzhi/Documents/gowork/tools/grill-mcp",
        "python",
        "server.py"
      ]
    }
  }
}
```

改完重启 Qoder（或重连 MCP），grill-mcp 的 8 个工具就会出现在工具列表里。

> 同机的 `apifox`、`feishu` 两个 MCP 也是这么注册的，可直接照抄格式。

### 其他客户端（通用 mcpServers 格式）

各客户端都接受同一种 `mcpServers` 映射，只是落在不同配置文件：

| 客户端 | 配置文件 |
|---|---|
| Qoder | `~/.qoder/settings.json` 的 `mcpServers` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Codex | `~/.codex/config.toml` 的 `[mcp_servers.grill-mcp]`（`command` + `args` 数组） |
| Cursor | 项目根 `.cursor/mcp.json` |

### HTTP 模式（streamable-http）

先单独起服务，再以 `url` 方式注册（适用于飞书智能体等需要公网/内网可达的 agent）：

```bash
GRILL_MCP_TRANSPORT=streamable-http uv run python server.py
# 监听 http://127.0.0.1:8000/mcp（启动日志会打印实际路径）
```

```json
{ "mcpServers": { "grill-mcp": { "url": "http://127.0.0.1:8000/mcp" } } }
```

> stdio 模式由 agent 负责 spawn，最适合本地 Qoder/Claude Desktop；HTTP 模式适合服务化、多人或多端复用，需自行处理鉴权（`token` 字段预留）。

## 配置

复制 `config.example.json` 为 `config.json`（可选，缺省值即可跑）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `transport` | `stdio` | `stdio` 或 `streamable-http` |
| `host` / `port` | `127.0.0.1` / `8000` | streamable-http 监听地址 |
| `token` | 空 | 预留 |
| `session_db` | `~/.qoder/grill-mcp/sessions.db` | SQLite 会话库 |
| `output_dir` | `~/.qoder/grill-mcp/outputs` | finalize 落盘根目录 |

## 使用示例

```bash
# 1. 开一个「中等功能」访谈
create_session(requirement="给任务管理列表加批量改标签", scenario="medium-feature")
# → session_id + 第一个 pending 问题（本质）

# 2. 逐题回答（宿主 AI 循环调用 next_question → 展示 → answer）
answer(session_id, "essence", "本质是批量改已有任务标签，不是新任务类型")
...

# 3. 收敛 + 落盘
finalize(session_id, write=True, output_dir="/path/to/project")
```

`finalize` 返回：

```json
{
  "done": true,
  "markdown": "# 需求：…\n> 核心结论：…\n## 本质\n…",
  "facts": [{"id": "essence", "question": "…", "choice": "…", "note": ""}],
  "out_of_scope": [],
  "open": [],
  "written": ["/path/to/project/<slug>-方案.md", "…/CONTEXT.md", "…/docs/adr/0001-<slug>.md"]
}
```

- `markdown` 按「方案写法」六点格式：核心结论 → 本质 → 归属 → 需要新增 → 复用现有 → 接口改动 → 后端流程 → 改动范围 → 明确不做 → 待拍板。
- 未闭环（`done=false`）也可 finalize，`open` 会列出还问不清/待拍板的项，宿主 AI 据此决定是否继续。

## 与 skill 的集成

| 场景模板 | 对应流程 | 下游 |
|---|---|---|
| `medium-feature` | 中等功能、边界不清 | grill-with-docs → **to-spec** → implement |
| `large-migration` | 大型迁移、路径不明 | wayfinder → to-spec → **to-tickets** → implement |
| `architecture` | 架构体检 | improve-codebase-architecture → domain-modeling |
| `free` | 自由（全由宿主 AI 传 decision_points） | 任意 |

grill-mcp 只产出**事实**（facts + open + out_of_scope）；spec / tickets 由宿主 AI 的 to-spec / to-tickets skill 依据事实继续生成。

## 目录结构

```
grill-mcp/
├── server.py            # FastMCP 入口，注册 8 个工具
├── interview.py         # 访谈状态机（四格 + 依赖排序 + finalize）
├── factsheet.py         # 方案写法渲染 + 三类文件落盘
├── session_store.py     # SQLite 会话持久化
├── templates/scenarios.json   # 场景模板
├── tests/test_interview.py    # 单测
├── config.example.json
└── pyproject.toml
```

## 测试

```bash
uv run python -m unittest discover -s tests
```
