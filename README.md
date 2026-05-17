# CodeWu

A minimal coding agent prototype. Pure-Python package (`codewu/`), one external dep (`openai`), four local tools, per-call y/n approval for any side effect, ANSI colors, session resume.

> Builds JS or Python programs. See [SPEC.md](./SPEC.md) for the design contract.

## Requirements

- Python 3.10+
- A local OpenAI-compatible Chat Completions endpoint that supports tool calling. Default config points at `http://localhost:4141/v1` and `claude-opus-4.6-1m`.

## Install

### Option A — install globally as a CLI (recommended)

```powershell
pip install -e D:\path\to\CodeWu
```

After this, `codewu` is on your `PATH` and you can run it from **any** working directory. The session log (`~/.codewu/sessions/`) is stored globally; each saved session records the cwd it was started in so you can tell projects apart in `/sessions`.

### Option B — run directly without install

```powershell
pip install -r requirements.txt
python -m codewu
```

## Run

```powershell
# new session in current directory
codewu

# resume the most recent session in current directory
codewu --resume

# resume a specific session
codewu --resume 20260516-201437-21d227

# bypass tool approval prompts (previews still print) — use with care
codewu --allow-all
```

## Configuration (env vars, all optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `CODEWU_BASE_URL` | `http://localhost:4141/v1` | Chat Completions endpoint |
| `CODEWU_MODEL`    | `claude-opus-4.6-1m`       | Model name |
| `CODEWU_API_KEY`  | placeholder                | Sent to the SDK; the proxy doesn't validate |
| `NO_COLOR` / `CODEWU_NO_COLOR` | unset | Set to any value to disable ANSI colors. Colors also auto-disable when stdout is not a TTY. |

## Tools

| Tool | Side effect | Approval |
|------|-------------|----------|
| `read_file(path)` | none | auto |
| `list_dir(path)`  | none | auto |
| `write_file(path, content)` | writes a file | **y/n** |
| `run_cmd(command)` | runs shell | **y/n** (or `edit` to modify before running) |

`run_cmd` uses PowerShell on Windows, `sh` on POSIX.

## In-REPL commands

### Slash commands

| Command | Effect |
|---------|--------|
| `/help` (or bare `/`) | List all commands |
| `/exit` / `/quit` (or `Ctrl+C` / `Ctrl+D`) | Quit; prints `--resume` hint with session id |
| `/sessions` | List saved sessions in `.codewu/sessions/` |
| `/resume [id]` | Switch to another saved session (defaults to latest); replays history |
| `/new` | Start a fresh session |
| `/dump` | Show message count + last 3 message previews (debug) |

### Bang shortcut — run a shell command directly

Prefix any line with `!` to run it in the shell yourself. The command runs **without approval** (you typed it), the output is shown, and the (`command`, `output`) pair is appended to the conversation context so the agent sees it on the next turn.

```
> !git status
> !npm test
> !ls -la
```

## Session storage

Each turn is persisted atomically to `~/.codewu/sessions/<id>.json` (global, shared across all projects), and `latest.json` is updated as a pointer for `--resume`. The session JSON also records the `cwd` it was started in, so `/sessions` shows you which project each session belongs to.

## Quickstart task

Try this once the server at `:4141` is up:

```
> Create fib.py at the project root with an iterative fib(n) function. Then create test_fib.py with 3 unittest cases. Then run python test_fib.py.
```

Approve each `write_file` and `run_cmd` with `y`. Done.

## Status

Prototype. See SPEC §16 for known limitations.
