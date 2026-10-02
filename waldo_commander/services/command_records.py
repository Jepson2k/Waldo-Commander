"""Optional command value capture around the existing execution adapter."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable
from uuid import uuid4

from waldoctl.record_values import snapshot_arguments, snapshot_value
from waldoctl.skills import current_skill_invocation


def recorded_method(io: Any, name: str, method: Callable) -> Callable:
    if not io.capture_values:
        return method

    def begin(args, kwargs):
        invocation = uuid4().hex
        io.emit_event(
            "command_started",
            name,
            command_id=invocation,
            parent_id=current_skill_invocation(),
            arguments=snapshot_arguments(method, args, kwargs),
        )
        return invocation

    def returned(invocation, result):
        io.emit_event(
            "command_returned",
            name,
            command_id=invocation,
            result=snapshot_value(result),
        )

    def failed(invocation, error):
        io.emit_event(
            "command_failed",
            name,
            command_id=invocation,
            error_type=type(error).__name__,
            message=str(error)[:512],
            code=snapshot_value(getattr(error, "code", None)),
        )

    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def async_call(*args, **kwargs):
            invocation = begin(args, kwargs)
            try:
                result = await method(*args, **kwargs)
            except BaseException as error:
                failed(invocation, error)
                raise
            returned(invocation, result)
            return result

        return async_call

    @wraps(method)
    def call(*args, **kwargs):
        invocation = begin(args, kwargs)
        try:
            result = method(*args, **kwargs)
        except BaseException as error:
            failed(invocation, error)
            raise
        returned(invocation, result)
        return result

    return call
