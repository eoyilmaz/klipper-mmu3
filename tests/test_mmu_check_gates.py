"""Tests for ``MMU_CHECK_GATE`` / ``MMU_CHECK_GATES``."""

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
from extras.mmu import MMU, FilamentPos, SlicerToolMap  # noqa: E402
from extras.mmu_gate_map import (  # noqa: E402
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    GateMap,
)
from extras.mmu_hh_compat import ACTION_CHECKING, ACTION_IDLE  # noqa: E402


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

    def lookup_object(self, name, default=None):
        # no user macros are defined
        return default


def make_mmu(num_tools: int = 5, empty_gates=()) -> MMU:
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
    mmu.is_homed = True
    mmu.is_paused = False
    mmu.current_gate = None
    mmu.loaded_gate = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.action = ACTION_IDLE
    mmu.enable_no_selector_mode = False
    mmu.messages = []
    mmu.calls = []
    mmu.paused = False
    mmu.empty_gates = set(empty_gates)

    def select_gate(gate):
        mmu.calls.append(("select", gate))
        assert mmu.action == ACTION_CHECKING
        mmu.current_gate = gate
        return True

    def load_filament_to_finda_in_loop():
        mmu.calls.append(("load", mmu.current_gate))
        return mmu.current_gate not in mmu.empty_gates

    def unload_filament_from_finda():
        mmu.calls.append(("unload", mmu.current_gate))
        mmu.loaded_gate = None
        mmu.filament_pos = FilamentPos.UNLOADED
        return True

    def pause():
        mmu.paused = True
        mmu.is_paused = True
        return True

    class FakeStepper:
        def do_set_position(self, pos):
            pass

    mmu.select_gate = select_gate
    mmu.load_filament_to_finda_in_loop = load_filament_to_finda_in_loop
    mmu.unload_filament_from_finda = unload_filament_from_finda
    mmu.pulley_stepper = FakeStepper()
    mmu.pause = pause
    mmu.disable_steppers = lambda: True
    mmu.enable_steppers = lambda: None
    mmu.show_recovery_prompt = lambda: None
    mmu.respond_info = mmu.messages.append
    mmu.respond_debug = lambda msg: None
    mmu.display_status_msg = mmu.messages.append
    return mmu


# ---------------------------------------------------------------------------
# parameter parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("params", "check_all", "expected"),
    [
        ({}, True, [0, 1, 2, 3, 4]),
        ({"ALL": 1}, False, [0, 1, 2, 3, 4]),
        ({"GATE": 2}, False, [2]),
        ({"GATE": 2}, True, [2]),
        ({"TOOL": 3}, False, [3]),
        ({"GATES": "0,3"}, True, [0, 3]),
        ({"GATES": "3, 0,"}, True, [3, 0]),
        ({"GATES": "1,1,2"}, True, [1, 2]),
        ({"TOOLS": "4,1"}, False, [4, 1]),
        ({"GATES": "0", "GATE": 2}, False, [0]),
        ({"GATE": 1, "TOOL": 2}, False, [1]),
    ],
)
def test_get_check_gates_param(params, check_all, expected) -> None:
    mmu = make_mmu()
    assert mmu.get_check_gates_param(FakeGCmd(**params), check_all) == expected


def test_check_gate_without_gate_uses_selected_gate() -> None:
    mmu = make_mmu()
    mmu.current_gate = 3
    assert mmu.get_check_gates_param(FakeGCmd(), check_all=False) == [3]


def test_check_gate_without_gate_and_selection_is_none() -> None:
    mmu = make_mmu()
    assert mmu.get_check_gates_param(FakeGCmd(), check_all=False) is None


