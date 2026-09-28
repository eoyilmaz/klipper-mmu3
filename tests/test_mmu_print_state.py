"""Tests for ``MMU_PRINT_START`` / ``MMU_PRINT_END`` and the ``print_state``.

The print state is switched explicitly by the two commands and, as a fallback
for start / end G-code that doesn't call them, by following ``print_stats``.
"""

# Standard Library Imports
import sys
import types

# Third-Party Imports
import pytest

# The real module imports Klipper's ``extras.manual_stepper``; stub it so the
# module under test can be imported standalone.
sys.modules.setdefault(
    "extras.manual_stepper",
    types.SimpleNamespace(ManualStepper=object),
)

# Local Imports
from extras.mmu import MMU, Operation, OperationKind, OperationStats  # noqa: E402
from extras.mmu_hh_compat import MmuStatus  # noqa: E402


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCmd:
    """A ``GCodeCommand`` stand-in reading from a dict of parameters."""

    def __init__(self, **params) -> None:
        self.params = {k: str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def error(self, msg):
        return CommandError(msg)


class FakePrintStats:
    """A ``print_stats`` stand-in with a settable state."""

    def __init__(self, state: str = "standby") -> None:
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


def make_mmu(print_start_detection: bool = True) -> MMU:
    """Build a bare MMU3 with just what the print state code reads."""
    mmu = object.__new__(MMU)
    mmu.print_stats = FakePrintStats()
    mmu._print_stats_state = "standby"
    mmu.print_state = "ready"
    mmu.print_start_detection = print_start_detection
    mmu.is_paused = False
    mmu.job_stats = OperationStats()
    return mmu


def record_toolchange(mmu: MMU) -> None:
    """Add a tool change to the job stats."""
    mmu.job_stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=0, to_tool=1), success=True
    )


def set_print_stats(mmu: MMU, state: str) -> str:
    """Switch ``print_stats`` to ``state`` and return the reported print state."""
    mmu.print_stats.state = state
    return MmuStatus(mmu).print_state(0.0)


# ---------------------------------------------------------------------------
# MMU_PRINT_START
# ---------------------------------------------------------------------------
def test_print_start_switches_to_printing_and_resets_job_stats() -> None:
    mmu = make_mmu()
    record_toolchange(mmu)
    assert mmu.cmd_mmu_print_start(FakeGCmd()) is True
    assert mmu.print_state == "printing"
    assert mmu.is_in_print is True
    assert mmu.job_stats.attempts == {}


def test_print_start_during_a_print_does_nothing() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_start(FakeGCmd())
    record_toolchange(mmu)
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.print_state == "printing"
    assert mmu.job_stats.attempts == {OperationKind.TOOL_CHANGE: 1}


def test_print_start_after_a_print_starts_a_new_job() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_start(FakeGCmd())
    record_toolchange(mmu)
    mmu.cmd_mmu_print_end(FakeGCmd())
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.print_state == "printing"
    assert mmu.job_stats.attempts == {}


def test_print_start_while_print_stats_is_printing_resets_job_stats_once() -> None:
    # MMU_PRINT_START runs from the start G-code, print_stats is printing by
    # then, the detection must not reset the stats of the first tool change
    mmu = make_mmu()
    mmu.print_stats.state = "printing"
    mmu.cmd_mmu_print_start(FakeGCmd())
    record_toolchange(mmu)
    assert MmuStatus(mmu).print_state(0.0) == "printing"
    assert mmu.job_stats.attempts == {OperationKind.TOOL_CHANGE: 1}


# ---------------------------------------------------------------------------
# MMU_PRINT_END
# ---------------------------------------------------------------------------
def test_print_end_defaults_to_complete() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.cmd_mmu_print_end(FakeGCmd()) is True
    assert mmu.print_state == "complete"
    assert mmu.is_in_print is False


@pytest.mark.parametrize(
    "state", ["complete", "cancelled", "error", "ready", "standby", "CANCELLED"]
)
def test_print_end_sets_the_given_state(state) -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_start(FakeGCmd())
    mmu.cmd_mmu_print_end(FakeGCmd(STATE=state))
    assert mmu.print_state == state.lower()


