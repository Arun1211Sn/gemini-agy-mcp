# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""MCP server that lets Claude consult Gemini through the Antigravity CLI (`agy`).

Gemini runs in agy's read-only `plan` mode: it can read files in the given
workspace and search the web, but it cannot edit anything or run commands.
"""

import asyncio
import json
import os
import shutil
import signal

from mcp.server.fastmcp import FastMCP

AGY = os.environ.get("AGY_BIN") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash-high")
TIMEOUT_S = int(os.environ.get("AGY_TIMEOUT", "600"))
EFFORTS = ("low", "medium", "high")

mcp = FastMCP("gemini")

# Appended to every prompt. In plan mode shell commands are auto-denied and Gemini may write a
# plan.md instead of answering; when that happens agy reports SUCCESS with an empty response.
TOOLING_NOTE = (
    "Operating notes: you are running headless in read-only mode. Shell/terminal commands are "
    "NOT available and will be denied, so do not try them. To read files (including images and "
    "PDFs) use your file-viewing tools (view_file, list_dir, grep_search, find_by_name); for the "
    "web use search_web / read_url_content. Do not write a plan or any file: put your complete "
    "answer directly in your reply as plain text/markdown."
)

DOCS_PREAMBLE = (
    "You are a Google/Android platform expert. Answer using Google's CURRENT official "
    "guidance. Search and cite primary sources (developer.android.com, source.android.com, "
    "cloud.google.com, firebase.google.com, developers.google.com, Google GitHub repos, "
    "AndroidX release notes). For each recommendation give: the recommended approach, the "
    "current stable version where relevant, deprecated/discouraged alternatives, and the "
    "source URL. If the docs are ambiguous or you could not verify something, say so "
    "explicitly instead of guessing."
)

REVIEW_PREAMBLE = (
    "You are an independent senior reviewer giving a second opinion. Another AI assistant "
    "produced the work below; do not assume it is correct and do not defer to it. Judge it "
    "on its merits. Report concrete problems first (correctness, security, performance, "
    "maintainability, deviations from official Google/Android best practice), each with "
    "severity (high/medium/low), location, and a suggested fix. Then list what is good. "
    "Then list alternatives worth considering. Be direct; if something is fine, say so briefly."
)

DESIGN_PREAMBLE = (
    "You are an independent architect giving an unbiased critique of a design proposal "
    "written by another AI assistant. Do not anchor on the proposal. First, briefly sketch "
    "how YOU would approach the problem from scratch. Then compare: where the proposal is "
    "weaker, stronger, or riskier than your approach; hidden assumptions; failure modes; "
    "and what Google officially recommends for this kind of problem (cite sources). End "
    "with a clear verdict and the top 3 changes you would make."
)

MEDIA_PREAMBLE = (
    "You are analysing local media files (screenshots, diagrams, photos, PDFs). Open every "
    "listed file with your view_file tool before answering. Describe only what you actually "
    "see; if a file cannot be opened or is unreadable, say so instead of guessing."
)

EMPTY_RETRY_MSG = (
    "Your previous reply reached the caller EMPTY (shell commands are unavailable in this mode "
    "and plan files are not delivered). Write your complete answer directly in this reply as "
    "plain text/markdown, using only file-viewing and web tools if you need more information."
)


class AgyError(Exception):
    pass


def _pick_model(model: str | None, effort: str | None) -> str:
    # agy encodes Gemini effort in the model id (gemini-3.8-flash-low|medium|high); its --effort
    # flag rejects levels the chosen model doesn't have, so we keep using the model suffix.
    if effort is not None and effort not in EFFORTS:
        raise AgyError(f"effort must be one of {', '.join(EFFORTS)} (got {effort!r})")
    base = model or DEFAULT_MODEL
    if effort and base.rsplit("-", 1)[-1] in EFFORTS:
        return base.rsplit("-", 1)[0] + "-" + effort
    return base


def _resolve_workspace(workspace: str | None) -> str:
    # Claude Code starts stdio MCP servers in the project directory, so that's the default.
    cwd = os.path.abspath(os.path.expanduser(workspace)) if workspace else os.getcwd()
    if not os.path.isdir(cwd):
        raise AgyError(f"workspace directory does not exist: {cwd}")
    return cwd


def _resolve_files(files: list[str] | None, cwd: str) -> tuple[list[str], list[str]]:
    """Absolute paths for `files`, plus extra dirs to expose for files outside the workspace."""
    paths, extra_dirs = [], []
    for f in files or []:
        p = os.path.abspath(os.path.join(cwd, os.path.expanduser(f)))
        if not os.path.exists(p):
            raise AgyError(f"file not found: {p}")
        paths.append(p)
        if os.path.commonpath([p, cwd]) != cwd:
            d = p if os.path.isdir(p) else os.path.dirname(p)
            if d not in extra_dirs:
                extra_dirs.append(d)
    return paths, extra_dirs


def _parse_result(stdout: str) -> dict | None:
    # stream-json: one JSON object per line; the last `result` event carries the answer.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            return obj["result"]
    return None


async def _agy_once(prompt: str, cwd: str, model: str, conversation_id: str | None,
                    json_schema: str | None, extra_dirs: list[str]) -> dict:
    args = [AGY, "--input-format", "stream-json", "--output-format", "stream-json",
            "--mode", "plan", "--model", model, "--print-timeout", f"{TIMEOUT_S}s"]
    if conversation_id:
        args += ["--conversation", conversation_id]
    if json_schema:
        args += ["--json-schema", json_schema]
    for d in extra_dirs:
        args += ["--add-dir", d]

    # The prompt goes through stdin, so large diffs don't hit the OS argument-size limit.
    stdin = (json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode()
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=TIMEOUT_S + 30)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # agy and anything it spawned
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        raise AgyError(f"agy timed out after {TIMEOUT_S}s")

    result = _parse_result(out.decode(errors="replace"))
    if result is None:
        tail = (out.decode(errors="replace")[-1500:] + "\n" + err.decode(errors="replace")[-1500:])
        raise AgyError(f"unexpected agy output (exit {proc.returncode}):\n{tail.strip()}")
    if result.get("status") != "SUCCESS":
        raise AgyError(f"Gemini call failed: {result.get('error') or json.dumps(result)[:2000]}")
    return result


async def _call(prompt: str, workspace: str | None, model: str | None, effort: str | None,
                conversation_id: str | None = None, files: list[str] | None = None,
                json_schema: str | None = None) -> dict:
    """Run agy; retries once in the same conversation if Gemini returns an empty answer."""
    cwd = _resolve_workspace(workspace)
    paths, extra_dirs = _resolve_files(files, cwd)
    if paths:
        prompt += "\n\nRead these files before answering:\n" + "\n".join(f"- {p}" for p in paths)
    prompt += "\n\n" + TOOLING_NOTE
    model_id = _pick_model(model, effort)

    result = await _agy_once(prompt, cwd, model_id, conversation_id, json_schema, extra_dirs)
    if not _has_answer(result, json_schema) and result.get("conversation_id"):
        result = await _agy_once(EMPTY_RETRY_MSG, cwd, model_id, result["conversation_id"],
                                 json_schema, extra_dirs)
    if not _has_answer(result, json_schema):
        denied = ", ".join(a.get("display_name") or a.get("action", "?")
                           for a in result.get("denied_actions") or [])
        raise AgyError("Gemini returned an empty answer"
                       + (f" (it tried blocked actions: {denied})" if denied else "")
                       + f". conversation_id: {result.get('conversation_id')}")
    return result


def _has_answer(result: dict, json_schema: str | None) -> bool:
    if json_schema:
        return result.get("structured_output") is not None
    return bool((result.get("response") or "").strip())


def _footer(result: dict) -> str:
    return (f"\n\n---\n[gemini conversation_id: {result.get('conversation_id')} — pass it to "
            f"follow_up to continue this thread]")


async def _text_tool(prompt: str, workspace, model, effort, conversation_id=None, files=None) -> str:
    try:
        result = await _call(prompt, workspace, model, effort, conversation_id, files)
    except AgyError as e:
        return f"Error: {e}"
    return result["response"].strip() + _footer(result)


def _join(*parts: str | None) -> str:
    return "\n\n".join(p for p in parts if p)


@mcp.tool()
async def ask_gemini(question: str, context: str | None = None, files: list[str] | None = None,
                     workspace: str | None = None, model: str | None = None,
                     effort: str | None = None) -> str:
    """Ask Gemini a general question. Gemini can read files and search the web, but cannot modify
    anything or run commands. `files` are paths (relative to `workspace`, which defaults to the
    current project) Gemini should read. `effort`: low|medium|high (default high).
    Calls take 30s–5min."""
    return await _text_tool(_join(question, context and f"Additional context:\n{context}"),
                            workspace, model, effort, files=files)


@mcp.tool()
async def google_recommended_approach(topic: str, context: str | None = None,
                                      files: list[str] | None = None, workspace: str | None = None,
                                      model: str | None = None, effort: str | None = None) -> str:
    """Get Google's current officially recommended way to do something for Google-maintained tech
    (Android, AOSP, Jetpack/AndroidX, Compose, Gradle/AGP, Kotlin on Android, Firebase, GCP,
    Google APIs, Play policies, Flutter, etc.), with documentation links and version numbers.
    Use when unsure if knowledge is outdated or when choosing between Google-maintained options."""
    return await _text_tool(
        _join(DOCS_PREAMBLE, f"Topic / question:\n{topic}",
              context and f"Additional context:\n{context}"),
        workspace, model, effort, files=files)


@mcp.tool()
async def review(subject: str, content: str | None = None, files: list[str] | None = None,
                 focus: str | None = None, workspace: str | None = None,
                 model: str | None = None, effort: str | None = None) -> str:
    """Independent, unbiased review by Gemini of code, a diff, a plan, or a document.
    Pass the material inline as `content` (e.g. the output of `git diff`; Gemini cannot run git
    or any other command) and/or as `files` (relative to `workspace`). `focus` narrows the review."""
    body = f"What to review: {subject}"
    if focus:
        body += f"\nFocus on: {focus}"
    if content:
        body += f"\n\n<material>\n{content}\n</material>"
    return await _text_tool(_join(REVIEW_PREAMBLE, body), workspace, model, effort, files=files)


@mcp.tool()
async def design_critique(problem: str, proposal: str, constraints: str | None = None,
                          files: list[str] | None = None, workspace: str | None = None,
                          model: str | None = None, effort: str | None = None) -> str:
    """Unbiased second opinion on a design/architecture proposal. Gemini first sketches its own
    approach independently, then compares it with `proposal` and gives a verdict."""
    body = f"Problem:\n{problem}\n\nProposal under review:\n{proposal}"
    if constraints:
        body += f"\n\nConstraints:\n{constraints}"
    return await _text_tool(_join(DESIGN_PREAMBLE, body), workspace, model, effort, files=files)


@mcp.tool()
async def analyze_media(prompt: str, media_files: list[str], workspace: str | None = None,
                        model: str | None = None, effort: str | None = None) -> str:
    """Have Gemini look at images (screenshots, UI mockups, diagrams, photos) and PDFs and answer
    `prompt` about them, e.g. "what's wrong with this layout?" or "summarise section 3 of this
    spec". `media_files` are paths (absolute, or relative to `workspace`)."""
    if not media_files:
        return "Error: media_files is empty"
    return await _text_tool(_join(MEDIA_PREAMBLE, prompt), workspace, model, effort,
                            files=media_files)