@pytest.mark.parametrize(
    "params",
    [{"GATE": 5}, {"GATE": -1}, {"TOOL": 9}, {"GATES": "0,5"}, {"GATES": "0,a"}],
)
def test_get_check_gates_param_rejects_invalid_gates(params) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError):
        mmu.get_check_gates_param(FakeGCmd(**params), check_all=True)


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------
def test_check_gates_records_status_and_continues_past_empty_gate() -> None:
    mmu = make_mmu(num_tools=3, empty_gates={1})
    assert mmu.cmd_mmu_check_gates(FakeGCmd()) is True
    assert [g.status for g in mmu.gate_map.gates] == [
        GATE_AVAILABLE,
        GATE_EMPTY,
        GATE_AVAILABLE,
    ]
    assert mmu.calls == [
        ("select", 0),
        ("load", 0),
        ("unload", 0),
        ("select", 1),
        ("load", 1),
        ("select", 2),
        ("load", 2),
        ("unload", 2),
    ]
    assert not mmu.paused
    assert mmu.pending_operation is None
    assert mmu.filament_pos == FilamentPos.UNLOADED
    assert mmu.action == ACTION_IDLE
    assert mmu.messages[-1] == (
        "Gate check: Gate 0: available, Gate 1: empty, Gate 2: available"
    )


def test_check_gates_quiet_prints_no_summary() -> None:
    mmu = make_mmu(num_tools=2)
    assert mmu.cmd_mmu_check_gates(FakeGCmd(QUIET=1)) is True
    assert not any(m.startswith("Gate check") for m in mmu.messages)


def test_check_gate_checks_only_the_given_gates() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_check_gates(FakeGCmd(GATES="0,3")) is True
    assert [c for c in mmu.calls if c[0] == "select"] == [("select", 0), ("select", 3)]
    assert mmu.gate_map[1].status == GATE_UNKNOWN


def test_check_gate_reselects_the_previous_gate() -> None:
    mmu = make_mmu()
    mmu.current_gate = 1
    assert mmu.cmd_mmu_check_gate(FakeGCmd(GATE=4)) is True
    assert mmu.calls[-1] == ("select", 1)
    assert mmu.current_gate == 1


def test_check_gate_holds_the_idler_on_the_selected_gate() -> None:
    # the steppers were disabled after the previous command, the idler is
    # already on the gate so its move doesn't re-enable it
    mmu = make_mmu()
    mmu.current_gate = 2
    mmu.enable_steppers = lambda: mmu.calls.append(("steppers_on",))
    mmu.disable_steppers = lambda: mmu.calls.append(("steppers_off",))
    assert mmu.cmd_mmu_check_gate(FakeGCmd()) is True
    assert mmu.calls == [
        ("steppers_on",),
        ("select", 2),
        ("load", 2),
        ("unload", 2),
        ("steppers_off",),
    ]


def test_enable_steppers_enables_the_idler_and_selector() -> None:
    class FakeStepper:
        def __init__(self) -> None:
            self.enabled = None

        def do_enable(self, enable):
            self.enabled = enable

    mmu = object.__new__(MMU)
    mmu.pulley_stepper = FakeStepper()
    mmu.idler_stepper = FakeStepper()
    mmu.selector_stepper = FakeStepper()
    mmu.enable_steppers()
    assert mmu.idler_stepper.enabled is True
    assert mmu.selector_stepper.enabled is True
    # the pulley is enabled by its first move
    assert mmu.pulley_stepper.enabled is None


def test_check_gate_without_selection_is_refused() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_check_gate(FakeGCmd()) is False
    assert mmu.calls == []
    assert not mmu.paused


@pytest.mark.parametrize(
    "pos", [FilamentPos.AT_FINDA, FilamentPos.IN_HOTEND, FilamentPos.LOADED]
)
def test_check_gates_refused_while_filament_is_loaded(pos) -> None:
    mmu = make_mmu()
    mmu.loaded_gate = 2
    mmu.filament_pos = pos
    assert mmu.cmd_mmu_check_gates(FakeGCmd()) is False
    assert mmu.calls == []
    assert not mmu.paused
    assert "unload it before checking gates" in mmu.messages[-1]


def test_check_gates_refused_in_no_selector_mode() -> None:
    mmu = make_mmu()
    mmu.enable_no_selector_mode = True
    assert mmu.cmd_mmu_check_gates(FakeGCmd()) is False
    assert mmu.calls == []
    assert not mmu.paused


def test_check_gates_pauses_when_filament_is_stuck_in_finda() -> None:
    mmu = make_mmu(num_tools=3)
    mmu.unload_filament_from_finda = lambda: False
    assert mmu.cmd_mmu_check_gates(FakeGCmd()) is False
    assert mmu.paused
    # the summary still reports what was found before the failure
    assert mmu.messages[-1] == "Gate check: Gate 0: available"
