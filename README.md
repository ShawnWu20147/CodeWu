# CodeWu

A minimal coding agent prototype. Single-file Python (`main.py`), one external dep (`openai`), four local tools, per-call y/n approval for any side effect.

> Builds JS or Python programs. See [SPEC.md](./SPEC.md) for the design contract.

## Requirements

- Python 3.10+
- A local OpenAI-compatible Chat Completions endpoint that supports tool calling. Default config points at `http://localhost:4141/v1` and `claude-opus-4.6-1m`.

## Install

```powershell
pip install -r requirements.txt
```

## Run

```powershell
# new session
python main.py

# resume the most recent session
python main.py --resume

# resume a specific session
python main.py --resume 20260516-201437-21d227

# bypass tool approval prompts (previews still print) — use with care
python main.py --allow-all
```

## Configuration (env vars, all optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `CODEWU_BASE_URL` | `http://localhost:4141/v1` | Chat Completions endpoint |
| `CODEWU_MODEL`    | `claude-opus-4.6-1m`       | Model name |
| `CODEWU_API_KEY`  | placeholder                | Sent to the SDK; the proxy doesn't validate |

## Tools

| Tool | Side effect | Approval |
|------|-------------|----------|
| `read_file(path)` | none | auto |
| `list_dir(path)`  | none | auto |
| `write_file(path, content)` | writes a file | **y/n** |
| `run_cmd(command)` | runs shell | **y/n** (or `edit` to modify before running) |

`run_cmd` uses PowerShell on Windows, `sh` on POSIX.

## In-REPL commands

| Command | Effect |
|---------|--------|
| `/help` (or bare `/`) | List all commands |
| `/exit` / `/quit` | Quit; prints `--resume` hint with session id |
| `/sessions` | List saved sessions in `.codewu/sessions/` |
| `/resume [id]` | Switch to another saved session (defaults to latest); replays history |
| `/new` | Start a fresh session |
| `/dump` | Show message count + last 3 message previews (debug) |

## Session storage

Each turn is persisted atomically to `.codewu/sessions/<id>.json`, and `latest.json` is updated as a pointer for `--resume`.

## Quickstart task

Try this once the server at `:4141` is up:

```
> Create fib.py at the project root with an iterative fib(n) function. Then create test_fib.py with 3 unittest cases. Then run python test_fib.py.
```

Approve each `write_file` and `run_cmd` with `y`. Done.

## Status

Prototype. See SPEC §16 for known limitations.
