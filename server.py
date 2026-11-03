# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""MCP server that lets Claude consult Gemini through the Antigravity CLI (`agy`).

Gemini runs in agy's read-only `plan` mode: it can read files in the given
workspace and search the web, but it cannot edit anything.
"""

import asyncio
import json
import os
import shutil

from mcp.server.fastmcp import FastMCP

AGY = os.environ.get("AGY_BIN") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash-high")
TIMEOUT_S = int(os.environ.get("AGY_TIMEOUT", "600"))

mcp = FastMCP("gemini")

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


def _pick_model(model: str | None, effort: str | None) -> str:
    # agy encodes Gemini effort in the model id (gemini-3.8-flash-low|medium|high).
    if model:
        return model
    if effort in ("low", "medium", "high") and DEFAULT_MODEL.startswith("gemini-") \
            and DEFAULT_MODEL.rsplit("-", 1)[-1] in ("low", "medium", "high"):
        return DEFAULT_MODEL.rsplit("-", 1)[0] + "-" + effort
    return DEFAULT_MODEL


async def _run_agy(prompt: str, workspace: str | None, model: str | None,
                   effort: str | None, conversation_id: str | None) -> str:
    cwd = os.path.expanduser(workspace) if workspace else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return f"Error: workspace directory does not exist: {cwd}"

    args = [AGY, "-p", prompt, "--mode", "plan", "--output-format", "json",
            "--model", _pick_model(model, effort), "--print-timeout", f"{TIMEOUT_S}s"]
    if conversation_id:
        args += ["--conversation", conversation_id]

    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S + 30)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: agy timed out after {TIMEOUT_S}s"

    text = out.decode(errors="replace").strip()
    try:
        data = json.loads(text[text.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return f"Error: unexpected agy output (exit {proc.returncode}):\n{text}\n{err.decode(errors='replace')}"

    if data.get("status") != "SUCCESS":
        return f"Gemini call failed: {json.dumps(data)[:2000]}"
    return (f"{data.get('response', '').strip()}\n\n"
            f"---\n[gemini conversation_id: {data.get('conversation_id')} — pass it to "
            f"follow_up to continue this thread]")


def _with_context(preamble: str, body: str, context: str | None, files: list[str] | None) -> str:
    parts = [preamble, "", body]
    if files:
        parts += ["", "Read these files from the workspace before answering:",
                  *[f"- {f}" for f in files]]
    if context:
        parts += ["", "Additional context:", context]
    return "\n".join(parts)


@mcp.tool()
async def ask_gemini(question: str, context: str | None = None, files: list[str] | None = None,
                     workspace: str | None = None, model: str | None = None,
                     effort: str | None = None) -> str:
    """Ask Gemini a general question. Gemini can read files under `workspace` and search the web,
    but cannot modify anything. `files` are paths (relative to workspace) Gemini should read.
    `effort`: low|medium|high (picks the Gemini variant; default high). Calls take 30s–5min."""
    return await _run_agy(_with_context("", question, context, files), workspace, model, effort, None)


@mcp.tool()
async def google_recommended_approach(topic: str, context: str | None = None,
                                      files: list[str] | None = None, workspace: str | None = None,
                                      model: str | None = None, effort: str | None = None) -> str:
    """Get Google's current officially recommended way to do something for Google-maintained tech
    (Android, AOSP, Jetpack/AndroidX, Compose, Gradle/AGP, Kotlin on Android, Firebase, GCP,
    Google APIs, Play policies, Flutter, etc.), with documentation links and version numbers.
    Use when unsure if knowledge is outdated or when choosing between Google-maintained options."""
    return await _run_agy(_with_context(DOCS_PREAMBLE, f"Topic / question:\n{topic}", context, files),
                          workspace, model, effort, None)


@mcp.tool()
async def review(subject: str, content: str | None = None, files: list[str] | None = None,
                 focus: str | None = None, workspace: str | None = None,
                 model: str | None = None, effort: str | None = None) -> str:
    """Independent, unbiased review by Gemini of code, a diff, a plan, or a document.
    Pass the material inline as `content` and/or as `files` (relative to `workspace`; for a git diff,
    you can also say e.g. 'run git diff main' in `focus`). `focus` narrows the review."""
    body = f"What to review: {subject}"
    if focus:
        body += f"\nFocus on: {focus}"
    if content:
        body += f"\n\n<material>\n{content}\n</material>"
    return await _run_agy(_with_context(REVIEW_PREAMBLE, body, None, files), workspace, model, effort, None)


@mcp.tool()
async def design_critique(problem: str, proposal: str, constraints: str | None = None,
                          files: list[str] | None = None, workspace: str | None = None,
                          model: str | None = None, effort: str | None = None) -> str:
    """Unbiased second opinion on a design/architecture proposal. Gemini first sketches its own
    approach independently, then compares it with `proposal` and gives a verdict."""
    body = f"Problem:\n{problem}\n\nProposal under review:\n{proposal}"
    if constraints:
        body += f"\n\nConstraints:\n{constraints}"
    return await _run_agy(_with_context(DESIGN_PREAMBLE, body, None, files), workspace, model, effort, None)


@mcp.tool()
async def follow_up(conversation_id: str, message: str, workspace: str | None = None,
                    model: str | None = None, effort: str | None = None) -> str:
    """Continue an earlier Gemini conversation (use the conversation_id printed at the end of a
    previous answer), e.g. to push back on a point or ask for sources."""
    return await _run_agy(message, workspace, model, effort, conversation_id)


if __name__ == "__main__":
    mcp.run()
