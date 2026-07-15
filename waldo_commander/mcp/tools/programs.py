"""MCP tools for the open-programs surface — ``commander.programs.*``.

Code edits flow through :func:`propose_edit` →
:func:`list_pending_edits` → :func:`cancel_pending_edit` and are
applied / discarded by a human via the editor's diff overlay. There is
**no** ``set_source`` tool by design — every LLM edit must be reviewed
before it touches the program.

Tools are ``async`` so FastMCP runs them on WC's event loop: the program
verbs (``open`` / ``new`` / ``close`` / ``switch``) fire ``notify_changed``,
and the editor's ``EditorPanel._reconcile_tabs`` listener turns that into
NiceGUI element creation/teardown (tab widgets, diff overlay) on the connected
page — so an MCP-opened program renders exactly like one opened in the GUI.
This is loop-affine, which is why the tools must run on the loop.
"""

from __future__ import annotations

import ast
import asyncio

import waldoctl
from waldoctl import EditId

from waldo_commander.constants import default_program_dir
from waldo_commander.mcp.server import get_mcp
from waldo_commander.services import edit_decisions

mcp = get_mcp()


def _program(program_id: str | None):
    """Resolve ``program_id`` to a Program. ``None`` means the active one.

    Raises ``KeyError`` if ``program_id`` isn't open or there's no
    active program.
    """
    tabs = waldoctl.commander.programs
    if program_id is None:
        p = tabs.active
        if p is None:
            raise KeyError("no active program")
        return p
    return tabs[program_id]


@mcp.tool(name="programs.list")
async def list_programs() -> list[dict]:
    """All currently open programs."""
    tabs = waldoctl.commander.programs
    return [
        {
            "id": p.id,
            "filename": p.filename,
            "file_path": p.file_path,
            "is_dirty": p.is_dirty,
            "is_active": p.id == tabs.active_id,
        }
        for p in tabs.items
    ]


@mcp.tool(name="programs.get_active")
async def get_active() -> dict | None:
    """Identifier and source of the active program, or ``None`` if none are open."""
    p = waldoctl.commander.programs.active
    if p is None:
        return None
    return {
        "id": p.id,
        "filename": p.filename,
        "file_path": p.file_path,
        "is_dirty": p.is_dirty,
        "source": p.source,
    }


@mcp.tool(name="programs.get_source")
async def get_source(program_id: str | None = None, numbered: bool = False) -> str:
    """Current editor source for ``program_id`` (defaults to active).

    Pass ``numbered=True`` for ``N<TAB>line`` output — read that immediately
    before ``programs.propose_edit`` so your hunk headers and context lines
    match the real line numbers and text exactly (diffs are applied with NO
    fuzzy matching).
    """
    src = _program(program_id).source
    if not numbered:
        return src
    return "\n".join(f"{n}\t{line}" for n, line in enumerate(src.splitlines(), 1))


@mcp.tool(name="programs.list_library")
async def list_library() -> list[dict]:
    """On-disk program library — saved programs and worked examples, openable
    with ``programs.open``.

    Programs are plain Python scripts run in a subprocess; they drive the
    robot through the backend client library (NOT these MCP tools). Do not
    guess that API: before authoring your first program, open an example
    from here and read its imports and motion calls.
    """
    out = []
    for f in sorted(default_program_dir().glob("*.py")):
        try:
            doc = ast.get_docstring(ast.parse(f.read_text())) or ""
        except (OSError, SyntaxError):
            doc = ""
        out.append(
            {
                "filename": f.name,
                "path": str(f),
                "summary": doc.splitlines()[0] if doc else "",
            }
        )
    return out


@mcp.tool(name="programs.open")
async def open_program(path: str) -> str:
    """Open a program by file path (see ``programs.list_library`` for what's
    on disk). Returns the new (or focused) program id."""
    return waldoctl.commander.programs.open(path).id


@mcp.tool(name="programs.close")
async def close_program(program_id: str) -> None:
    """Close the program with the given id."""
    waldoctl.commander.programs.close(program_id)


@mcp.tool(name="programs.switch")
async def switch_program(program_id: str) -> None:
    """Make ``program_id`` the active program."""
    waldoctl.commander.programs.switch(program_id)


