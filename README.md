# gemini-agy-mcp

An MCP server that lets Claude (or any MCP client) consult Gemini through the
Antigravity CLI (`agy`), for:

- Google's current officially recommended approach for Android, AOSP, Jetpack/Compose,
  Gradle/AGP, Firebase, GCP, Flutter, Play and other Google-maintained tech, with doc links
- independent, unbiased reviews of code, diffs, plans and documents
- design critiques: Gemini sketches its own approach first, then compares

Gemini runs in agy's read-only `plan` mode: it can read files (including images and PDFs)
and search the web, but it cannot modify anything or run commands (so pass diffs inline).

## Tools

| Tool | Purpose |
|---|---|
| `google_recommended_approach` | Official Google guidance, versions, deprecations, source URLs |
| `review` | Independent review of code / diff / plan / doc |
| `design_critique` | Unbiased second opinion on a design proposal |
| `ask_gemini` | General question |
| `analyze_media` | Gemini looks at screenshots, diagrams, photos and PDFs |
| `structured_query` | Answer as JSON validated against a JSON Schema you supply |
| `follow_up` | Continue a previous Gemini conversation by `conversation_id` (can add files/context) |
| `agy_info` | List agy's models, agents, plugins or MCP servers |

The Gemini tools accept `workspace` (directory Gemini runs in; defaults to the current
project), `files` (paths to read; files outside the workspace are exposed with `--add-dir`),
`model`, and `effort` (`low` / `medium` / `high`, which selects the Gemini variant).

Prompts are sent to agy over stdin (stream-json), so large diffs are fine. If Gemini
returns an empty answer, the server asks once more in the same conversation.

## Requirements

- `agy` (Antigravity CLI), logged in
- [`uv`](https://docs.astral.sh/uv/) (dependencies are declared inline in `server.py`)

## Install (Claude Code)

```sh
claude mcp add gemini --scope user -- uv run --script /path/to/gemini-agy-mcp/server.py
```

Calls take 30 seconds to several minutes, so raise Claude Code's MCP tool timeout,
e.g. in `~/.claude/settings.json`:

```json
{ "env": { "MCP_TOOL_TIMEOUT": "660000" } }
```

## Configuration (environment variables)

| Variable | Default |
|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash-high` |
| `AGY_TIMEOUT` | `600` (seconds) |
| `AGY_BIN` | `agy` on `PATH`, else `~/.local/bin/agy` |

## Caveat

Gemini's answers, including version numbers, can be wrong or inconsistent between
runs. Check the cited source before acting on them.
