"""Unit tests for background_tasks.py.

Pure asyncio, no Home Assistant involved - this covers exactly the
concurrency mechanism that scheduler.py relies on to cancel any
in-flight campaign-attempt task (scheduled tick, retry-timer
continuation, restart-resume) on integration unload, including when two
of them overlap (e.g. a schedule change firing a fresh campaign while an
old retry from the previous one is still in flight).
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import load_component_module

background_tasks = load_component_module("background_tasks", "background_tasks.py")


async def _hang_forever(event: asyncio.Event) -> None:
    """Simulates an in-flight network call that never resolves on its own -
    only cancellation ends it."""
    await event.wait()


@pytest.mark.asyncio
async def test_cancel_all_cancels_a_tracked_pending_task():
    tracker = background_tasks.BackgroundTasks()
    never = asyncio.Event()
    task = tracker.track(asyncio.create_task(_hang_forever(never)))

    tracker.cancel_all()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_all_cancels_two_overlapping_tasks():
    # E.g. a retry-timer continuation still in flight when a schedule
    # change fires a fresh campaign start - both must be tracked and both
    # must be cancelled, not just the most recently started one.
    tracker = background_tasks.BackgroundTasks()
    never = asyncio.Event()
    retry_task = tracker.track(asyncio.create_task(_hang_forever(never)))
    fresh_start_task = tracker.track(asyncio.create_task(_hang_forever(never)))

    tracker.cancel_all()

    with pytest.raises(asyncio.CancelledError):
        await retry_task
    with pytest.raises(asyncio.CancelledError):
        await fresh_start_task


@pytest.mark.asyncio
async def test_completed_task_is_pruned_and_not_touched_again():
    tracker = background_tasks.BackgroundTasks()

    async def finishes_immediately() -> str:
        return "done"

    task = tracker.track(asyncio.create_task(finishes_immediately()))
    assert await task == "done"
    # Let the done-callback (scheduled via call_soon) actually run.
    await asyncio.sleep(0)

    assert task not in tracker._tasks
    # A no-op, not an error - the finished task is no longer tracked.
    tracker.cancel_all()


@pytest.mark.asyncio
async def test_cancel_all_on_empty_tracker_does_not_raise():
    background_tasks.BackgroundTasks().cancel_all()
