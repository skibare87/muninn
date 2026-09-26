"""Bounded shutdown: a step that overruns is named in a warning, never waited on forever.

WHY THIS EXISTS. Shutdown used to be `task.cancel(); await task` for every
background loop, which assumes a cancelled task always finishes. It does not.
anyio's connect_tcp (under httpx) swallows a Task.cancel() that lands in the
instant a new connection is established: the request completes normally, the
CancelledError never reaches the caller, and a `while True` loop goes round
again. A tier upload worker did exactly that -- back to waiting on its queue --
and the lifespan awaited it forever. Nothing errored; shutdown just never ended.

Two defences, because either alone leaves a hole:

- `reraise_if_cancelled()` at the top of every background loop turns a swallowed
  cancel back into a CancelledError. Task.cancelling() stays raised after a
  swallow, so the loop can still see it was asked to stop.
- `cancel_and_wait()` and `bounded()` put a time limit on every shutdown step.
  A future defect of this kind becomes a slow shutdown with a warning naming the
  step, instead of a hang with nothing to say where.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable

log = logging.getLogger("xhc.shutdown")

# How long one shutdown step may take before it is abandoned with a warning.
# Every step normally finishes in milliseconds; this only has to be longer than
# a slow but healthy close, and short enough that an orchestrator's own stop
# timeout is not the thing that ends the process.
STEP_TIMEOUT_S = 10.0


def reraise_if_cancelled() -> None:
    """Raise CancelledError if this task was asked to stop and is still running.

    For the top of a background loop. A cancel that some library swallowed
    leaves Task.cancelling() above zero, and a loop that ignores that runs
    forever under a shutdown that is waiting for it.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def cancel_and_wait(
    tasks: Iterable[asyncio.Task | None], what: str, timeout: float | None = None
) -> bool:
    """Cancel every task and wait for them to finish, for at most `timeout`.

    Returns True if all finished. On overrun, logs a warning naming `what` and
    each task still running, and returns False: those tasks are abandoned, not
    awaited. Their results and exceptions are consumed either way, so nothing
    is reported later as "never retrieved".
    """
    pending = [t for t in tasks if t is not None]
    for t in pending:
        t.cancel()
    if not pending:
        return True
    done, still = await asyncio.wait(pending, timeout=STEP_TIMEOUT_S if timeout is None else timeout)
    for t in done:
        if not t.cancelled():
            t.exception()  # retrieve it; a loop that died earlier was already logged
    if still:
        log.warning(
            "shutdown: %s did not finish within %.0fs of being cancelled; abandoning "
            "%d task(s): %s",
            what,
            STEP_TIMEOUT_S if timeout is None else timeout,
            len(still),
            ", ".join(_describe(t) for t in still),
        )
        return False
    return True


async def bounded(step: Awaitable[object], what: str, timeout: float | None = None) -> bool:
    """Run one shutdown step for at most `timeout`. True if it completed.

    Deliberately not asyncio.wait_for: on timeout wait_for cancels the step and
    then WAITS for it to finish, which is the unbounded wait this exists to
    remove. Here an overrunning step is cancelled once and abandoned. An
    exception from the step is logged rather than raised, so one failed step
    does not stop the ones after it.
    """
    limit = STEP_TIMEOUT_S if timeout is None else timeout
    task = asyncio.ensure_future(step)
    done, _ = await asyncio.wait({task}, timeout=limit)
    if not done:
        task.cancel()
        log.warning("shutdown: %s did not finish within %.0fs; abandoning it (%s)",
                    what, limit, _describe(task))
        return False
    if task.cancelled():
        log.warning("shutdown: %s was cancelled", what)
        return False
    exc = task.exception()
    if exc is not None:
        log.error("shutdown: %s failed", what, exc_info=exc)
        return False
    return True


def _describe(task: asyncio.Task) -> str:
    """The task's coroutine and where it is suspended, for the warning."""
    coro = task.get_coro()
    name = getattr(coro, "__qualname__", None) or task.get_name()
    frames = task.get_stack(limit=1)
    if frames:
        f = frames[-1]
        return f"{name} at {f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno}"
    return name
