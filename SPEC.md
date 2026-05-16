# CodeWu — Minimal Prototype Spec

## 1. Goal

一个极简编程 agent 原型，用 LLM 的 Chat Completion API + 本地工具调用，能与用户多轮交互完成一个 JS/Python 软件开发任务。验证「Chat API + Tool Use Loop」最小闭环。

## 2. Non-Goals（明确不做）

- MCP / Skills / Subagents / Hooks / Memory 持久化
- 任务图、并发 agent、子任务派发
- 流式输出 UI 美化、富文本 TUI（先纯文本 REPL）
- 沙箱、容器隔离、命令白名单引擎（用「每条命令问 y/n」兜底）
- 多语言开发支持（仅 JS / Python）
- 鉴权（由本地 GitHub Copilot 反代承担）

## 3. Tech Stack & External Deps

- **实现语言**：Python 3.10+
- **唯一外部依赖**：本地 GitHub Copilot 反代（暴露 OpenAI 兼容 `/v1/chat/completions`）
- **SDK**：`openai`（`base_url` 指向 `http://localhost:<port>/v1`，`api_key` 用占位串）
- **运行形态**：单文件 `main.py` 作为第一版（验证闭环后再考虑拆模块）

## 4. Architecture（一句话版）

REPL → 把用户输入加入 messages → 调 Chat API（带 tools 定义）→ 若响应是 tool_calls，则在本地执行（write/run 类需 y/n）→ 把 tool 结果作为 `tool` role 消息回灌 → 继续循环，直到模型给出最终文本响应 → 等待用户下一轮输入。

```
User → CodeWu REPL
         │
         ▼
   messages[]  ───▶  Chat Completions API（带 tools）
         ▲                       │
         │                       ▼
   tool result  ◀──────  tool_calls?
                              │ yes
                              ▼
                       本地 tool 执行
                       （write/run 需 y/n）
```

## 5. Tool Inventory（v1 仅 4 个）

| 工具 | 用途 | 副作用 | 需 y/n |
|---|---|---|---|
| `read_file(path)` | 读文件全文 | 无 | 否 |
| `write_file(path, content)` | 覆盖写文件 | 有（写盘） | **是** |
| `list_dir(path)` | 列目录（一层） | 无 | 否 |
| `run_cmd(command)` | 执行 shell（Windows PowerShell / POSIX sh 自动选择） | 任意 | **是** |

每个工具有：
- 明确 JSON Schema 给 LLM（OpenAI tool calling 格式）
- Python 实现函数
- 统一返回 `{"ok": bool, "result": str, "error": str|null}`，截断超长输出（如前 8KB）

## 6. Approval Flow（每条副作用工具）

模型发起 `write_file` 或 `run_cmd` 时：
1. 在终端打印 `[Tool] write_file path=... (size=N bytes)` 或 `[Cmd] <command>` 预览
2. 提示 `Approve? [y/n/edit]:`
   - `y` 执行
   - `n` 跳过，把 `{"ok": false, "error": "user denied"}` 回传模型
   - `edit` 允许用户改命令字符串后再问一次（仅 run_cmd；write_file v1 不支持 edit）

## 7. Conversation Loop（伪代码）

```
messages = [system_prompt]
while True:
    user_input = prompt("> ")
    if user_input in {"/exit", "/quit"}: break
    messages.append({"role": "user", "content": user_input})

    while True:  # tool-use inner loop
        resp = client.chat.completions.create(model, messages, tools)
        msg = resp.choices[0].message
        messages.append(msg.model_dump())  # 含 tool_calls
        if not msg.tool_calls:
            print(msg.content)
            break
        for tc in msg.tool_calls:
            result = dispatch_tool(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
```

## 8. System Prompt（v1 草案，落入 main.py 常量）

- 你是 CodeWu，一个用于开发 JS/Python 程序的编程 agent
- 工作目录是用户当前 CWD
- 你只能开发 JS 和 Python 代码
- 调用 `run_cmd` 和 `write_file` 前不要请求确认，CLI 会自动拦截让用户审批
- 报告完成前应执行验证（运行测试、跑脚本、grep 检查等）
- 大改动前先用 `read_file` / `list_dir` 探索现状

## 9. Configuration

- 端点 URL、模型名走环境变量（已验证）：
  - `CODEWU_BASE_URL` 默认 `http://localhost:4141/v1`（**已验证 OpenAI 协议 + tool calling 透传**）
  - `CODEWU_MODEL` 默认 `claude-opus-4.6-1m`（**已验证支持 tool_calls**）
  - `CODEWU_API_KEY` 占位串（反代不校验，但 openai SDK 要求非空）
