# CodeWu

A minimal coding agent prototype. Pure-Python package (`codewu/`), two direct deps (`openai` + `prompt_toolkit`), five local tools, per-call y/n approval for any side effect, ANSI colors, live streaming, session resume per project.

> Builds JS or Python programs. See [SPEC.md](./SPEC.md) for the design contract and full change log.

## Requirements

- Python 3.10+
- A local OpenAI-compatible Chat Completions endpoint that supports tool calling. Default config points at `http://localhost:4141/v1` and `claude-opus-4.6-1m`.

## Install

### Quick install (recommended)

```powershell
git clone https://github.com/ShawnWu20147/CodeWu.git
cd CodeWu
.\install.ps1
```

If your PowerShell execution policy blocks the script:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer detects Python, refuses if a `codewu.exe` is already running (you must `/exit` first), then runs `pip install -e .` and prints usage hints with your config / session paths.

**Linux / macOS:**
```bash
git clone https://github.com/ShawnWu20147/CodeWu.git
cd CodeWu
./install.sh
```

### Manual install

```powershell
pip install -e D:\path\to\CodeWu
```

After installing either way, `codewu` is on your `PATH` and you can run it from **any** working directory.

### Run without installing

```powershell
pip install -r requirements.txt
python -m codewu
```

## Run

```powershell
# new session in current cwd
codewu

# interactively pick from sessions for this cwd
codewu --pick      # or -p

# resume the most recent session for this cwd
codewu --resume

# resume a specific session id (looks across projects if needed)
codewu --resume 20260516-201437-21d227

# bypass tool approval prompts (previews still print) — use with care
codewu --allow-all
```

## Configuration

CodeWu reads `~/.codewu/config.json` (auto-generated with real default values on first run). Edit any key; set to `null` to fall back to the built-in default. Environment variables override the file.

| Config key / env var | Default | Purpose |
|---|---|---|
| `base_url` / `CODEWU_BASE_URL` | `http://localhost:4141/v1` | Chat Completions endpoint |
| `model` / `CODEWU_MODEL` | `claude-opus-4.6-1m` | Model name |
| `api_key` / `CODEWU_API_KEY` | placeholder | Sent to the SDK; the proxy doesn't validate it |
| `history` / `CODEWU_HISTORY` | `true` | Persist up/down-arrow input history (prompt_toolkit) |
| `history_file` / `CODEWU_HISTORY_FILE` | `~/.codewu/history.txt` | Where input history lives |
| `default_cmd_timeout_sec` / `CODEWU_CMD_TIMEOUT_SEC` | `60` | Default timeout for `run_cmd` (LLM can override per call) |
| `llm_max_retries` / `CODEWU_LLM_MAX_RETRIES` | `3` | Retries before giving up on a chat call |
| `NO_COLOR` / `CODEWU_NO_COLOR` | unset | Disable ANSI colors (also auto-off when stdout isn't a TTY) |

Precedence: env var > `~/.codewu/config.json` > built-in default. Run `/config` inside CodeWu to see current effective values + their source.

## Tools

| Tool | Side effect | Approval | Preview |
|---|---|---|---|
| `read_file(path)` | none | auto | — |
| `list_dir(path)` | none | auto | — |
| `write_file(path, content)` | writes a file | **y/n** | first 10 lines (NEW) or unified diff (OVERWRITE) |
| `edit_file(path, old_string, new_string)` | edits a file | **y/n** | unified diff (red `-` / green `+`) |
| `run_cmd(command, timeout_sec?, background?)` | runs shell | **y/n** (or `edit`) | command + effective timeout |

- `run_cmd` uses **PowerShell on Windows**, `sh` on POSIX. The model is told the version and warned about PowerShell 5's lack of `&&`.
- `run_cmd` streams stdout/stderr live with a dim-blue `│ ` sidebar.
- `edit_file` requires `old_string` to appear in the file **exactly once**; the model is told to add surrounding context when a short snippet isn't unique.
- For long-running commands (`npm start`, `vite dev`, watchers, web servers), the model passes `background=true` — the process spawns detached and stays alive across CodeWu sessions. See **Background processes** below.

## In-REPL commands

### Slash commands

| Command | Effect |
|---|---|
| `/help` (or bare `/`) | List all commands |
| `/exit` / `/quit` (or Ctrl+C / Ctrl+D) | Quit; prints `--resume` hint with session id |
| `/config` | Show current effective config + source (env / file / default) |
| `/bg [list\|stop <pid>\|log <pid>]` | Manage background processes (list / kill / tail log) |
| `/sessions [all]` | List saved sessions for the current cwd (or `all` projects) |
| `/resume [id]` | Switch to another saved session (defaults to this cwd's latest); replays history |
| `/new` | Start a fresh session |
| `/dump` | Show message count + last 3 message previews (debug) |

### Inline file references — `@path`

Type `@` anywhere in your message to reference a file:

```
> what does @codewu/cli.py do?
> compare @SPEC.md and @README.md and suggest fixes
> look at @src/components/  ← directory navigation: @dir/ shows subdir files
```

While you type, a completion menu of files in the current directory is shown with startswith filtering (prompt_toolkit). When you press Enter, file contents are inlined into the message as `<file path="...">...</file>` blocks so the agent doesn't need to call `read_file`. Typos get caught at submit time with fuzzy suggestions (`did you mean: README.md?`) and the turn aborts so you can fix the path.

### Bang shortcut — run a shell command directly

Prefix any line with `!` to run it yourself in the shell. Output is shown with a dim-blue `│ ` sidebar (so it's visually distinct from agent messages) and the (command, output) pair is appended to the conversation context for the agent to see on the next turn.

```
> !git status
> !npm test
```

## Background processes

When the agent calls `run_cmd(..., background=true)`, the process is spawned detached and its stdout/stderr stream to a per-process log file under `~/.codewu/bg/`. Use:

```
/bg list                    # see what's running
/bg log <pid>               # tail the last 50 lines of one process's log
/bg stop <pid>              # kill it (process-tree kill on Windows, SIGTERM then SIGKILL on POSIX)
```

Background processes survive CodeWu exit. The next startup banner reminds you if any are still alive.

## Session storage

Sessions live under `~/.codewu/sessions/<cwd-slug>/<session-id>.json`, with a per-project `latest.json` pointer. The slug uses Claude-Code-style `--` separators (`D:\git-nonwork\yongshen` → `D--git-nonwork--yongshen`). Pre-v1.20 flat-layout sessions are auto-migrated on first run; nothing is lost.

`/sessions` defaults to the current project for less noise; `/sessions all` shows everything.

## Failure diagnostics

If a chat completion stream fails with `RemoteProtocolError` (proxy SSE timeout on a slow generation), CodeWu:
1. Skips the usual backoff and tries non-streaming immediately (saves ~7 seconds);
2. Writes the failing request payload to `~/.codewu/failed-requests/<ts>-RemoteProtocolError.json` (messages + tools + model). Share that file if you'd like the failure debugged offline.

## Quickstart task

Try this once the model proxy is up:

```
> Create fib.py at the project root with an iterative fib(n) function. Then create test_fib.py with 3 unittest cases. Then run python test_fib.py.
```

Approve each `write_file` and `run_cmd` with `y`. Done.

## Status

Prototype. See SPEC §16 for known limitations and the change log for what landed in each version.