@mcp.tool()
async def structured_query(prompt: str, json_schema: dict | str, context: str | None = None,
                           files: list[str] | None = None, workspace: str | None = None,
                           model: str | None = None, effort: str | None = None) -> str:
    """Ask Gemini something and get back JSON that is validated against `json_schema` (a JSON
    Schema object, or a JSON string of one). Use when you need machine-readable output, e.g. a
    list of findings with severity/location, version tables, or yes/no verdicts with reasons.
    Returns {"data": <the JSON>, "conversation_id": "..."}."""
    if isinstance(json_schema, str):
        try:
            json_schema = json.loads(json_schema)
        except json.JSONDecodeError as e:
            return f"Error: json_schema is not valid JSON: {e}"
    try:
        result = await _call(_join(prompt, context and f"Additional context:\n{context}"),
                             workspace, model, effort, files=files,
                             json_schema=json.dumps(json_schema))
    except AgyError as e:
        return f"Error: {e}"
    return json.dumps({"data": result["structured_output"],
                       "conversation_id": result.get("conversation_id")}, indent=2)


@mcp.tool()
async def follow_up(conversation_id: str, message: str, context: str | None = None,
                    files: list[str] | None = None, workspace: str | None = None,
                    model: str | None = None, effort: str | None = None) -> str:
    """Continue an earlier Gemini conversation (use the conversation_id printed at the end of a
    previous answer), e.g. to push back on a point, ask for sources, or share more `files`."""
    return await _text_tool(_join(message, context and f"Additional context:\n{context}"),
                            workspace, model, effort, conversation_id, files)


@mcp.tool()
async def agy_info(category: str = "models") -> str:
    """List what the local agy install offers: `models` (valid values for the `model` argument),
    `agents`, `plugins`, or `mcp` (MCP servers configured in agy)."""
    sub = {"models": ["models"], "agents": ["agents"], "plugins": ["plugin", "list"],
           "mcp": ["mcp", "list"]}.get(category)
    if sub is None:
        return "Error: category must be one of models, agents, plugins, mcp"
    proc = await asyncio.create_subprocess_exec(
        AGY, *sub, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return "Error: agy timed out"
    text = out.decode(errors="replace").strip()
    if category == "models":
        text += f"\n\n(default model: {DEFAULT_MODEL})"
    return text


if __name__ == "__main__":
    mcp.run()