- 工作目录默认 `os.getcwd()`，所有路径相对它解析
- Shell 选择：Windows 默认 `powershell -NoProfile -Command`，POSIX 默认 `sh -c`；platform 在 system prompt 里告知模型

## 10. Repository Layout（v1.3+）

```
D:\git-nonwork\CodeWu\
├── SPEC.md                    # 本文件
├── codewu.py                  # 全部逻辑（原 main.py，v1.3 重命名）
├── pyproject.toml             # v1.3 加入；声明 codewu console script
├── requirements.txt           # openai 一行（Option B 用）
├── README.md                  # 用法 + 配置说明
├── .gitignore                 # 忽略运行时 .codewu/、__pycache__/ 等
└── <cwd>/.codewu/             # 注意：sessions 落在用户 cwd，不是仓库内
    └── sessions/
        ├── <ts>-<slug>.json
        └── latest.json
```

## 11. Done Contract

第一版「跑通」定义为以下三个验收任务全部由 CodeWu 自身完成（不靠人工手改）：

1. **任务 A — Python**：在空目录创建 `fib.py`，实现 `fib(n)` 函数，写一个 `test_fib.py`，运行测试全部通过
2. **任务 B — JS**：在子目录创建 `package.json` + `index.js`，实现一个简单的 `add(a,b)` 与 mocha/node:test，运行 `node --test` 通过
3. **任务 C — 多轮交互**：先让 CodeWu 创建文件 A，再追加一句让它修改 A，再追加一句让它 grep 验证修改生效——三轮对话上下文连续

验证证据 = 终端可见的命令输出 + 测试 pass + 文件实际落盘。

## 12. Risks

- **R1 Copilot 反代 tool calling 支持度未知**：如果反代不透传 `tools`/`tool_calls`，整套方案失效——必须先做一次 hello-tool 验证
- **R2 Windows PowerShell vs POSIX sh**：`run_cmd` 跨平台行为差异，v1 默认用 `subprocess.run(command, shell=True)` 走系统默认 shell，模型自适应
- **R3 输出爆破**：长输出（如 `dir /s`）撑爆 context — 用 8KB 截断 + 提示「output truncated」
- **R4 路径越权**：模型可能写到 CWD 外 — v1 不做强制约束（每条 write/cmd 都要 y/n 兜底），但在 system prompt 中提示「stay inside CWD」
- **R5 messages 无限增长**：原型阶段不做压缩——超长会话由用户 `/exit` 重启兜底

## 13. Session Persistence & Resume

- **每轮回写**：每完成一次「user → assistant 最终文本」（即一次 inner-loop 收敛），把完整 `messages[]` 原子写入 `~/.codewu/sessions/<session_id>.json`（v1.4 起改为全局存储，session JSON 含 `cwd` 字段标记发起目录）
- **session_id**：启动时生成 `YYYYMMDD-HHMMSS-<6hex>`
- **同步 latest.json**：每次保存后覆盖 `~/.codewu/sessions/latest.json` 为同样内容（便于 `/resume` 默认拿最近一次）
- **CLI 启动模式**：
  - 无参数 → 新建会话
  - `--resume`（不带值） → 加载 `latest.json`
  - `--resume <session_id>` → 加载指定文件
- **REPL 内斜杠命令（v1 最小集）**：
  - `/exit` 或 `/quit` — 退出
  - `/sessions` — 列出 `.codewu/sessions/` 下所有会话（id + 首条 user 消息前 60 字符）
  - `/resume <session_id>` — 当前会话内切换到指定历史会话
  - `/new` — 抛弃当前 messages，开新会话
  - `/dump` — 打印当前 messages 长度与最后 3 条 role/content 摘要（debug 用）

## 14. Open Questions

无（all resolved）

## 15. Change Log

- 2026-05-16 初稿落盘
- 2026-05-16 反代探测过关：OpenAI 协议 + tool calling 透传验证通过；锁定 `claude-opus-4.6-1m` 为默认模型；加入 session 持久化 + resume 设计
- 2026-05-16 v1 实现完成（main.py ~380 行单文件），加入 UTF-8 stdio 兼容（Windows GBK 控制台）
- 2026-05-16 Hello-tool 验证通过：模型调用 `list_dir(".")`，自动审批 → 结果回灌 → 最终文本响应 → session 落盘 5 条消息
- 2026-05-16 Done Contract Task A 通过：模型自主创建 fib.py + test_fib.py，跑 `python test_fib.py` 通过 3/3 测试。Task B (JS) / Task C (多轮交互) 待人工跑验收
- 2026-05-16 用户实测做了一个 2048 小游戏成功；同时反馈 3 个 UX 问题
- 2026-05-16 v1.1 UX 修复（用户实测后驱动）：
  - `_preview_write` 改为「前 5/10 行 + 总行数/字节数 + NEW/OVERWRITE 标签」
  - 新增 `call_llm` 包装：调用前 `[~] thinking...`、调用后 `\r` 覆盖为 `<in>→<out> tokens, <s>s`
  - 新增 `print_history`：`--resume` 时打印 user 消息原文 + tool call 摘要 + assistant 文本（500 字符截断）
