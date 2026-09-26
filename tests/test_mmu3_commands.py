"""Tests for the ``MMU``, ``MMU_HELP`` and ``MMU_STATUS`` commands."""

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
from extras.mmu3 import (  # noqa: E402
    MMU3,
    FilamentPos,
    Operation,
    OperationKind,
)
from extras.mmu3_gate_map import GateMap  # noqa: E402
from extras.mmu3_hh_compat import ACTION_IDLE, ACTION_LOADING  # noqa: E402


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


class FakeGCode:
    """Records ``register_command()`` calls."""

    def __init__(self) -> None:
        self.handlers = {}
        self.help = {}

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        assert cmd not in self.handlers, f"{cmd} registered twice"
        self.handlers[cmd] = func
        if desc is not None:
            self.help[cmd] = desc


def make_mmu(num_tools: int = 5) -> MMU3:
    """Build a bare MMU3 with just what the commands read."""
    mmu = object.__new__(MMU3)
    mmu.gcode = FakeGCode()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.is_enabled = True
    mmu.is_homed = False
    mmu.is_paused = False
    mmu.current_tool = None
    mmu.current_filament = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.action = ACTION_IDLE
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.messages = []
    mmu.steppers_disabled = 0

    def disable_steppers():
        mmu.steppers_disabled += 1
        return True

    mmu.disable_steppers = disable_steppers
    mmu.respond_info = mmu.messages.append
    mmu.display_status_msg = mmu.messages.append
    return mmu


# ---------------------------------------------------------------------------
# MMU ENABLE=0|1
# ---------------------------------------------------------------------------
def test_mmu_enable_0_disables_and_turns_off_the_motors() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu(FakeGCmd(ENABLE=0)) is True
    assert mmu.is_enabled is False
    assert mmu.steppers_disabled == 1
    assert mmu.messages == ["MMU Disabled"]


def test_mmu_enable_1_enables() -> None:
    mmu = make_mmu()
    mmu.is_enabled = False
    assert mmu.cmd_mmu(FakeGCmd(ENABLE=1)) is True
    assert mmu.is_enabled is True
    assert mmu.steppers_disabled == 0
    assert mmu.messages == ["MMU Enabled"]


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(True, "MMU is enabled."), (False, "MMU is disabled.")],
)
def test_mmu_without_enable_reports_the_state(enabled, expected) -> None:
    mmu = make_mmu()
    mmu.is_enabled = enabled
    assert mmu.cmd_mmu(FakeGCmd()) is True
    assert mmu.is_enabled is enabled
    assert mmu.messages == [expected]


@pytest.mark.parametrize("value", [2, -1, "yes"])
def test_mmu_rejects_invalid_enable(value) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError):
        mmu.cmd_mmu(FakeGCmd(ENABLE=value))
    assert mmu.is_enabled is True


@pytest.mark.parametrize(
    ("old_name", "new_name", "enabled"),
    [("MMU_ENABLE", "MMU ENABLE=1", True), ("MMU_DISABLE", "MMU ENABLE=0", False)],
)
def test_old_enable_commands_still_work_with_a_warning(
    old_name, new_name, enabled
) -> None:
    mmu = make_mmu()
    mmu.is_enabled = not enabled
    mmu.register_commands()
    assert mmu.gcode.handlers[old_name](FakeGCmd()) is True
    assert mmu.is_enabled is enabled
    assert mmu.messages[0] == f"{old_name} is deprecated, use {new_name} instead."


# ---------------------------------------------------------------------------
# registration / MMU_HELP
# ---------------------------------------------------------------------------
def test_register_commands_gives_the_commands_a_description() -> None:
    mmu = make_mmu()
    mmu.register_commands()
    for name in ("MMU", "MMU_HELP", "MMU_STATUS", "MMU_HOME", "M702"):
        assert mmu.gcode.help[name]
    # aliases and unsupported commands stay out of Klipper's HELP
    for name in ("MMU_ENABLE", "MMU_DISABLE", "HOME_MMU", "MMU_TTG_MAP"):
        assert name in mmu.gcode.handlers
        assert name not in mmu.gcode.help


def test_mmu_help_lists_the_commands() -> None:
    mmu = make_mmu(num_tools=5)
    assert mmu.cmd_mmu_help(FakeGCmd()) is True
    assert len(mmu.messages) == 1
    lines = mmu.messages[0].splitlines()
    assert lines[0] == "MMU commands:"
    names = [line.split(": ")[0].strip() for line in lines[1:]]
    for name, _, desc in mmu.command_table():
        assert name in names
        assert any(line.endswith(f": {desc}") for line in lines)
    assert "T0 - T4" in names
    assert "K0 - K4" in names


# ---------------------------------------------------------------------------
# MMU_STATUS
# ---------------------------------------------------------------------------
def test_mmu_status_idle() -> None:
    mmu = make_mmu(num_tools=3)
    assert mmu.cmd_mmu_status(FakeGCmd()) is True
    status, gate_map = mmu.messages
    assert status.splitlines() == [
        "MMU status:",
        "Enabled: yes",
        "Homed: no",
        "Paused: no",
        "Selected gate: none",
        "Loaded gate: none",
        "Filament position: UNLOADED",
        f"Action: {ACTION_IDLE}",
        "Pending operation: none",
    ]
    assert gate_map.splitlines()[0] == "Gate map:"
    assert len(gate_map.splitlines()) == 4


def test_mmu_status_loaded_and_paused() -> None:
    mmu = make_mmu(num_tools=3)
    mmu.is_homed = True
    mmu.is_paused = True
    mmu.current_tool = 2
    mmu.current_filament = 1
    mmu.filament_pos = FilamentPos.AT_EXTRUDER
    mmu.action = ACTION_LOADING
    operation = Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=2)
    operation.filament_pos_at_fail = FilamentPos.AT_EXTRUDER
    mmu.pending_operation = operation
    assert mmu.cmd_mmu_status(FakeGCmd()) is True
    lines = mmu.messages[0].splitlines()
    assert "Homed: yes" in lines
    assert "Paused: yes" in lines
    assert "Selected gate: T2" in lines
    assert "Loaded gate: T1" in lines
    assert "Filament position: AT_EXTRUDER" in lines
    assert f"Action: {ACTION_LOADING}" in lines
    assert f"Pending operation: {operation.describe()}" in lines
    assert "<- loaded" in mmu.messages[1].splitlines()[2]
