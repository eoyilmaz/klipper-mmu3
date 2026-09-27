"""Tests for reading the gate from ``GATE=``, ``TOOL=`` and ``VALUE=``."""

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
from extras.mmu3 import MMU3, FilamentPos, get_gate_param  # noqa: E402
from extras.mmu3_gate_map import GateMap  # noqa: E402


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCmd:
    """A ``GCodeCommand`` stand-in reading from a dict of parameters."""

    def __init__(self, **params) -> None:
        self.params = {k: str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name)
        if value is None:
            return default
        try:
            value = int(value)
        except ValueError:
            raise self.error(f"Unable to parse '{value}' as a int") from None
        if (minval is not None and value < minval) or (
            maxval is not None and value > maxval
        ):
            raise self.error(f"{name} out of range")
        return value

    def error(self, msg):
        return CommandError(msg)


class FakePrinter:
    command_error = CommandError


def make_mmu(num_tools: int = 5) -> MMU3:
    """Build a bare MMU3 whose moves only record what was done."""
    mmu = object.__new__(MMU3)
    mmu.printer = FakePrinter()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.current_tool = None
    mmu.current_filament = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.messages = []
    mmu.calls = []

    def record(name):
        def f(gate):
            mmu.calls.append((name, gate))
            return True

        return f

    mmu.select_tool = record("select")
    mmu.load_tool = record("load")
    mmu.pre_load_filament_to_finda = record("preload")
    mmu.cmd_tx = lambda gcmd, tool_id: record("tx")(tool_id)
    mmu.assess_filament_pos = lambda: None
    mmu.sync_active_spool = lambda: None
    mmu.save_total_stats = lambda: None
    mmu.disable_steppers = lambda: True
    mmu.show_recovery_prompt = lambda: None
    mmu.pause = lambda: True
    mmu.respond_info = mmu.messages.append
    mmu.respond_debug = lambda msg: None
    mmu.display_status_msg = mmu.messages.append
    return mmu


# ---------------------------------------------------------------------------
# get_gate_param()
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, None),
        ({"GATE": 2}, 2),
        ({"TOOL": 3}, 3),
        ({"VALUE": 4}, 4),
        ({"GATE": 1, "TOOL": 1}, 1),
        ({"GATE": 1, "VALUE": 4}, 1),
        ({"TOOL": 3, "VALUE": 4}, 3),
        ({"GATE": 2, "TOOL": 2, "VALUE": 4}, 2),
    ],
)
def test_get_gate_param_precedence(params, expected) -> None:
    assert get_gate_param(FakeGCmd(**params)) == expected


def test_get_gate_param_without_gcmd_is_none() -> None:
    assert get_gate_param(None) is None


def test_get_gate_param_rejects_conflicting_gate_and_tool() -> None:
    with pytest.raises(CommandError, match="GATE=1 and TOOL=2 differ"):
        get_gate_param(FakeGCmd(GATE=1, TOOL=2))


@pytest.mark.parametrize("name", ["GATE", "TOOL", "VALUE"])
def test_get_gate_param_minval(name) -> None:
    assert get_gate_param(FakeGCmd(**{name: -1}), minval=-1) == -1
    with pytest.raises(CommandError):
        get_gate_param(FakeGCmd(**{name: -2}), minval=-1)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_select_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_select(FakeGCmd(**{name: 1})) is True
    assert mmu.calls == [("select", 1)]
    assert mmu.messages[-1].startswith("MMU_SELECT 1 took")


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_load_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_load(FakeGCmd(**{name: 1})) is True
    assert mmu.calls == [("load", 1)]
    assert mmu.messages[-1].startswith("MMU_LOAD 1 took")


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_preload_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_preload(FakeGCmd(**{name: 1})) is True
    assert mmu.calls == [("preload", 1)]


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_change_tool_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_change_tool(FakeGCmd(**{name: 1})) is True
    assert mmu.calls == [("tx", 1)]


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_recover_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_recover(FakeGCmd(**{name: 2})) is True
    assert mmu.current_filament == 2
    assert mmu.cmd_mmu_recover(FakeGCmd(**{name: -1})) is True
    assert mmu.current_filament is None


@pytest.mark.parametrize(
    "command",
    [
        "cmd_mmu_select",
        "cmd_mmu_load",
        "cmd_mmu_preload",
        "cmd_mmu_change_tool",
        "cmd_mmu_recover",
    ],
)
def test_commands_reject_conflicting_gate_and_tool(command) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match="differ"):
        getattr(mmu, command)(FakeGCmd(GATE=1, TOOL=2))
    assert mmu.calls == []
