"""Revision-checked object edits, including source captured while recording."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import waldoctl
from waldoctl.shapes import Shape, param_names

from waldo_commander.services.control_lease import require_browser_control
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.state import ui_state


if TYPE_CHECKING:
    from waldo_commander.services.motion_recorder import SceneRecordingCheckpoint
    from waldo_commander.services.urdf_scene.scene_handle import WcSceneHandle


def plain(shape: Shape) -> Shape:
    """*shape* with Python floats, so recorded source never spells numpy scalars."""
    return replace(
        shape,
        pose=tuple(float(v) for v in shape.pose),
        margin=None if shape.margin is None else float(shape.margin),
        **{k: float(getattr(shape, k)) for k in param_names(type(shape))},
    )


@dataclass(frozen=True)
class WorldEdit:
    label: str
    before: tuple[Shape, ...]
    after: tuple[Shape, ...]
    source_before: str
    source_after: str
    program: str | None
    recording_before: SceneRecordingCheckpoint
    recording_after: SceneRecordingCheckpoint
    renames: tuple[tuple[str, str], ...]
    draft_before: tuple[Shape, ...]
    draft_after: tuple[Shape, ...]


class SceneHistory:
    def __init__(self, owner) -> None:
        self.owner = owner
        self.undo_stack: list[WorldEdit] = []
        self.redo_stack: list[WorldEdit] = []
        self.busy = False
        self.feedback = ""
        self.expected: tuple | None = None

    @staticmethod
    def _handle() -> WcSceneHandle:
        from waldo_commander.services.urdf_scene.scene_handle import WcSceneHandle

        handle = waldoctl.commander.scene
        if not isinstance(handle, WcSceneHandle):
            raise ValueError("Object editing is unavailable in this view")
        return handle

    @staticmethod
    def document() -> tuple[str | None, str]:
        """The active program's id and source."""
        program = waldoctl.commander.programs.active
        return (program.id, program.source) if program else (None, "")

    @staticmethod
    def _recording_key() -> tuple:
        return (
            id(motion_recorder.session),
            is_any_program_recording(),
            motion_recorder.scene_revision,
        )

    def context(self) -> tuple:
        return (self._handle().revision, *self.document(), *self._recording_key())

    def validate(self) -> bool:
        """Clear the history if what it would restore has changed elsewhere;
        whether it was cleared."""
        if self.busy or self.expected is None or self.expected == self.context():
            return False
        self._clear(
            "History cleared: the world, program, or recording session changed elsewhere"
        )
        self.expected = None
        return True

    def _clear(self, feedback: str) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.feedback = feedback

    def guard(self, revision: tuple, *, confirmed: bool = True) -> WcSceneHandle:
        """The scene handle, if an edit begun at *revision* may commit now;
        *confirmed* False admits it over a world the controller refused."""
        self.validate()
        handle = self._handle()
        if self.busy or (confirmed and not handle.confirmed):
            raise ValueError("Wait for the current object edit to be confirmed")
        if self.context() != revision:
            raise ValueError(
                "The world or program changed during this edit. Review the current object and try again"
            )
        if motion_recorder.scene_edit_busy:
            raise ValueError(
                "Finish the current recording operation before editing objects"
            )
        if is_any_program_running():
            raise ValueError("Stop program execution before editing objects")
        if not self.owner._has_control() or not require_browser_control(
            self.owner.scene.client.id
        ):
            raise ValueError("This browser does not have authoring control")
        return handle

    def _set_source(self, program_id: str | None, source: str) -> None:
        program = waldoctl.commander.programs.active
        if program is None or program.id != program_id:
            return
        # Whichever page's editor shows the program takes the text, or its
        # next change would write the old text back.
        editor = ui_state.active_textarea
        if editor is not None and not editor.is_deleted:
            editor.commit_scene_value(source)
        else:
            program.source = source

    @staticmethod
    def validate_names(shapes: tuple[Shape, ...]) -> None:
        """Refuse empty or repeated names; a shape checks its own geometry
        when it is built."""
        names = [s.name for s in shapes]
        if any(not isinstance(n, str) or not n.strip() for n in names) or len(
            set(names)
        ) != len(names):
            raise ValueError("Each object needs a unique, nonempty name")

    async def _apply(
        self,
        handle: WcSceneHandle,
        target: tuple[Shape, ...],
        draft: tuple[Shape, ...],
        renames: tuple[tuple[str, str], ...],
        rejected: str,
        confirmed: Callable[[tuple, tuple], None],
    ) -> None:
        """Assign *target* with the installation proposal *draft* and wait for
        the controller; *confirmed* gets the document and recording key from
        before the assignment."""
        world = tuple(handle.shapes)
        layers = (handle.installation, handle.installation_draft)
        document = self.document()
        recording = self._recording_key()
        self.busy = True
        self.feedback = "Waiting for controller confirmation…"
        self.owner._set_object_renames(renames)
        self.owner._refresh_object_inspector()
        try:
            task = handle._assign(list(target), draft, push=True, record=False)
            request_revision = handle.revision
            if task is not None:
                await task
            await handle.wait_for_confirmation(request_revision)
            if not handle.confirmed or tuple(handle.shapes) != target:
                raise ValueError(handle.last_error or rejected)
            confirmed(document, recording)
        except Exception as error:
            if (
                handle.confirmed
                and tuple(handle.shapes) == world
                and self.document() == document
                and self._recording_key() == recording
                and (handle.installation, handle.installation_draft) == layers
            ):
                self.expected = self.context()
            self.feedback = str(error)
            raise
        finally:
            self.busy = False
            self.owner._set_object_renames(())
            self.owner._refresh_object_inspector()

    async def commit(
        self,
        label: str,
        after: list[Shape],
        *,
        revision: tuple,
        renames: tuple[tuple[str, str], ...] = (),
        draft: tuple[Shape, ...] | None = None,
    ) -> None:
        handle = self.guard(revision)
        before = tuple(handle.shapes)
        target = tuple(plain(s) for s in after)
        draft_before = handle.installation_draft
        draft_after = draft_before if draft is None else draft
        self.validate_names(target)
        if before == target and draft_before == draft_after:
            return

        def confirmed(document: tuple, recording: tuple) -> None:
            if self.document() != document or self._recording_key() != recording:
                self._clear(
                    "Object saved; history cleared because the program changed during confirmation or recording changed"
                )
                return
            if motion_recorder.scene_edit_busy:
                self._clear(
                    "Object saved but not recorded: motion capture started during confirmation"
                )
                return
            recording_before = motion_recorder.scene_checkpoint()
            handle._record_snippet(list(target))
            recording_after = motion_recorder.scene_checkpoint()
            program, source_before = document
            self.undo_stack.append(
                WorldEdit(
                    label,
                    before,
                    target,
                    source_before,
                    self.document()[1],
                    program,
                    recording_before,
                    recording_after,
                    renames,
                    draft_before,
                    draft_after,
                )
            )
            self.undo_stack = self.undo_stack[-50:]
            self.redo_stack.clear()
            self.expected = self.context()
            self.feedback = ""

        await self._apply(
            handle,
            target,
            draft_after,
            renames,
            "The controller did not confirm this object edit",
            confirmed,
        )

    async def restore(self, *, redo: bool = False) -> None:
        handle = self.guard(self.context())
        stack, other = (
            (self.redo_stack, self.undo_stack)
            if redo
            else (self.undo_stack, self.redo_stack)
        )
        if not stack:
            return
        edit = stack[-1]

        def confirmed(document: tuple, recording: tuple) -> None:
            if (
                self.document() != document
                or self._recording_key() != recording
                or motion_recorder.scene_edit_busy
            ):
                self._clear(
                    "World restored; source changed during confirmation and was preserved"
                )
                return
            self._set_source(
                edit.program, edit.source_after if redo else edit.source_before
            )
            motion_recorder.restore_scene_checkpoint(
                edit.recording_after if redo else edit.recording_before
            )
            stack.pop()
            other.append(edit)
            self.expected = self.context()
            self.feedback = ""

        await self._apply(
            handle,
            edit.after if redo else edit.before,
            edit.draft_after if redo else edit.draft_before,
            edit.renames if redo else tuple((b, a) for a, b in edit.renames),
            "The controller did not confirm the restored world",
            confirmed,
        )