def test_print_end_rejects_an_unknown_state() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_start(FakeGCmd())
    with pytest.raises(CommandError, match="Unknown STATE 'printing'"):
        mmu.cmd_mmu_print_end(FakeGCmd(STATE="printing"))
    assert mmu.print_state == "printing"


def test_print_end_outside_a_print_does_nothing() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_print_end(FakeGCmd(STATE="cancelled"))
    assert mmu.print_state == "ready"


def test_print_end_keeps_its_state_when_print_stats_ends() -> None:
    mmu = make_mmu()
    assert set_print_stats(mmu, "printing") == "printing"
    mmu.cmd_mmu_print_end(FakeGCmd(STATE="cancelled"))
    assert set_print_stats(mmu, "complete") == "cancelled"


def test_print_end_ends_a_paused_print() -> None:
    mmu = make_mmu()
    set_print_stats(mmu, "printing")
    set_print_stats(mmu, "paused")
    mmu.cmd_mmu_print_end(FakeGCmd(STATE="cancelled"))
    assert mmu.print_state == "cancelled"


# ---------------------------------------------------------------------------
# print_stats detection (the fallback without the commands)
# ---------------------------------------------------------------------------
def test_print_stats_drives_a_full_print() -> None:
    mmu = make_mmu()
    record_toolchange(mmu)
    assert set_print_stats(mmu, "standby") == "ready"
    assert set_print_stats(mmu, "printing") == "printing"
    assert mmu.job_stats.attempts == {}
    record_toolchange(mmu)
    assert set_print_stats(mmu, "paused") == "paused"
    assert set_print_stats(mmu, "printing") == "printing"
    # resuming is not a new print
    assert mmu.job_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    assert set_print_stats(mmu, "complete") == "complete"
    assert set_print_stats(mmu, "standby") == "ready"


@pytest.mark.parametrize("end_state", ["cancelled", "error"])
def test_print_stats_end_states(end_state) -> None:
    mmu = make_mmu()
    set_print_stats(mmu, "printing")
    assert set_print_stats(mmu, end_state) == end_state


def test_print_stats_cancel_while_paused() -> None:
    mmu = make_mmu()
    set_print_stats(mmu, "printing")
    set_print_stats(mmu, "paused")
    assert set_print_stats(mmu, "cancelled") == "cancelled"


def test_the_poll_timer_follows_print_stats() -> None:
    mmu = make_mmu()
    mmu.print_stats.state = "printing"
    next_time = mmu._poll_print_stats(100.0)
    assert next_time > 100.0
    assert mmu.print_state == "printing"


def test_without_print_stats_the_state_is_ready() -> None:
    mmu = make_mmu()
    mmu.print_stats = None
    mmu.follow_print_stats(0.0)
    assert MmuStatus(mmu).print_state(0.0) == "ready"


def test_pause_locked_overrides_the_print_state() -> None:
    mmu = make_mmu()
    set_print_stats(mmu, "printing")
    mmu.is_paused = True
    assert MmuStatus(mmu).print_state(0.0) == "pause_locked"
    mmu.is_paused = False
    assert MmuStatus(mmu).print_state(0.0) == "printing"


# ---------------------------------------------------------------------------
# print_start_detection: 0
# ---------------------------------------------------------------------------
def test_without_detection_print_stats_does_not_start_or_end_a_print() -> None:
    mmu = make_mmu(print_start_detection=False)
    record_toolchange(mmu)
    assert set_print_stats(mmu, "printing") == "ready"
    assert mmu.job_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    # a pause outside a print job is not followed either
    assert set_print_stats(mmu, "paused") == "ready"
    assert set_print_stats(mmu, "cancelled") == "ready"


def test_without_detection_the_commands_drive_the_print() -> None:
    mmu = make_mmu(print_start_detection=False)
    mmu.print_stats.state = "printing"
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert MmuStatus(mmu).print_state(0.0) == "printing"
    # pause / resume are still followed
    assert set_print_stats(mmu, "paused") == "paused"
    assert set_print_stats(mmu, "printing") == "printing"
    # print_stats ending does not end the job, MMU_PRINT_END does
    assert set_print_stats(mmu, "complete") == "printing"
    mmu.cmd_mmu_print_end(FakeGCmd())
    assert mmu.print_state == "complete"
    assert set_print_stats(mmu, "standby") == "ready"
