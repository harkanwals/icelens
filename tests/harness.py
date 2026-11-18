"""Driving the app the way a person does: press a key, wait for the workers, look at the screen.

These live outside `test_app.py` because two suites need them: the feature tests, which drive one
path each, and `test_driving.py`, which drives every path there is.
"""

from __future__ import annotations

from textual.widgets import Static
from textual.worker import WorkerState

from icelens.app import Grid, IcelensApp


async def settle(app: IcelensApp, pilot) -> None:
    """Let the worker threads finish and the screen catch up.

    `workers.wait_for_complete()` is no good here: the panes use exclusive workers, so a
    superseded one is cancelled and raises. Idle is the only thing worth waiting for.
    """
    for _ in range(200):
        await pilot.pause()
        if not any(w.state == WorkerState.RUNNING for w in app.workers):
            await pilot.pause()
            if not any(w.state == WorkerState.RUNNING for w in app.workers):
                return
    raise AssertionError("workers never went idle")


def note(app: IcelensApp, pane: str) -> str:
    return str(app.query_one(f"#note-{pane}", Static).content)


def rows(app: IcelensApp, pane: str) -> int:
    return app.query_one(f"#grid-{pane}", Grid).row_count


def rail(app: IcelensApp) -> str:
    return str(app.query_one("#guide", Static).content)


def grid_of(app: IcelensApp, pane: str) -> Grid:
    return app.query_one(f"#grid-{pane}", Grid)


def unbroken(app: IcelensApp, where: str = "") -> None:
    """Assert nothing has blown up out of sight.

    Textual catches an exception raised in a message handler, stores it, and unwinds the app -
    which a test that only looks at widgets will happily pass through. This is the assertion
    that makes "press every key in every state" mean anything.
    """
    failure = getattr(app, "_exception", None)
    assert failure is None, f"{where}: {type(failure).__name__}: {failure}"
