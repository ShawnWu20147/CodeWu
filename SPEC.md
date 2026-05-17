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

## 5. Tool Inventory（v1.9: 5 个）

| 工具 | 用途 | 副作用 | 需 y/n | 预览 |
|---|---|---|---|---|
| `read_file(path)` | 读文件全文 | 无 | 否 | — |
| `list_dir(path)` | 列目录（一层） | 无 | 否 | — |
| `write_file(path, content)` | 创建新文件或彻底重写 | 写盘 | **是** | NEW: 前 10 行；OVERWRITE: unified diff |
| `edit_file(path, old, new)` | 精准替换（v1.9 加入）；`old` 须文件中恰好出现 1 次 | 写盘 | **是** | unified diff |
| `run_cmd(command)` | shell（PowerShell / sh） | 任意 | **是** | 命令字符串 |

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

## 10. Repository Layout（v1.6+）

```
D:\git-nonwork\CodeWu\
├── SPEC.md                    # 本文件
├── README.md                  # 用法 + 配置说明
├── pyproject.toml             # 声明 codewu console script
├── requirements.txt           # openai 一行（Option B 用）
├── .gitignore
└── codewu/                    # v1.6 起改为包结构
    ├── __init__.py            # __version__、暴露 main
    ├── __main__.py            # `python -m codewu` 入口
    ├── cli.py                 # main()、argparse、REPL 主循环、banner
    ├── config.py              # env vars、CWD、SYSTEM_PROMPT、ALLOW_ALL state
    ├── ui.py                  # ANSI 颜色常量、separator、TTY 检测、Windows ANSI 启用
    ├── tools.py               # 4 个 tool 实现 + JSON Schema + dispatch_tool
    ├── approval.py            # approve_or_skip + _preview_write
    ├── session.py             # save/load/list/new_session_id + print_history
    ├── slash.py               # SLASH_COMMANDS 注册表 + 各 handler
    ├── loop.py                # call_llm_stream (stream=True) + run_turn + looks_like_promise + auto-continue
    └── repl.py                # prompt_toolkit session: lexer + completer + @file expand (v1.10)

# 运行时数据（v1.4 起全局，跨 cwd 共享）：
~/.codewu/sessions/
├── <ts>-<slug>.json
└── latest.json
```

依赖 DAG（无环）：`config`/`ui` → `tools`/`session` → `approval`/`slash` → `loop` → `cli`。

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
- 2026-05-17 v1.17 流式失败自动重试（exponential backoff）：
  - 用户反馈：`RemoteProtocolError: peer closed connection without sending complete message body` 经常撞到，体验差
  - 根因：4141 反代偶发中途断流，OpenAI SDK 的内置重试不覆盖**已开始**的 stream，直接抛
  - `loop.py` 拆 `_stream_once`（裸调用）+ `call_llm_stream` 重试包装：
    - 可重试异常：`httpx.RemoteProtocolError` / `ReadError` / `ReadTimeout` / `ConnectError` / `ConnectTimeout` / `WriteError`、`openai.APIConnectionError` / `APITimeoutError` / `RateLimitError` / `InternalServerError`、Python `ConnectionError`
    - 不重试：4xx (BadRequest / Auth) 直接抛，让外层 turn loop 回滚
    - Backoff schedule：1s / 2s / 4s / 8s（`2^attempt`）
  - 默认重试 3 次（共 4 attempts，最坏 1+2+4=7s 总额外等待）
  - 新 config 键 `llm_max_retries` (default 3) + env `CODEWU_LLM_MAX_RETRIES`
  - 重试时显式打 red `[!] stream error: <Type>: <msg>` + dim `retrying in Xs (n/max)`，给完最后告 `giving up after N attempt(s)`
  - 重试期间 partial output 已经在屏幕上——用户看到「半截内容 + retry 提示 + 完整重来内容」，比直接死掉好
  - 单测覆盖：2-fail-then-succeed (3 attempts, 3.0s 精确)、all-fail give-up (3 attempts, 3.0s 精确)
  - Sync `__version__` 0.1.16 → 0.1.17
