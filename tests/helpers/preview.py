"""Reading a preview client's plan the way tests count what a program did."""

from __future__ import annotations

from waldoctl import TickBlock


def motion_blocks(client) -> list[TickBlock]:
    """The blocks of *client*'s plan that came from motion commands, in
    program order. A refused move is among them: it is on the record with
    its error, the way the scene shows it."""
    return [b for b in client.plan().blocks if b.move_type is not None]


def block_end_tcp(client, block: TickBlock) -> list[float]:
    """Where the TCP stands on the last row of *block* \\[m, rad\\]."""
    record = client.plan()
    return record.tcp[block.start_row + block.rows - 1].astype(float).tolist()
