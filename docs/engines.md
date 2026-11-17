# Engines: API vs free

The same jobs (the same prompts and tools) can run on two engines. Pick one with `--engine` or in the web UI.

| | `--engine api` | `--engine free` |
|---|---|---|
| Runs on | Anthropic API (`agent.py`) | Headless Claude Code (`free_engine.py`) |
| Cost | Pay per token (API credit) | Included in your Claude subscription |
| Needs | `ANTHROPIC_API_KEY` in `.env` | Claude Code installed at `~/.local/bin/claude` and logged in |
| Tool calls | Native tool use | Claude Code runs `tools_cli.py` through Bash |

Both engines call `agent.build_job(mode, target, …)`, which returns the same system prompt and task, so reports look the same.

## API engine (`agent.py`)

A hand-written agent loop:

1. Send the goal, the system prompt and `TOOL_DEFINITIONS` (10 tools with strict JSON schemas) to `claude-opus-5-5` with adaptive thinking.
2. If Claude replies with `tool_use` blocks, run them with `tools.run_tool` and send back `tool_result` blocks.
3. Repeat until Claude stops asking for tools. That reply is the report.

Safety rails:
- `MAX_STEPS = 15`, so a confused agent can't loop and bill forever.
- Handles the `refusal`, `pause_turn` and `max_tokens` stop reasons.
- `load_dotenv(override=True)`, so the key in `.env` wins over an old one in your shell.

## Free engine (`free_engine.py`)

Runs `claude -p` (non-interactive Claude Code) with:

- `--append-system-prompt`: the same system prompt, plus a help text listing the `tools_cli.py` commands
- `--allowedTools "Bash(.venv/bin/python tools_cli.py *)"`: it may run **only** our tools. Article mode also gets `Edit(drafts/**)`.
- `--permission-mode dontAsk`: anything not allowed is refused rather than prompted
- `--output-format stream-json --verbose`: progress is parsed live and shown as steps
- `ANTHROPIC_API_KEY` **removed** from the environment, so it uses your subscription login and not API credit
- Bash timeouts raised to 10 minutes for long crawls

The command is built as an argument list (no shell), so a URL can't inject shell commands.

First run: if you see "Not logged in", run `claude` once in a terminal and log in.