- 2026-05-17 v1.16 Windows 超时改用 `taskkill /F /T` 杀进程树：
  - 用户反馈：CodeWu 自己 timeout 触发的 kill 失败，孙进程（PowerShell → npm.cmd → node.exe → workers）在 task manager 里仍然活着
  - 根因：Python `Popen.kill()` 在 Windows 上调 `TerminateProcess` 只杀直接子进程，不递归。所以我们的 PowerShell 死了但它派生出的 node/npm 进程变孤儿继续跑
  - **不需要 admin 修**——用 `taskkill /F /T /PID <pid>` 杀整树，对自己拥有的进程不要求提升权限
  - 新增 `_kill_process_tree(proc)` helper：Windows 路径优先 taskkill，失败 fallback 到 `proc.kill()`；POSIX 走原路径
  - `tool_run_cmd` 超时分支 `proc.kill()` → `_kill_process_tree(proc)`
  - 端到端测试：parent PowerShell 启 `ping -t` 子进程（无限运行），trigger 3s timeout，**确认子 ping 进程在 timeout-kill 后死亡**
  - Sync `__version__` 0.1.15 → 0.1.16
- 2026-05-17 v1.15 PowerShell 版本检测 + system prompt 告知 `&&` 限制：
  - 实测用户原 prompt「react 开发一个表达对网易程序员勇神崇拜的小程序」端到端跑 9 min：
    1. v1.13 stdin=DEVNULL 修复确实生效 —— `npx create-react-app yongshen-tribute` 在 ~120s 完整跑通，创建了 `node_modules` / `package.json` / `src` / `public` 等完整脚手架
    2. 但模型第一次用了 bash 风格 `cd X && npx ...`，PowerShell 5.1 不支持 `&&`，返回 `The token '&&' is not a valid statement separator in this version.` 模型不得不重试一次（浪费 token + 时间）
    3. 最后模型 write_file 阶段撞到 proxy `RemoteProtocolError`（服务端中断），与 codewu 无关
  - 启动时 `subprocess.run("$PSVersionTable.PSVersion.Major")` 探一次 PowerShell 主版本（~200ms 一次性成本），结果存 `POWERSHELL_MAJOR`
  - 根据版本生成 `_SHELL_NOTE` 注入 SYSTEM_PROMPT 的 ENVIRONMENT 段：
    - PS < 7：明确写「PowerShell 5.x，**不支持 `&&` / `||`**，用 `;` 或分多次 run_cmd」
    - PS ≥ 7：注明「`&&` / `||` 支持」
  - 同步 `__version__` 0.1.6 → 0.1.15（之前几个版本只 bump 了 pyproject.toml，忘了 `__init__.py`）
- 2026-05-17 v1.14 install.ps1 / install.sh 安装脚本：
  - 用户反馈：「`pip install -e .` 在哪儿跑？最好做个安装脚本」
  - 新增 `install.ps1`（Windows PowerShell）：
    - `Set-Location $PSScriptRoot` 让用户从任何目录都能跑
    - 检测 Python 在 PATH 上，没找到给链接退出
    - 检测 `Get-Process codewu` 是否有运行实例（pip 不能覆盖锁住的 .exe），有则报 PID + 指引「/exit 或 Stop-Process」
    - 调 `python -m pip install -e .`，失败时给常见原因清单（exe 锁、Python 版本、site-packages 权限）
    - 成功后打印 cyan 使用提示 + config/sessions 路径
  - 新增 `install.sh`（POSIX bash，文件 mode 100755）：精简版同上，无 process detection（POSIX 上 pip 替换二进制不锁）
  - README「Install」章节重写：one-liner clone + 脚本调用为首选；保留手动 `pip install -e .` 和 `python -m codewu` 作为备选
  - 实测 install.ps1 端到端：从 0.1.12 自动卸载 + 装上 0.1.13，结尾干净打使用提示
- 2026-05-17 v1.13 修「`npm init -y` 等命令在交互 TTY 下挂起」：
  - 用户反馈：cmd 里直接跑 `npm init -y 2>&1` 秒成，codewu 里跑就一直 timeout
  - 复现：用 codewu 的 Popen 精确配置在 pipe stdin 下跑都正常（< 3 秒），但用户的 codewu 是交互 TTY 启动的，**子进程继承用户终端的 TTY 作为 stdin**，prompt_toolkit 又对终端状态做过手脚，npm 或其子进程（cmd → node）看到 TTY 可能进入交互/检测分支挂起
  - **Surgical fix：`Popen` 加 `stdin=subprocess.DEVNULL`**——子进程拿到 EOF stdin，跟非交互 shell 行为一致；我们的 tool 模型本来就不支持子进程读 user 输入，DEVNULL 反而是更正确的语义
  - 回归测试 3/3：基础 `!echo`、流式 5x sleep、timeout 提前 kill 全部通过
  - bump 0.1.12 → 0.1.13
