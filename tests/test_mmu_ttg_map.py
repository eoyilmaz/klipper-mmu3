"""Tests for the tool-to-gate (TTG) map and ``MMU_TTG_MAP``."""

# Standard Library Imports
import ast
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
    TTG_MAP_VARIABLE,
    FilamentPos,
    OperationStats,
    default_ttg_map,
    map_tool_to_gate,
    ttg_map_from_saved,
)
from extras.mmu_gate_map import (  # noqa: E402
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    GateMap,
)
from extras.mmu_hh_compat import ACTION_IDLE, MmuStatus  # noqa: E402


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
    """Records the scripts run and the commands registered."""

    def __init__(self) -> None:
        self.scripts = []
        self.handlers = {}

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        self.handlers[cmd] = func


def make_mmu(num_tools: int = 5) -> MMU:
    """Build a bare MMU3 whose moves only record what was done."""
    mmu = object.__new__(MMU)
    mmu.printer = types.SimpleNamespace(
        command_error=CommandError,
        lookup_object=lambda name, default=None: default,
    )
    mmu.gcode = FakeGCode()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.ttg_map = default_ttg_map(num_tools)
    mmu.selected_tool = None
    mmu.save_variables = object()
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.current_gate = None
    mmu.loaded_gate = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.action = ACTION_IDLE
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.total_stats = OperationStats()
    mmu.job_stats = OperationStats()
    mmu.messages = []
    mmu.calls = []
    mmu.respond_info = mmu.messages.append
    mmu.respond_debug = lambda msg: None
    mmu.display_status_msg = lambda msg: None
    mmu.sync_active_spool = lambda quiet=False: None
    mmu.save_total_stats = lambda: None
    mmu.save_gate_map = lambda: None
    return mmu


def saved_ttg_map(mmu: MMU) -> list[int]:
    """Return the map the last ``SAVE_VARIABLE`` saved, parsed like Klipper."""
    script = mmu.gcode.scripts[-1]
    prefix = f"SAVE_VARIABLE VARIABLE={TTG_MAP_VARIABLE} VALUE="
    assert script.startswith(prefix)
    # save_variables parses VALUE with ast.literal_eval()
    return ast.literal_eval(ast.literal_eval(script[len(prefix) :]))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_default_ttg_map_is_the_identity() -> None:
    assert default_ttg_map(3) == [0, 1, 2]


@pytest.mark.parametrize(
    ("tool", "ttg_map", "expected"),
    [
        (1, [0, 3, 2, 1], 3),
        (1, None, 1),
        (-1, [0, 3, 2, 1], -1),
        (4, [0, 3, 2, 1], 4),
    ],
)
def test_map_tool_to_gate(tool, ttg_map, expected) -> None:
    assert map_tool_to_gate(tool, ttg_map) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([2, 2, 0], [2, 2, 0]),
        ((1, 0, 2), [1, 0, 2]),
        (None, [0, 1, 2]),
        ({}, [0, 1, 2]),
        ([0, 1], [0, 1, 2]),
        ([0, 1, 3], [0, 1, 2]),
        ([0, 1, -1], [0, 1, 2]),
        ([0, 1, "2"], [0, 1, 2]),
        ([0, 1, True], [0, 1, 2]),
    ],
)
def test_ttg_map_from_saved(value, expected) -> None:
    assert ttg_map_from_saved(3, value) == expected


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def test_tool_to_gate_uses_the_map() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [4, 1, 2, 3, 0]
    assert mmu.tool_to_gate(0) == 4
    assert mmu.tool_to_gate(None) is None


def test_gate_to_tool_prefers_the_selected_tool() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [2, 1, 2, 3, 4]
    assert mmu.gate_to_tool(2) == 0
    mmu.selected_tool = 2
    assert mmu.gate_to_tool(2) == 2
    # the selected tool does not map to gate 1
    assert mmu.gate_to_tool(1) == 1


def test_gate_to_tool_without_a_mapped_tool_is_none() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 0, 0, 0, 0]
    assert mmu.gate_to_tool(3) is None
    assert mmu.gate_to_tool(None) is None


def test_loaded_tool_follows_the_map() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 3, 2, 1, 4]
    assert mmu.loaded_tool is None
    mmu.loaded_gate = 3
    assert mmu.loaded_tool == 1


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def test_set_ttg_map_saves_a_changed_map() -> None:
    mmu = make_mmu()
    mmu.set_ttg_map([1, 1, 2, 3, 4])
    assert mmu.ttg_map == [1, 1, 2, 3, 4]
    assert saved_ttg_map(mmu) == [1, 1, 2, 3, 4]