- 2026-05-16 v1.2 用户二次反馈后驱动：
  - `/resume`（REPL 内）也走 `print_history`，与 `--resume` 行为一致
  - `handle_slash` 重构为 `SLASH_COMMANDS` 注册表（dict[name → {arg_hint, desc, handler}]），未来加命令只需注册一处
  - 新增 `/help`；裸 `/` 与未知 `/xxx` 一律跳到 help（未知再加 `[!] unknown` 提示行）
  - `/exit` & `/quit` 打印 `Session saved: <id>` + `Resume: python main.py --resume <id>`
  - 新增 `--allow-all` CLI flag：跳过 y/n 提示但保留 preview，banner 显眼 ⚠ 警告
  - 顺手修了 `approve_or_skip` 中 `edit` 命令后 `cmd` 变量未刷新的小 bug
- 2026-05-16 提交 git + 推送 `https://github.com/ShawnWu20147/CodeWu`（commit 18bbff5）
- 2026-05-16 v1.4 用户四次反馈后驱动：
  - **`SESSION_DIR` 移到 `~/.codewu/sessions/`** 全局存储；session JSON 加 `cwd` 字段；`/sessions` 显示 `session_id | cwd | first message` 三列对齐
  - **不自动迁移旧 `<cwd>/.codewu/`**——存在的让用户自己处理
  - **`SYSTEM_PROMPT` 加 `Today's date: <ISO>`** 注入；验证模型回答正确日期
  - **新增 `TURN COMPLETION` 章节**：明确禁止「verbal promise without action」（例：「我来修正一下：」+ 无 tool_call 即 turn 结束）。规定 turn 仅在 (a) work fully done & verified 或 (b) genuinely need user input 两种情况下才结束
- 2026-05-16 v1.3 用户三次反馈后驱动：
  - **Ctrl+C / Ctrl+D 退出也打印 `Session saved` + `Resume:` 提示**；空 session 不打印（避免误导），有内容则在退出前强制 save_session 一次
  - **`run_turn` 中途 `KeyboardInterrupt` 完整回滚** 至 user 消息之前，避免遗留孤儿 `tool_calls` 让下轮 API 报错
  - **重命名 `main.py` → `codewu.py`**（git mv 保留历史）；模块名不再泛用，避免全局 import 冲突
  - **新增 `pyproject.toml`**：声明 console script `codewu = "codewu:main"`；`pip install -e .` 后 `codewu` 全局可用，工作目录 = 用户 shell cwd，session 落在 `<cwd>/.codewu/sessions/`
  - **新增 `!cmd` 直执行**（仿 Claude Code）：`!` 开头的行直接走 PowerShell/sh，无审批（用户输入即批准），输出打印 + 追加到 messages 作 user 角色（前缀 `[I ran: ...]`），不触发 LLM 调用
  - 提取 `_print_exit_hint(sid, msgs)` 与 `handle_bang(line, msgs)` 两个辅助函数

## 16. Known Limitations / Reverse-Sync 发现

- **管道输入下的多余审批 y**：若 `y` 数量多于实际需要，剩余的 `y` 会被外层 input() 当作新一轮 user 消息送给 LLM，产生若干次「nothing to do」回应。交互式使用无此问题。
- **messages 无上限增长**：原型不做 context trim，长会话会撞上模型上下文限制——目前由 `claude-opus-4.6-1m` 的 1M context 暂时兜住。
- **run_cmd 仅 PowerShell（Windows）**：模型生成 PowerShell 语法良好；若希望换 cmd.exe / pwsh / git-bash，需要修 `tool_run_cmd` 中的 argv 构造。
- **审批 UI 简陋**：write_file 不支持 `edit`（只能 y/n 全盘接受或拒绝）；run_cmd 支持但纯行编辑。
- **OVERWRITE 不显示 diff**：v1.1 preview 标了「OVERWRITE 19B → 21B」并显示新内容前 5 行，但没显示真正的 diff。若希望 diff，需要引入 `difflib` 或外部 diff——v1 保持极简。
- **路径越权未强制阻拦**：仅靠 system prompt 提示 + 每条 write/run 的 y/n 兜底——这是有意的极简取舍。