- 2026-05-17 v1.12 命令执行 UX 改造（用户反馈 npx create-react-app 「一直卡着」）：
  - **`tool_run_cmd` 改用 `Popen` + 两个 daemon 线程逐行读 stdout/stderr**，实时打印 `│ ` dim blue 侧栏前缀（与 `!cmd` 一致风格）。用户跑 `npm install` 等长命令时能看到进度，不再「黑屏等」
  - 进入命令前打印 `[~] running (timeout Xs)...`，让用户知道边界
  - 超时干净 kill + `proc.wait(timeout=5)` 收尸；超时输出红色 `[!] timed out after Xs — process killed`；tool result 含部分 stdout/stderr 给 LLM 决策
  - **`timeout_sec` 加入 `run_cmd` tool schema**（optional integer，minimum 1）；JSON schema 提示 LLM「install ~300、build ~600 等」；`SYSTEM_PROMPT` 中 `run_cmd` 段落同步补充
  - **新增 config 键 `default_cmd_timeout_sec`**（默认 60s，比原硬编码 120 更激进）；env `CODEWU_CMD_TIMEOUT_SEC` 覆盖
  - 优先级链：tool_call.timeout_sec arg > env > config 文件 > 内置 default
  - **`_DEFAULTS` 集中真实默认值字典**，作为「模板生成」和「resolve fallback」的 single source of truth
  - **config.json 模板写入实际默认值**（不再是 null）—— 用户一打开就看到当前默认是啥，比 null 友好得多。null 仍当「未设置」处理（让用户能显式 revert 某项）
  - api_key 显示从「按 source 判断」改为「按值判断」：只要值等于内置 placeholder 就显示明文（不论来自 file 还是 default），否则 `<set>`
  - `handle_bang` 删除重复打印（live stream 已在 `tool_run_cmd` 中处理，避免双打）
  - bump 0.1.11 → 0.1.12
- 2026-05-17 v1.11 history 开关 + 首次自动建 config 模板：
  - **`~/.codewu/config.json` 首次启动自动建模板**（所有键设为 `null` + `_comment` 字段说明），让用户立刻能找到要编辑哪里；已存在则不动
  - **`_resolve` 把 `null` 视作未设置**，走 default —— 模板里的 null 是 no-op，等用户改成实值才生效
  - 新增 `_resolve_bool` 帮助函数：解析 `1/true/yes/on/y` ↔ `0/false/no/off/n`，env 字符串值无法识别时静默 fall through 到 file/default
  - **config 新增 2 键**：
    - `history`：bool，控制是否启用 prompt_toolkit 跨 session 历史。默认 true。
    - `history_file`：string，自定义 history 文件路径。默认 `~/.codewu/history.txt`
  - 对应 env 变量：`CODEWU_HISTORY` / `CODEWU_HISTORY_FILE`
  - `repl._build_history`：根据 `config.HISTORY_ENABLED` 选 `FileHistory(path)` 或 `InMemoryHistory()`；路径不可写时 fallback 到 InMemory，避免 prompt 构造异常
  - `/config` 显示扩到 5 行，history 关闭时 `history_file` 显示 `(unused)` source 为 `—`
  - 实测：null 模板默认 → enabled；file `"history": false` → disabled (file)；env `CODEWU_HISTORY=1` 覆盖文件 → enabled (env)；env `CODEWU_HISTORY=0` → disabled (env)
  - bump 版本 0.1.10 → 0.1.11
