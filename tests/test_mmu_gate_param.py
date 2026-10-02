"""Tests for reading the tool and gate from ``TOOL=`` and ``GATE=``."""

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
    SlicerToolMap,
    MMU,
    FilamentPos,
    get_gate_param,
    get_tool_and_gate_params,
)
from extras.mmu_gate_map import GateMap  # noqa: E402


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


def make_mmu(num_tools: int = 5) -> MMU:
    """Build a bare MMU3 whose moves only record what was done."""
    mmu = object.__new__(MMU)
    mmu.printer = FakePrinter()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.ttg_map = list(range(num_tools))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(num_tools))
    mmu.selected_tool = None
    mmu.save_variables = None
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.current_gate = None
    mmu.loaded_gate = None
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

    mmu.select_gate = record("select")
    mmu.load_gate = record("load")
    mmu.pre_load_filament_to_finda = record("preload")
    mmu.cmd_tx = lambda gcmd, tool_id, gate=None: record("tx")((tool_id, gate))
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
        ({"GATE": 1, "TOOL": 1}, 1),
        # the old MMU3 VALUE= is not read anymore
        ({"VALUE": 4}, None),
        # GATE= wins over TOOL=, they can differ when the tool is remapped
        ({"GATE": 1, "TOOL": 2}, 1),
    ],
)
def test_get_gate_param_precedence(params, expected) -> None:
    assert get_gate_param(FakeGCmd(**params)) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"TOOL": 0}, (0, 3)),
        ({"TOOL": 3}, (3, 1)),
        # GATE= bypasses the map
        ({"GATE": 0}, (None, 0)),
        ({"TOOL": 0, "GATE": 2}, (0, 2)),
        # not in the map, left for the caller to reject
        ({"TOOL": -1}, (-1, -1)),
        ({"TOOL": 7}, (7, 7)),
    ],
)
def test_get_tool_and_gate_params_resolves_tools_through_the_map(
    params, expected
) -> None:
    ttg_map = [3, 1, 2, 1, 4]
    assert get_tool_and_gate_params(FakeGCmd(**params), ttg_map) == expected


def test_get_gate_param_resolves_tool_through_the_map() -> None:
    assert get_gate_param(FakeGCmd(TOOL=0), ttg_map=[4, 1, 2, 3, 0]) == 4


def test_get_gate_param_without_gcmd_is_none() -> None:
    assert get_gate_param(None) is None


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
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
    assert mmu.calls == [("tx", (1, 1))]


@pytest.mark.parametrize("name", ["GATE", "TOOL"])
def test_mmu_recover_accepts_gate_and_tool(name) -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_recover(FakeGCmd(**{name: 2})) is True
    assert mmu.loaded_gate == 2
    assert mmu.cmd_mmu_recover(FakeGCmd(**{name: -1})) is True
    assert mmu.loaded_gate is None


# ---------------------------------------------------------------------------
# tool-to-gate map
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "call"),
    [
        ("cmd_mmu_select", "select"),
        ("cmd_mmu_load", "load"),
        ("cmd_mmu_preload", "preload"),
    ],
)
def test_commands_resolve_tool_through_the_map(command, call) -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert getattr(mmu, command)(FakeGCmd(TOOL=1)) is True
    assert mmu.calls == [(call, 3)]


@pytest.mark.parametrize(
    ("command", "call"),
    [
        ("cmd_mmu_select", "select"),
        ("cmd_mmu_load", "load"),
        ("cmd_mmu_preload", "preload"),
    ],
)
def test_commands_gate_bypasses_the_map(command, call) -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert getattr(mmu, command)(FakeGCmd(GATE=1, TOOL=2)) is True
    assert mmu.calls == [(call, 1)]


def test_mmu_select_with_tool_selects_the_tool() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 3, 4]
    assert mmu.cmd_mmu_select(FakeGCmd(TOOL=3)) is True
    mmu.loaded_gate = 3
    # T1 maps to gate 3 too, the selected T3 names it
    assert mmu.loaded_tool == 3


def test_mmu_change_tool_resolves_tool_through_the_map() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert mmu.cmd_mmu_change_tool(FakeGCmd(TOOL=1)) is True
    assert mmu.calls == [("tx", (1, 3))]


def test_mmu_change_tool_gate_loads_as_the_mapped_tool() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert mmu.cmd_mmu_change_tool(FakeGCmd(GATE=3)) is True
    assert mmu.calls == [("tx", (1, 3))]


def test_mmu_change_tool_refuses_a_gate_no_tool_maps_to() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 0, 0, 0, 0]
    assert mmu.cmd_mmu_change_tool(FakeGCmd(GATE=3)) is False
    assert mmu.calls == []
    assert "No tool maps to gate 3" in mmu.messages[-1]


def test_mmu_recover_tool_alone_is_its_mapped_gate() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert mmu.cmd_mmu_recover(FakeGCmd(TOOL=1, LOADED=1)) is True
    assert mmu.loaded_gate == 3
    assert mmu.loaded_tool == 1
    assert mmu.filament_pos == FilamentPos.LOADED
    assert mmu.ttg_map == [0, 3, 2, 1, 4]


def test_mmu_recover_tool_and_gate_remaps_the_tool() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_recover(FakeGCmd(TOOL=2, GATE=4, LOADED=1)) is True
    assert mmu.ttg_map == [0, 1, 4, 3, 4]
    assert mmu.loaded_gate == 4
    assert mmu.loaded_tool == 2


def test_mmu_recover_unknown_tool_keeps_the_map() -> None:
    # Mainsail's recover dialog sends TOOL=-1 when the tool is unknown
    mmu = make_mmu()
    assert mmu.cmd_mmu_recover(FakeGCmd(TOOL=-1, GATE=2)) is True
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert mmu.loaded_gate == 2
    assert mmu.loaded_tool == 2


def test_mmu_recover_rejects_invalid_tool() -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match="Invalid tool: 7"):
        mmu.cmd_mmu_recover(FakeGCmd(TOOL=7, GATE=1))