def test_set_ttg_map_does_not_save_an_unchanged_map() -> None:
    mmu = make_mmu()
    mmu.set_ttg_map([0, 1, 2, 3, 4])
    assert mmu.gcode.scripts == []


def test_set_ttg_map_makes_a_new_list() -> None:
    # Klipper only pushes a status change if the object is a new one
    mmu = make_mmu()
    old = mmu.ttg_map
    new = [1, 1, 2, 3, 4]
    mmu.set_ttg_map(new)
    assert mmu.ttg_map is not old
    assert mmu.ttg_map is not new


def test_save_ttg_map_without_save_variables_is_noop() -> None:
    mmu = make_mmu()
    mmu.save_variables = None
    mmu.set_ttg_map([1, 1, 2, 3, 4])
    assert mmu.ttg_map == [1, 1, 2, 3, 4]
    assert mmu.gcode.scripts == []


def test_saved_ttg_map_survives_a_restart() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_ttg_map(FakeGCmd(TOOL=0, GATE=3, QUIET=1))
    saved = saved_ttg_map(mmu)
    # what handle_connect() reads back after a restart
    assert ttg_map_from_saved(5, saved) == [3, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# MMU_TTG_MAP
# ---------------------------------------------------------------------------
def test_mmu_ttg_map_without_arguments_prints_the_map() -> None:
    mmu = make_mmu(num_tools=3)
    mmu.ttg_map = [2, 1, 2]
    mmu.gate_map.update(2, material="PLA", name="Red", status=GATE_AVAILABLE)
    mmu.loaded_gate = 2
    assert mmu.cmd_mmu_ttg_map(FakeGCmd()) is True
    assert mmu.messages[-1].splitlines() == [
        "TTG map:",
        "T0 -> gate 2 PLA Red [available] <- loaded",
        "T1 -> gate 1 - [unknown]",
        "T2 -> gate 2 PLA Red [available]",
    ]
    assert mmu.gcode.scripts == []


def test_mmu_ttg_map_quiet_without_arguments_still_prints() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(QUIET=1)) is True
    assert mmu.messages[-1].startswith("TTG map:")


def test_mmu_ttg_map_maps_a_tool_to_a_gate() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(TOOL=1, GATE=4)) is True
    assert mmu.ttg_map == [0, 4, 2, 3, 4]
    assert saved_ttg_map(mmu) == [0, 4, 2, 3, 4]
    assert "T1 -> gate 4" in mmu.messages[-1]


def test_mmu_ttg_map_quiet_does_not_print() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(TOOL=1, GATE=4, QUIET=1)) is True
    assert mmu.messages == []


def test_mmu_ttg_map_sets_the_whole_map() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(MAP="4,3,2,1,0", QUIET=1)) is True
    assert mmu.ttg_map == [4, 3, 2, 1, 0]
    assert saved_ttg_map(mmu) == [4, 3, 2, 1, 0]


def test_mmu_ttg_map_accepts_a_quoted_map() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(MAP='"0, 0, 0, 0, 0"', QUIET=1)) is True
    assert mmu.ttg_map == [0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("0,1,2", "MAP= has 3 gates, it needs one for each of the 5 tools"),
        ("0,1,2,3,x", "Invalid gate in MAP=: x"),
        ("0,1,2,3,5", "Invalid gate in MAP=: 5"),
        ("0,1,2,3,-1", "Invalid gate in MAP=: -1"),
    ],
)
def test_mmu_ttg_map_rejects_an_invalid_map(value, match) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match=match):
        mmu.cmd_mmu_ttg_map(FakeGCmd(MAP=value))
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert mmu.gcode.scripts == []


def test_mmu_ttg_map_reset_restores_the_identity() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [4, 3, 2, 1, 0]
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(RESET=1, QUIET=1)) is True
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert saved_ttg_map(mmu) == [0, 1, 2, 3, 4]


def test_mmu_ttg_map_tool_needs_a_gate() -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match="TOOL= needs a GATE="):
        mmu.cmd_mmu_ttg_map(FakeGCmd(TOOL=1))


@pytest.mark.parametrize("params", [{"TOOL": 5, "GATE": 0}, {"TOOL": 0, "GATE": 5}])
def test_mmu_ttg_map_rejects_out_of_range(params) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match="out of range"):
        mmu.cmd_mmu_ttg_map(FakeGCmd(**params))


def test_mmu_ttg_map_sets_the_gate_availability() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(GATE=2, AVAILABLE=0, QUIET=1)) is True
    assert mmu.gate_map[2].status == GATE_EMPTY
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert mmu.cmd_mmu_ttg_map(FakeGCmd(TOOL=0, GATE=2, AVAILABLE=1, QUIET=1)) is True
    assert mmu.gate_map[2].status == GATE_AVAILABLE
    assert mmu.ttg_map == [2, 1, 2, 3, 4]
    assert mmu.gate_map[3].status == GATE_UNKNOWN