- 2026-05-17 v1.10 prompt_toolkit 集成（交互体验对齐 Claude Code / Codex）：
  - 用户澄清依赖政策：「外部依赖」原意指**收费服务**（如 LLM API），开源 Python 包随便引；记得做版本控制防 regression
  - 加 `prompt_toolkit>=3.0.50,<4` 直接依赖（已实测 3.0.52 在环境中，纯 Python 唯一传递依赖 wcwidth）；`pyproject.toml` 版本 bump 到 0.1.10，`openai` 也加 `<2` 上界
  - **新模块 `codewu/repl.py`**：
    - `CodewuLexer`：每字符重染色 —— `!` 行首蓝、`/` 行首青、`@<path>` token magenta（行内只匹配 word boundary，避免 `email@example.com` 误染色）
    - `CodewuCompleter`：`/h` → `/help` 等 slash 命令名（从 SLASH_COMMANDS dict 拉，display_meta 显 desc）；`@s` → cwd 文件/目录 startswith 过滤（dir 优先，含 `@codewu/c` 子目录穿透）；其他文本不补全
    - `complete_while_typing=True` —— 每键自动弹补全菜单（这就是 codex/claude code 体验）
    - 历史持久化到 `~/.codewu/history.txt`（FileHistory，上下键调用）
    - `prompt_input(msg)` 入口：TTY 走 PromptSession，非 TTY 退回 `input()`（pipe 测试不破）
  - **`@<path>` 提交时展开**：cli.py 在送 LLM 前 `expand_at_files(line)` —— 找到文件 → 注入 `<file path='X'>...</file>` 块；找不到 → fuzzy 建议（`difflib.get_close_matches`，cutoff 0.4）+ 不发请求，让用户改 typo
  - 实测：`Look at @pyproject.toml ...` 一句话获得答案，**模型未调 read_file**，省一次 round-trip；典型工作流提速明显
  - 回归测试通过：piped fallback、/help、/config、/sessions、edit_file、!cmd 侧栏、streaming、resume 等全保留
  - history.txt 在 `~/.codewu/`（项目外），不进 git
- 2026-05-17 v1.9 编辑体验 + 视觉区分 + 持久化配置：
  - **新增 `edit_file(path, old_string, new_string)` 工具**：精准定位替换，`old_string` 必须文件中恰好出现一次（0 报「未找到」，≥2 报「不唯一加 context」），强制 UTF-8 编码，禁止空 `old_string` 和 no-op 替换；side-effect 工具，走审批
  - **审批预览全面升级为 `difflib.unified_diff` 红绿染色**：`-` 红 / `+` 绿 / `@@` 黄 / context dim；`write_file` 的 OVERWRITE 分支也用 diff（read 旧内容比新内容）；NEW 分支保持「首 10 行 + 总行数」简介
  - SYSTEM_PROMPT 加 `edit_file` 段落 + 规则：「修改已有文件用 edit_file，新建/彻底重写才用 write_file；不唯一时加 context 行；多处改用多次 edit_file 调用」
  - 端到端实测：模型自觉做 2 次 edit_file（def + call site 分开），每次 diff 预览清晰
  - **`!cmd` 输出加 dim blue `│ ` 侧栏前缀**：bang 输出视觉像 blockquote，与 agent / tool 消息一眼分离
  - **新增 `~/.codewu/config.json` 持久化配置**：JSON 顶层 dict，支持 `base_url` / `model` / `api_key`；将来加 mcp_servers 等键不破坏老版本；malformed 不 crash 但启动时记 `CONFIG_LOAD_ERROR`
  - 优先级链：**env var > config 文件 > 内置 default**；每个 setting 记 `_SRC` 用于 /config 显示
  - 新 slash 命令 `/config`：表格显示当前 effective config（key / value / source）+ 文件路径 + exists 状态 + 优先级说明；api_key 非 default 来源时显示 `<set>` 不漏密
  - 不做 `/set` 写命令——靠编辑器直接改 JSON，避免误改坏
- 2026-05-17 v1.8 用户要求加流式（v1.5 之前 `[~] thinking...` 静态条 UX 不够好）：
  - 探测反代：`stream=true` 支持（haiku 实测 20+ 个 delta chunk）；tool_calls 按 `index` 拼接（name 在首块、arguments 增量追加）；usage 走最终 chunk；`thinking`/`reasoning` 参数被默吃、无独立 reasoning_content 字段 → 思维链单独 UI 不做
  - `codewu/loop.py` 中 `call_llm` → `call_llm_stream`（stream=True）：
    - 首 chunk 来时 `\r` 擦掉 `[~] thinking...`
    - delta.content → 打印 `[CodeWu] ` label 一次，随后 token 实时打印
    - delta.tool_calls → 按 `index` 累加到字典；首次见到 name 即打 `[~] calling tool: <name>`；args 静默累加直到 stream 结束才 dispatch
    - 错误：捕获 stream 异常打印 `[~] (stream error)` 再 re-raise
    - 用量条 `[~] N→M tokens, Xs` stream 结束后单独一行
  - `run_turn` 适配：text 已 stream 完成不再重打 [CodeWu] label；auto-continue 触发也不重打 text（仅打 warn + 注入 nudge）；tool label 已在 stream 时打，不再重打
  - 空响应边缘：content 与 tool_calls 都空 → 打印 `[CodeWu] (empty)` 占位
  - 端到端：haiku 内容流式 + list_dir 工具流式两条都通；分隔线 / session 落盘 / 退出提示等保持
