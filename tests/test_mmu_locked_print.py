"""Tests for a print started while the MMU3 is paused and needs unlocking (#10).

``MMU_PRINT_START`` refuses to start the print, and a command failing while
the printer prints with a paused MMU3 pauses the print, instead of being
silently skipped because the MMU3 is already paused.
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
from extras.mmu import (  # noqa: E402
    MMU,
    Operation,
    OperationKind,
    OperationStats,
    SlicerToolMap,
    auto_pause,
)
from extras.mmu_gate_map import GateMap  # noqa: E402


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCmd:
    """A ``GCodeCommand`` stand-in without parameters."""

    def get(self, name, default=None):
        return default

    def error(self, msg):
        return CommandError(msg)


class FakePrintStats:
    """A ``print_stats`` stand-in with a settable state."""

    def __init__(self, state: str = "standby") -> None:
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


class FakeGCode:
    """Records the scripts the MMU3 runs."""

    def __init__(self) -> None:
        self.scripts = []

    def run_script_from_command(self, script: str) -> None:
        self.scripts.append(script.strip())


def make_mmu(print_stats_state: str = "standby") -> MMU:
    """Build a bare MMU3 with what ``auto_pause`` and the print start read."""
    mmu = object.__new__(MMU)
    mmu.printer = types.SimpleNamespace(command_error=CommandError)
    mmu.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
    mmu.gcode = FakeGCode()
    mmu.print_stats = FakePrintStats(print_stats_state)
    mmu._print_stats_state = print_stats_state
    mmu.print_state = "ready"
    mmu.print_start_detection = True
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.job_stats = OperationStats()
    mmu.ttg_map = list(range(5))
    mmu.gate_map = GateMap(5)
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.filament_pos = None
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.messages = []
    mmu.pauses = 0
    mmu.prompts = 0

    def pause():
        mmu.pauses += 1
        mmu.is_paused = True
        return True

    def show_recovery_prompt():
        mmu.prompts += 1

    mmu.pause = pause
    mmu.show_recovery_prompt = show_recovery_prompt
    mmu.respond_info = mmu.messages.append
    mmu.respond_debug = lambda msg: None
    mmu.display_status_msg = mmu.messages.append
    return mmu


def tool_change(to_tool: int = 1) -> Operation:
    """Return a tool change operation."""
    return Operation(OperationKind.TOOL_CHANGE, from_tool=0, to_tool=to_tool)


@auto_pause
def failing_command(mmu: MMU, gcmd: FakeGCmd) -> bool:
    """A command that fails, like ``Tn`` with the MMU3 paused."""
    return False


# ---------------------------------------------------------------------------
# MMU_PRINT_START
# ---------------------------------------------------------------------------
def test_print_start_is_refused_while_the_mmu_is_paused() -> None:
    mmu = make_mmu("printing")
    mmu.is_paused = True
    with pytest.raises(CommandError, match="MMU_UNLOCK"):
        mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.print_state == "ready"


def test_print_start_works_after_unlocking() -> None:
    mmu = make_mmu("printing")
    mmu.is_paused = True
    with pytest.raises(CommandError):
        mmu.cmd_mmu_print_start(FakeGCmd())
    mmu.is_paused = False
    assert mmu.cmd_mmu_print_start(FakeGCmd()) is True
    assert mmu.print_state == "printing"


def test_print_start_with_a_disabled_mmu_is_not_refused() -> None:
    # printing without the MMU, its paused state doesn't matter
    mmu = make_mmu("printing")
    mmu.is_enabled = False
    mmu.is_paused = True
    assert mmu.cmd_mmu_print_start(FakeGCmd()) is True
    assert mmu.print_state == "printing"


# ---------------------------------------------------------------------------
# auto_pause
# ---------------------------------------------------------------------------
def test_a_failure_while_paused_outside_a_print_does_not_pause_again() -> None:
    mmu = make_mmu("standby")
    mmu.is_paused = True
    mmu.current_operation = tool_change()
    assert failing_command(mmu, FakeGCmd()) is False
    assert mmu.pauses == 0
    assert mmu.gcode.scripts == []
    assert mmu.pending_operation is None


def test_a_failure_while_paused_and_printing_pauses_the_print() -> None:
    # the MMU was locked before the print started without MMU_PRINT_START,
    # T1 fails at its first step and must not let the print go on
    mmu = make_mmu("printing")
    mmu.is_paused = True
    operation = tool_change()
    mmu.current_operation = operation
    assert failing_command(mmu, FakeGCmd()) is False
    assert mmu.pauses == 1
    # Klipper's pause_resume is still paused from the pause outside the print
    assert mmu.gcode.scripts == ["CLEAR_PAUSE"]
    assert any("needs to be unlocked" in msg for msg in mmu.messages)
    # RESUME_MMU retries the tool change
    assert mmu.pending_operation is operation
    assert mmu.current_operation is None
    assert mmu.prompts == 1


def test_a_failure_while_the_print_is_paused_does_not_pause_again() -> None:
    mmu = make_mmu("paused")
    mmu.is_paused = True
    assert failing_command(mmu, FakeGCmd()) is False
    assert mmu.pauses == 0
    assert mmu.gcode.scripts == []


def test_a_failure_while_printing_pauses_without_clearing_the_pause() -> None:
    mmu = make_mmu("printing")
    assert failing_command(mmu, FakeGCmd()) is False
    assert mmu.pauses == 1
    assert mmu.gcode.scripts == []


def test_without_print_stats_a_failure_while_paused_does_not_pause_again() -> None:
    mmu = make_mmu()
    mmu.print_stats = None
    mmu.is_paused = True
    assert failing_command(mmu, FakeGCmd()) is False
    assert mmu.pauses == 0
