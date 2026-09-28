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
from extras.mmu import (  # noqa: E402
    MMU,
    FilamentPos,
    FilamentTracker,
    FilamentSwitchSensorPosition,
    Operation,
    OperationKind,
    OperationStats,
)
from extras.mmu_gate_map import GateMap  # noqa: E402
from extras.mmu_hh_compat import (  # noqa: E402
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


def make_mmu(num_tools: int = 5) -> MMU:
    """Build a bare MMU3 instance with just what the status objects read."""
    mmu = object.__new__(MMU)
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.is_enabled = True
    mmu.is_homed = True
    mmu.is_paused = False
    mmu.print_stats = FakePrintStats("standby")
    mmu._print_stats_state = "standby"
    mmu.print_state = "ready"
    mmu.print_start_detection = True
    mmu.current_gate = None
    mmu.loaded_gate = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.finda_triggered = False
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.job_stats = OperationStats()
    mmu.action = ACTION_IDLE
    mmu.spoolman_support = "readonly"
    mmu.filament_switch_sensor = None
    mmu.filament_switch_sensor_position = FilamentSwitchSensorPosition.PreGears
    mmu.enable_no_selector_mode = False
    mmu.bowden_load_length1 = 450
    mmu.bowden_load_length3 = 20
    mmu.extra_load_length = 30
    mmu.filament_tracker = FilamentTracker(mmu)
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
    ("current_gate", "loaded_gate", "gate", "tool"),
    [
        (None, None, -1, -1),
        (2, None, 2, 2),
        (None, 3, 3, 3),
        (1, 3, 1, 3),
    ],
)
def test_gate_and_tool_fallbacks(current_gate, loaded_gate, gate, tool) -> None:
    mmu = make_mmu()
    mmu.current_gate = current_gate
    mmu.loaded_gate = loaded_gate
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
    # every state but standby is reached from a print
    mmu.print_stats.state = "printing"
    MmuStatus(mmu).get_status(0.0)
    mmu.print_stats.state = state
    status = MmuStatus(mmu).get_status(0.0)
    assert status["print_state"] == expected
    assert status["is_in_print"] is in_print


@pytest.mark.parametrize("enabled", [True, False])
def test_print_start_detection_is_reported(enabled) -> None:
    mmu = make_mmu()
    mmu.print_start_detection = enabled
    assert MmuStatus(mmu).get_status(0.0)["print_start_detection"] == int(enabled)


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
    mmu.loaded_gate = 2
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
@pytest.mark.parametrize(
    ("pos", "triggered"),
    [
        (FilamentPos.UNLOADED, True),
        (FilamentPos.AT_FINDA, False),
    ],
)
def test_gate_sensor_reports_finda_not_filament_pos(pos, triggered) -> None:
    mmu = make_mmu()
    mmu.filament_pos = pos
    mmu.finda_triggered = triggered
    assert MmuStatus(mmu).sensors() == {"mmu_gate": triggered}


class FakePins:
    """A ``pins`` stand-in recording the pins allowed to be shared."""

    def __init__(self) -> None:
        self.multi_use_pins = []

    def allow_multi_use_pin(self, pin_desc):
        # like Klipper, the chip name is whatever is before the ":", so a
        # ^ ~ ! modifier ends up in it and fails the chip lookup
        chip_name = pin_desc.split(":", 1)[0].strip()
        if chip_name not in ("mcu", "mmboard"):
            raise ValueError(f"Unknown pin chip name '{chip_name}'")
        self.multi_use_pins.append(pin_desc)


class FakeButtons:
    """A ``buttons`` stand-in recording the registered pins."""

    def __init__(self) -> None:
        self.registered = []

    def register_buttons(self, pins, callback):
        self.registered.append((pins, callback))


class FakePrinter:
    """A ``printer`` stand-in serving the ``pins`` and ``buttons`` objects."""

    def __init__(self) -> None:
        self.pins = FakePins()
        self.buttons = FakeButtons()

    def lookup_object(self, name):
        assert name == "pins"
        return self.pins

    def load_object(self, config, name):
        assert name == "buttons"
        return self.buttons


class FakeConfig:
    """A config stand-in with the pulley stepper's section."""

    def __init__(self, sections: dict) -> None:
        self.sections = sections

    def getsection(self, name):
        return FakeConfig(self.sections[name])

    def get(self, option):
        return self.sections[option]


