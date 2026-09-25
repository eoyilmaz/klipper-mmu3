"""Tests for the Happy Hare compatible ``mmu`` / ``mmu_machine`` status objects.

These are what Mainsail's and Fluidd's MMU panels read, so the field names,
shapes and constants must match what the panels expect.
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
from extras.mmu3 import (  # noqa: E402
    MMU3,
    FilamentPos,
    FilamentSwitchSensorPosition,
    Operation,
    OperationKind,
    OperationStats,
)
from extras.mmu3_gate_map import GateMap  # noqa: E402
from extras.mmu3_hh_compat import (  # noqa: E402
    ACTION_CUTTING_FILAMENT,
    ACTION_FORMING_TIP,
    ACTION_IDLE,
    ACTION_LOADING,
    ACTION_LOADING_EXTRUDER,
    ACTION_UNLOADING,
    ACTION_UNLOADING_EXTRUDER,
    DIRECTION_LOAD,
    DIRECTION_UNKNOWN,
    DIRECTION_UNLOAD,
    FILAMENT_POS_EXTRUDER_ENTRY,
    FILAMENT_POS_HOMED_ENTRY,
    FILAMENT_POS_HOMED_GATE,
    FILAMENT_POS_IN_EXTRUDER,
    FILAMENT_POS_LOADED,
    FILAMENT_POS_UNLOADED,
    MmuMachine,
    MmuStatus,
)


class FakePrintStats:
    """A ``print_stats`` stand-in reporting a fixed state."""

    def __init__(self, state: str) -> None:
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


class FakeSwitchSensor:
    """A ``filament_switch_sensor`` stand-in."""

    def __init__(self, detected: bool) -> None:
        self.detected = detected

    def get_status(self, eventtime):
        return {"filament_detected": self.detected}


def make_mmu(num_tools: int = 5) -> MMU3:
    """Build a bare MMU3 instance with just what the status objects read."""
    mmu = object.__new__(MMU3)
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.is_enabled = True
    mmu.is_homed = True
    mmu.is_paused = False
    mmu.print_stats = FakePrintStats("standby")
    mmu.current_tool = None
    mmu.current_filament = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.job_stats = OperationStats()
    mmu.action = ACTION_IDLE
    mmu.spoolman_support = "readonly"
    mmu.filament_switch_sensor = None
    mmu.filament_switch_sensor_position = FilamentSwitchSensorPosition.PreGears
    mmu.enable_no_selector_mode = False
    return mmu


# ---------------------------------------------------------------------------
# filament_pos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pos", "expected"),
    [
        (FilamentPos.UNLOADED, FILAMENT_POS_UNLOADED),
        (FilamentPos.AT_FINDA, FILAMENT_POS_HOMED_GATE),
        (FilamentPos.IN_HOTEND, FILAMENT_POS_IN_EXTRUDER),
        (FilamentPos.LOADED, FILAMENT_POS_LOADED),
    ],
)
def test_filament_pos_mapping(pos, expected) -> None:
    mmu = make_mmu()
    mmu.filament_pos = pos
    assert MmuStatus(mmu).filament_pos() == expected


@pytest.mark.parametrize(
    ("sensor_pos", "expected"),
    [
        (FilamentSwitchSensorPosition.PreGears, FILAMENT_POS_HOMED_ENTRY),
        (FilamentSwitchSensorPosition.OnGears, FILAMENT_POS_HOMED_ENTRY),
        (FilamentSwitchSensorPosition.PostGears, FILAMENT_POS_EXTRUDER_ENTRY),
    ],
)
def test_filament_pos_at_extruder_depends_on_sensor_position(
    sensor_pos, expected
) -> None:
    mmu = make_mmu()
    mmu.filament_pos = FilamentPos.AT_EXTRUDER
    mmu.filament_switch_sensor_position = sensor_pos
    assert MmuStatus(mmu).filament_pos() == expected


@pytest.mark.parametrize(
    ("pos", "expected"),
    [
        (FilamentPos.UNLOADED, "Unloaded"),
        (FilamentPos.AT_FINDA, "Unknown"),
        (FilamentPos.AT_EXTRUDER, "Unknown"),
        (FilamentPos.IN_HOTEND, "Unknown"),
        (FilamentPos.LOADED, "Loaded"),
    ],
)
def test_filament_state(pos, expected) -> None:
    mmu = make_mmu()
    mmu.filament_pos = pos
    assert MmuStatus(mmu).filament() == expected


# ---------------------------------------------------------------------------
# gate / tool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current_tool", "current_filament", "gate", "tool"),
    [
        (None, None, -1, -1),
        (2, None, 2, 2),
        (None, 3, 3, 3),
        (1, 3, 1, 3),
    ],
)
def test_gate_and_tool_fallbacks(current_tool, current_filament, gate, tool) -> None:
    mmu = make_mmu()
    mmu.current_tool = current_tool
    mmu.current_filament = current_filament
    status = MmuStatus(mmu).get_status(0.0)
    assert status["gate"] == gate
    assert status["tool"] == tool


def test_last_and_next_tool_during_tool_change() -> None:
    mmu = make_mmu()
    mmu.current_operation = Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=4)
    status = MmuStatus(mmu).get_status(0.0)
    assert status["last_tool"] == 1
    assert status["next_tool"] == 4


def test_last_and_next_tool_unknown_outside_tool_change() -> None:
    mmu = make_mmu()
    mmu.current_operation = Operation(OperationKind.LOAD, to_tool=4)
    status = MmuStatus(mmu).get_status(0.0)
    assert status["last_tool"] == -1
    assert status["next_tool"] == -1


# ---------------------------------------------------------------------------
# print_state / pause
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("state", "expected", "in_print"),
    [
        ("standby", "ready", False),
        ("printing", "printing", True),
        ("paused", "paused", True),
        ("complete", "complete", False),
        ("cancelled", "cancelled", False),
        ("error", "error", False),
    ],
)
def test_print_state_mapping(state, expected, in_print) -> None:
    mmu = make_mmu()
    mmu.print_stats = FakePrintStats(state)
    status = MmuStatus(mmu).get_status(0.0)
    assert status["print_state"] == expected
    assert status["is_in_print"] is in_print


def test_print_state_without_print_stats_is_ready() -> None:
    mmu = make_mmu()
    mmu.print_stats = None
    assert MmuStatus(mmu).print_state(0.0) == "ready"


def test_paused_mmu_is_pause_locked_with_reason() -> None:
    mmu = make_mmu()
    mmu.print_stats = FakePrintStats("printing")
    mmu.is_paused = True
    operation = Operation(OperationKind.LOAD, to_tool=2)
    mmu.pending_operation = operation
    status = MmuStatus(mmu).get_status(0.0)
    assert status["print_state"] == "pause_locked"
    assert status["is_paused"] is True
    assert status["is_locked"] is True
    assert status["is_in_print"] is True
    assert status["reason_for_pause"] == operation.describe()


def test_no_reason_for_pause_without_pending_operation() -> None:
    assert MmuStatus(make_mmu()).get_status(0.0)["reason_for_pause"] == ""


# ---------------------------------------------------------------------------
# action / direction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("action", "direction"),
    [
        (ACTION_IDLE, DIRECTION_UNKNOWN),
        (ACTION_LOADING, DIRECTION_LOAD),
        (ACTION_LOADING_EXTRUDER, DIRECTION_LOAD),
        (ACTION_UNLOADING, DIRECTION_UNLOAD),
        (ACTION_UNLOADING_EXTRUDER, DIRECTION_UNLOAD),
        (ACTION_FORMING_TIP, DIRECTION_UNLOAD),
        (ACTION_CUTTING_FILAMENT, DIRECTION_UNKNOWN),
    ],
)
def test_action_and_direction(action, direction) -> None:
    mmu = make_mmu()
    mmu.action = action
    status = MmuStatus(mmu).get_status(0.0)
    assert status["action"] == action
    assert status["filament_direction"] == direction


# ---------------------------------------------------------------------------
# gate arrays / filament metadata
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("num_tools", [5, 12])
def test_arrays_are_num_gates_long(num_tools) -> None:
    status = MmuStatus(make_mmu(num_tools)).get_status(0.0)
    assert status["num_gates"] == num_tools
    for key in (
        "ttg_map",
        "endless_spool_groups",
        "gate_status",
        "gate_filament_name",
        "gate_material",
        "gate_color",
        "gate_temperature",
        "gate_spool_id",
        "gate_speed_override",
    ):
        assert len(status[key]) == num_tools, key


def test_ttg_map_is_identity() -> None:
    assert MmuStatus(make_mmu()).get_status(0.0)["ttg_map"] == [0, 1, 2, 3, 4]


def test_gate_arrays_reflect_gate_map() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, material="PETG", color="#00ff00", spool_id=9)
    status = MmuStatus(mmu).get_status(0.0)
    assert status["gate_material"][1] == "PETG"
    assert status["gate_color"][1] == "00FF00"
    assert status["gate_spool_id"] == [-1, 9, -1, -1, -1]


def test_active_filament_of_loaded_gate() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(
        2, name="Silk Gold", material="PLA", color="ffd700", temperature=215, spool_id=4
    )
    mmu.current_filament = 2
    assert MmuStatus(mmu).get_status(0.0)["active_filament"] == {
        "filament_name": "Silk Gold",
        "material": "PLA",
        "color": "FFD700",
        "spool_id": 4,
        "temperature": 215,
    }


def test_active_filament_empty_when_nothing_loaded() -> None:
    active = MmuStatus(make_mmu()).get_status(0.0)["active_filament"]
    assert active["spool_id"] == -1
    assert active["material"] == ""


def test_num_toolchanges_counts_successful_ones() -> None:
    mmu = make_mmu()
    change = Operation(OperationKind.TOOL_CHANGE, from_tool=0, to_tool=1)
    mmu.job_stats.record(change, success=True)
    mmu.job_stats.record(change, success=True)
    mmu.job_stats.record(change, success=False)
    assert MmuStatus(mmu).get_status(0.0)["num_toolchanges"] == 2


# ---------------------------------------------------------------------------
# sensors
# ---------------------------------------------------------------------------
def test_gate_sensor_follows_filament_pos() -> None:
    mmu = make_mmu()
    assert MmuStatus(mmu).sensors() == {"mmu_gate": False}
    mmu.filament_pos = FilamentPos.AT_FINDA
    assert MmuStatus(mmu).sensors() == {"mmu_gate": True}


@pytest.mark.parametrize(
    ("sensor_pos", "key"),
    [
        (FilamentSwitchSensorPosition.PreGears, "extruder"),
        (FilamentSwitchSensorPosition.PostGears, "toolhead"),
    ],
)
def test_switch_sensor_key(sensor_pos, key) -> None:
    mmu = make_mmu()
    mmu.filament_switch_sensor = FakeSwitchSensor(detected=True)
    mmu.filament_switch_sensor_position = sensor_pos
    assert MmuStatus(mmu).sensors() == {"mmu_gate": False, key: True}


# ---------------------------------------------------------------------------
# mmu_machine
# ---------------------------------------------------------------------------
def test_mmu_machine_unit() -> None:
    status = MmuMachine(make_mmu(12)).get_status(0.0)
    assert status["num_units"] == 1
    unit = status["unit_0"]
    assert unit["num_gates"] == 12
    assert unit["first_gate"] == 0
    assert unit["has_bypass"] is False
    assert unit["selector_type"] == "LinearSelector"


def test_mmu_machine_no_selector_mode() -> None:
    mmu = make_mmu()
    mmu.enable_no_selector_mode = True
    unit = MmuMachine(mmu).get_status(0.0)["unit_0"]
    assert unit["selector_type"] == "VirtualSelector"