@mcp.tool(name="programs.new")
async def new_program(
    source: str = "",
    filename: str = "untitled.py",
    file_path: str | None = None,
) -> str:
    """Create a new program tab, make it ACTIVE, and return its id.

    Name your program (don't leave the default ``untitled.py``). If a tab
    with the same filename is already open — e.g. this call is a retry after
    a reconnect — that tab is switched to and its id returned unchanged
    instead of stacking a duplicate; ``untitled.py`` is exempt so the human's
    scratch tab is never taken over.
    """
    tabs = waldoctl.commander.programs
    if filename != "untitled.py":
        existing = next((p for p in tabs.items if p.filename == filename), None)
        if existing is not None:
            tabs.switch(existing.id)
            return existing.id
    program = tabs.new(source=source, filename=filename, file_path=file_path)
    tabs.switch(program.id)
    return program.id


@mcp.tool(name="programs.save")
async def save_program(program_id: str | None = None, path: str | None = None) -> None:
    """Persist the program's source to disk (uses its ``file_path`` if ``path``
    is None)."""
    _program(program_id).save(path)


@mcp.tool(name="programs.get_log")
async def get_log(program_id: str | None = None) -> list[dict]:
    """Captured stdout/stderr lines for ``program_id`` (defaults to active)."""
    p = _program(program_id)
    return [
        {"timestamp": e.timestamp, "stream": e.stream, "text": e.text}
        for e in p.log.entries
    ]


# --------------------------------------------------------------------------
# Diff-edit lifecycle (LLM-proposed edits, human-approved)
# --------------------------------------------------------------------------


@mcp.tool(name="programs.propose_edit")
async def propose_edit(
    diff: str,
    description: str = "",
    program_id: str | None = None,
) -> dict:
    """Queue a unified-diff edit on ``program_id`` (defaults to active).

    This is the preferred way to author ANY code — a repeatable routine or a
    quick throwaway — because the edit shows up in the human's editor as a diff
    they can see and scrub. Create/switch to a target program first with
    ``programs.new`` / ``programs.open`` / ``programs.switch``, and read
    ``programs.get_source(numbered=True)`` immediately before proposing: the
    diff must apply against the current source EXACTLY (line numbers, context,
    whitespace — no fuzzy matching). Invalid or non-applicable diffs raise
    immediately so the caller can fix and retry.

    Returns ``{"id", "status"}`` where ``status`` is ``"applied"`` (the
    control mode auto-applies edits and it's already in the source) or
    ``"pending"`` (a human must approve it in the editor — tell them what you
    proposed, then await the outcome with ``programs.wait_edit_decision``).
    """
    p = _program(program_id)
    edit_id = p.edits.propose(diff, description).value
    # Auto-apply (Auto-edits / Autopilot) runs synchronously in the editor's
    # edit-change listener, so by now the edit either left pending or didn't.
    pending = any(e.id.value == edit_id for e in p.edits.pending)
    return {"id": edit_id, "status": "pending" if pending else "applied"}


@mcp.tool(name="programs.list_pending_edits")
async def list_pending_edits(program_id: str | None = None) -> list[dict]:
    """Pending (not-yet-approved) edits on ``program_id`` (defaults to active)."""
    p = _program(program_id)
    return [
        {
            "id": e.id.value,
            "description": e.description,
            "proposed_at": e.proposed_at,
            "diff": e.diff,
        }
        for e in p.edits.pending
    ]


@mcp.tool(name="programs.cancel_pending_edit")
async def cancel_pending_edit(edit_id: str, program_id: str | None = None) -> None:
    """Withdraw a pending edit (e.g. the LLM realised it was wrong).

    Equivalent to the human clicking Reject in the editor — the edit is
    discarded without being applied.
    """
    p = _program(program_id)
    p.edits.reject(EditId(edit_id))
    edit_decisions.record(edit_id, "withdrawn")


def _edit_pending_anywhere(edit_id: str) -> bool:
    return any(
        e.id.value == edit_id
        for p in waldoctl.commander.programs.items
        for e in p.edits.pending
    )


@mcp.tool(name="programs.wait_edit_decision")
async def wait_edit_decision(edit_id: str, timeout: float = 45.0) -> dict:
    """Block until the human decides a pending edit, up to ``timeout`` seconds.

    Returns ``{"status": ...}`` — ``"applied"`` / ``"rejected"`` /
    ``"withdrawn"`` once decided, ``"pending"`` if the timeout expired first
    (just call again to keep waiting), or ``"unknown"`` for an id that is
    neither pending nor on record. Use this instead of polling
    ``programs.list_pending_edits`` in a loop.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        outcome = edit_decisions.get(edit_id)
        if outcome is not None:
            return {"status": outcome}
        if not _edit_pending_anywhere(edit_id):
            return {"status": "unknown"}
        if asyncio.get_event_loop().time() >= deadline:
            return {"status": "pending"}
        await asyncio.sleep(0.5)