@pytest.mark.parametrize(
    "pin",
    ["mmboard:PC15", "^mmboard:PC15", "~!mmboard:PC15", " ^ ! mmboard:PC15"],
)
def test_setup_finda_sensor_shares_the_pulley_endstop_pin(pin) -> None:
    mmu = make_mmu()
    mmu.printer = FakePrinter()
    config = FakeConfig({"manual_stepper pulley_stepper": {"endstop_pin": pin}})
    mmu.setup_finda_sensor(config)
    # the bare pin is shared, the buttons get the modifiers (e.g. the pullup)
    assert mmu.printer.pins.multi_use_pins == ["mmboard:PC15"]
    assert mmu.printer.buttons.registered == [([pin], mmu._handle_finda_state)]


def test_finda_state_follows_the_mcu_reports() -> None:
    mmu = make_mmu()
    mmu._handle_finda_state(0.0, 1)
    assert MmuStatus(mmu).sensors() == {"mmu_gate": True}
    # e.g. the filament was removed by hand
    mmu._handle_finda_state(1.0, 0)
    assert MmuStatus(mmu).sensors() == {"mmu_gate": False}


class FakeEndstop:
    """A FINDA endstop stand-in returning a fixed reading."""

    def __init__(self, triggered: int) -> None:
        self.triggered = triggered

    def query_endstop(self, print_time):
        return self.triggered


class FakeToolhead:
    """A ``toolhead`` stand-in."""

    def get_last_move_time(self):
        return 0.0


@pytest.mark.parametrize("triggered", [0, 1])
def test_is_filament_in_finda_caches_the_reading(triggered) -> None:
    mmu = make_mmu()
    mmu.toolhead = FakeToolhead()
    mmu.pulley_stepper_endstop = FakeEndstop(triggered)
    assert mmu.is_filament_in_finda() is bool(triggered)
    assert mmu.finda_triggered is bool(triggered)
    assert MmuStatus(mmu).sensors() == {"mmu_gate": bool(triggered)}


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


# ---------------------------------------------------------------------------
# printer.mmu
# ---------------------------------------------------------------------------
class FakeObjectPrinter:
    """A ``printer`` stand-in recording the added objects."""

    def __init__(self) -> None:
        self.objects = {}

    def add_object(self, name, obj):
        self.objects[name] = obj


def make_printer_mmu(num_tools: int = 5) -> MMU:
    """Build a bare MMU3 instance that can report the printer.mmu status."""
    mmu = make_mmu(num_tools)
    mmu.hh_status = MmuStatus(mmu)
    mmu.total_stats = OperationStats()
    return mmu


def test_printer_mmu_has_the_happy_hare_fields() -> None:
    mmu = make_printer_mmu(12)
    status = mmu.get_status(0.0)
    hh_status = MmuStatus(mmu).get_status(0.0)
    for key, value in hh_status.items():
        assert status[key] == value, key


def test_printer_mmu_has_the_extra_fields() -> None:
    mmu = make_printer_mmu()
    mmu.current_gate = 2
    mmu.loaded_gate = 2
    mmu.filament_pos = FilamentPos.LOADED
    status = mmu.get_status(0.0)
    assert status["is_enabled"] is True
    assert status["current_gate"] == 2
    assert status["loaded_gate"] == 2
    # the pre tool / gate split names, kept for user macros
    assert status["current_tool"] == 2
    assert status["current_filament"] == 2
    assert status["filament_pos"] == FILAMENT_POS_LOADED
    assert status["filament_pos_name"] == "LOADED"
    assert status["pending_operation"] is None
    assert status["total_stats"] == OperationStats().to_dict()
    assert status["job_stats"] == OperationStats().to_dict()
    assert status["gate_map"] == mmu.gate_map.to_dict()


def test_printer_mmu_reports_the_pending_operation() -> None:
    mmu = make_printer_mmu()
    mmu.pending_operation = Operation(OperationKind.LOAD, to_tool=1)
    status = mmu.get_status(0.0)
    assert status["pending_operation"] == mmu.pending_operation.describe()
    assert status["reason_for_pause"] == mmu.pending_operation.describe()


def test_register_mmu_panel_adds_only_mmu_machine() -> None:
    mmu = make_printer_mmu()
    mmu.printer = FakeObjectPrinter()
    mmu.register_mmu_panel()
    assert list(mmu.printer.objects) == ["mmu_machine"]
    assert isinstance(mmu.printer.objects["mmu_machine"], MmuMachine)