- 2026-05-17 v1.7 v1.5 兜底再被绕过（session 20260517-085634-01c11f：「I已看完HTML内容。现在将其转换为Flask Python网页应用。」未被 heuristic 命中）：
  - **SYSTEM_PROMPT 从 ~25 行扩到 ~100 行**，结构化分章：ROLE / ENVIRONMENT / TOOLS / TURN DISCIPLINE / EXECUTION STYLE / ERROR RECOVERY
  - TURN DISCIPLINE 章节直接引用**真实失败原文作为反例**（含本次 session 那一句、上次 yongshen footer 那一句等），并给出 CORRECT PATTERN 对照
  - STYLE 章节加 5 条具体规则：terse / no preamble / no apology / no recap / explore-before-change + verify-after-change
  - ERROR RECOVERY 章节加 4 条：读错误别盲重试、文件找不到 list_dir、cmd 失败查 stderr、两次失败换方法
  - **`looks_like_promise` 新增 4 类中文 future-tense 模式**：`现在(?:我|我们)?(?:将|就|要|来|去|开始|准备|马上|会)`、`接下来(?:我|我们)?(?:将|要|会|来|准备|开始|是)`、`下一步(?:我|我们)?...`、`马上(?:我|我们)?...`；加英文 `now,?\s+I'll` / `next,?\s+I'll` 两条
  - 单元测试 23/23 通过（含本次失败原文 + 7 个新中文模式 + 9 个负例回归）
  - prompt token 从 ~1.3K 涨到 ~2.8K，每轮成本上升约 2x，可接受
  - API 层「显式 done 信号」（response_format=json）未采用：与 tool_choice 互斥风险、反代支持未知、协议复杂度大；继续走「prompt + heuristic 双保险」路线
- 2026-05-17 v1.6 用户要求重构 + UX 增强：
  - **单文件 codewu.py（~755 行）拆为 codewu/ 包，10 个模块**：__init__、__main__、cli、config、ui、tools、approval、session、slash、loop；依赖 DAG 无环
  - `pyproject.toml`：`py-modules = ["codewu"]` → `packages = ["codewu"]`；入口 `codewu = "codewu.cli:main"`；新增 `python -m codewu` 入口
  - **加颜色系统**（`codewu/ui.py`）：ANSI 常量 + `style()` helper；TTY 检测自动启停；支持 `NO_COLOR` / `CODEWU_NO_COLOR` 环境变量；Windows 启用 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` via ctypes
  - 颜色配色：`[CodeWu]` bold cyan、`[~]` system meta dim、`[Tool]/[Cmd]` bold yellow、preview `\| ...` dim、`(NEW)` green / `(OVERWRITE)` yellow、`[!]` bold red、`[*]` bold green、`>` user 提示 bold green、`!cmd` bold blue、session_id cyan、box drawing dim
  - **轮次分隔线**：每轮（除首轮外）在 `>` 提示前打印 dim 60-字符 `─` 横线
  - 修了几个对齐 bug：`/sessions` / `/help` 表格用 ANSI 包裹前先 pad，再 style
- 2026-05-16 v1.5 v1.4 prompt 不够强，「promise then stop」复发后驱动：
  - **新增代码层 auto-continue 兜底**：`looks_like_promise(text)` 启发式 + `run_turn` 检测到无 tool_call 且文本看起来是「半路承诺」时，注入 user 消息 "Call the tool now to perform the action you just announced" 再次调用 LLM，上限 3 次/turn
  - 启发式两路：(a) 结尾是 `:` `：` `...` `。。。` `—` 任一字符；(b) 文本 ≤ 250 字符且含「I'll/I will/let me + 动作动词」regex 或「我来/我现在/我去/我马上/我接下来/让我来」中文短语
  - 用 regex 收紧 `let me`：仅 `let me fix/update/add/...` 算 promise，避免 `let me know` / `let me see` 误报（14/14 单元测试通过）
  - 触发时终端打印 `[~] auto-continue: model paused on a promise (n/3)`
  - SYSTEM_PROMPT 加铁律：「NEVER end a response with `:`, `：`, `...`, or `。。。`」
  - 端到端：英文复现 yongshen footer 年份场景，模型一路 read→write→verify→done 完成，未触发 auto-continue（说明 prompt 强化本身已经够多数情况）；auto-continue 留作罕见情况安全网
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
