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

import waldoctl
from waldoctl import EditId

from waldo_commander.mcp.server import get_mcp

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
async def get_source(program_id: str | None = None) -> str:
    """Current editor source for ``program_id`` (defaults to active)."""
    return _program(program_id).source


@mcp.tool(name="programs.open")
async def open_program(path: str) -> str:
    """Open a program by file path. Returns the new (or focused) program id."""
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
    """Create a new program tab. Returns its id."""
    return waldoctl.commander.programs.new(
        source=source, filename=filename, file_path=file_path
    ).id


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
) -> str:
    """Queue a unified-diff edit on ``program_id`` (defaults to active).

    The diff must apply cleanly against the program's current source;
    invalid or non-applicable diffs raise immediately so the caller can
    retry. Returns the new pending edit's id. The edit is **not**
    applied — a human must approve it in WC's editor before the source
    actually changes.
    """
    p = _program(program_id)
    return p.edits.propose(diff, description).value


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