def test_mmu_remap_ttg_is_the_same_command() -> None:
    mmu = make_mmu()
    mmu.register_commands()
    assert mmu.gcode.handlers["MMU_REMAP_TTG"] == mmu.cmd_mmu_ttg_map
    assert mmu.gcode.handlers["MMU_TTG_MAP"] == mmu.cmd_mmu_ttg_map
    # what Mainsail's TTG map dialog sends
    mmu.gcode.handlers["MMU_REMAP_TTG"](FakeGCmd(TOOL=3, GATE=0, QUIET=1))
    assert mmu.ttg_map == [0, 1, 2, 0, 4]


# ---------------------------------------------------------------------------
# Tn / Kn
# ---------------------------------------------------------------------------
def make_tool_change_mmu() -> MMU:
    """Build an MMU3 whose load / unload / cut only record the gate."""
    mmu = make_mmu()
    mmu.tool_change_retry = 3
    mmu.filament_switch_sensor = None
    mmu.filament_motion_sensor = None
    mmu.reactor = None
    mmu.toolhead = None
    mmu.assess_filament_pos = lambda: None
    mmu.disable_steppers = lambda: True
    mmu.home_idler = lambda: True
    mmu.home_mmu = lambda: True

    def unload_gate():
        mmu.calls.append(("unload", mmu.loaded_gate))
        mmu.loaded_gate = None
        mmu.filament_pos = FilamentPos.UNLOADED
        return True

    def load_gate(gate):
        mmu.calls.append(("load", gate))
        mmu.loaded_gate = gate
        mmu.filament_pos = FilamentPos.LOADED
        return True

    def cut_filament_in_mmu(gate):
        mmu.calls.append(("cut", gate))
        return True

    mmu.unload_gate = unload_gate
    mmu.load_gate = load_gate
    mmu.cut_filament_in_mmu = cut_filament_in_mmu
    return mmu


def test_tn_loads_the_mapped_gate() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [0, 1, 4, 3, 2]
    assert mmu.cmd_tx(FakeGCmd(), tool_id=2) is True
    assert mmu.calls == [("unload", None), ("load", 4)]
    assert mmu.loaded_gate == 4
    assert mmu.loaded_tool == 2


def test_tn_names_the_tool_when_tools_share_a_gate() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [0, 1, 1, 3, 4]
    assert mmu.cmd_tx(FakeGCmd(), tool_id=2) is True
    assert mmu.loaded_gate == 1
    assert mmu.loaded_tool == 2
    assert MmuStatus(mmu).tool() == 2


def test_tn_with_the_mapped_gate_loaded_does_nothing() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [0, 1, 4, 3, 2]
    mmu.loaded_gate = 4
    mmu.filament_pos = FilamentPos.LOADED
    assert mmu.cmd_tx(FakeGCmd(), tool_id=2) is True
    assert mmu.calls == []


def test_tn_records_the_tool_change_by_tool() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [3, 1, 2, 0, 4]
    mmu.loaded_gate = 3
    mmu.filament_pos = FilamentPos.LOADED
    assert mmu.cmd_tx(FakeGCmd(), tool_id=3) is True
    assert mmu.calls == [("unload", 3), ("load", 0)]
    assert mmu.total_stats.toolchanges == {(0, 3): 1}


def test_failed_tn_retries_the_mapped_gate() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [0, 1, 4, 3, 2]
    mmu.pause = lambda: True
    mmu.show_recovery_prompt = lambda: None
    mmu.load_gate = lambda gate: mmu.calls.append(("load", gate)) and False
    assert mmu.cmd_tx(FakeGCmd(), tool_id=2) is False
    op = mmu.pending_operation
    assert (op.to_tool, op.to_gate) == (2, 4)
    assert op.describe().startswith("Load T2")

    loaded = []

    def load_gate(gate):
        loaded.append(gate)
        mmu.loaded_gate = gate
        mmu.filament_pos = FilamentPos.LOADED
        return True

    mmu.load_gate = load_gate
    assert mmu.retry_pending_operation() is True
    assert loaded == [4]


def test_kn_cuts_the_mapped_gate() -> None:
    mmu = make_tool_change_mmu()
    mmu.ttg_map = [0, 1, 4, 3, 2]
    assert mmu.cmd_kx(FakeGCmd(), tool_id=2) is True
    assert mmu.calls == [("cut", 4)]
