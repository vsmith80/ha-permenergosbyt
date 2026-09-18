"""Tracks fire-and-forget asyncio tasks so they can all be cancelled
together - e.g. a scheduled campaign start, a retry-timer continuation and
the restart-resume task in scheduler.py, any of which can overlap another
(a schedule change mid-retry, a restart during a pending retry) and any of
which must not be left running past an integration unload.

Deliberately pure asyncio, no Home Assistant imports, so it's cheaply
unit-testable without stubbing the framework - see
tests/test_background_tasks.py.
"""

from __future__ import annotations

import asyncio


class BackgroundTasks:
    """A set of tracked asyncio.Task objects, self-pruning on completion."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def track(self, task: asyncio.Task) -> asyncio.Task:
        """Start tracking a task; it removes itself once it finishes."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_all(self) -> None:
        """Cancel every task still running and stop tracking all of them."""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
