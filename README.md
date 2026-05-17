# CodeWu

A minimal coding agent prototype. Pure-Python package (`codewu/`), one external dep (`openai`), four local tools, per-call y/n approval for any side effect, ANSI colors, session resume.

> Builds JS or Python programs. See [SPEC.md](./SPEC.md) for the design contract.

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

The installer detects Python, refuses if a `codewu.exe` is already running (you must `/exit` first), then runs `pip install -e .` and prints usage hints with your config/session paths.

**Linux / macOS:**
```bash
git clone https://github.com/ShawnWu20147/CodeWu.git
cd CodeWu
./install.sh
```

### Manual install (if you don't want to run the script)

```powershell
pip install -e D:\path\to\CodeWu
```

After installing either way, `codewu` is on your `PATH` and you can run it from **any** working directory. The session log (`~/.codewu/sessions/`) is stored globally; each saved session records the cwd it was started in so you can tell projects apart in `/sessions`.

### Run without installing

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

### Inline file references — `@path`

Type `@` anywhere in your message to reference a file:

```
> what does @codewu/cli.py do?
> compare @SPEC.md and @README.md and suggest fixes
```

When you press Enter the file contents are inlined into the message (wrapped in `<file path="...">...</file>` blocks) so the agent doesn't need to call `read_file`. While you type, a completion menu of files in the current directory is shown with startswith filtering; `@<dir>/` navigates one level deeper. Typos get caught at submit time with fuzzy suggestions (`did you mean: README.md?`).

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
