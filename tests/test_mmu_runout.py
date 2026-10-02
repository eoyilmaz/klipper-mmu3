"""Tests for ``MMU_RUNOUT``, endless spool on runout and the runout sensors
during ``M702``."""

# Standard Library Imports
import inspect
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
from extras.mmu_hh_compat import (  # noqa: E402
    ACTION_IDLE,
    ACTION_UNLOADING,
    PRINT_STATE_PAUSED,
    PRINT_STATE_PRINTING,
    PRINT_STATE_READY,
)


class FakeGCmd:
    """A ``GCodeCommand`` stand-in, ``MMU_RUNOUT`` takes no parameters."""


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCode:
    """Records the scripts run."""

    def __init__(self) -> None:
        self.scripts = []

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def run_script(self, script):
        self.scripts.append(script)


class FakeReactor:
    """Records the timer updates and the callbacks registered."""

    NOW = 0.0
    NEVER = 9999999999999999.0

    def __init__(self) -> None:
        self.timer_waketimes = []
        self.callbacks = []

    def monotonic(self):
        return 1.0

    def update_timer(self, timer, waketime):
        self.timer_waketimes.append(waketime)

    def register_callback(self, callback):
        self.callbacks.append(callback)


class FakePauseResume:
    """Records ``send_pause_command()``."""

    def __init__(self) -> None:
        self.pause_commands = 0

    def send_pause_command(self):
        self.pause_commands += 1


def make_mmu(num_tools: int = 5, finda: bool = False, switch: bool = False) -> MMU:
    """Build a bare MMU3 with gate 2 loaded and available, while printing.

    ``finda`` / ``switch`` are what FINDA and the filament switch sensor see.
    """
    mmu = object.__new__(MMU)
    mmu.gcode = FakeGCode()
    mmu.reactor = FakeReactor()
    mmu._runout_tail_timer = object()
    mmu.runout_tail_gate = None
    mmu.runout_tail_start = 0.0
    mmu.runout_tail_length = 150.0
    mmu.extruder_pos = 100.0
    mmu.extruder_position = lambda eventtime: mmu.extruder_pos
    mmu.pause_resume = FakePauseResume()
    mmu.printer = types.SimpleNamespace(
        lookup_object=lambda name, default=None: (
            mmu.pause_resume if name == "pause_resume" else default
        )
    )
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.ttg_map = list(range(num_tools))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(num_tools))
    mmu.selected_tool = None
    mmu.gate_map.update(2, status=GATE_AVAILABLE)
    mmu.save_variables = None
    mmu.is_enabled = True
    mmu.current_gate = 2
    mmu.loaded_gate = 2
    mmu.filament_pos = FilamentPos.LOADED
    mmu.action = ACTION_IDLE
    mmu.print_state = PRINT_STATE_PRINTING
    mmu.enable_no_selector_mode = False
    mmu.messages = []
    mmu.respond_info = mmu.messages.append
    mmu.is_filament_in_finda = lambda: finda
    mmu.is_filament_in_switch_sensor = lambda: switch
    return mmu


def with_endless_spool(
    mmu: MMU,
    groups: list[int],
    tx_result: bool = True,
    macros: tuple[str, ...] = (),
    failing_macro: str = "",
) -> list:
    """Enable endless spool with ``groups``, record the tool changes.

    Args:
        mmu (MMU): The MMU.
        groups (list[int]): The endless spool groups.
        tx_result (bool): What the tool change returns.
        macros (tuple[str, ...]): The user macros that are defined.
        failing_macro (str): A macro that raises an error.

    Returns:
        list: The ``(tool, gate)`` of each tool change, and the scripts run
            (``"PAUSE"`` / ``"RESUME"`` / macros) in order.
    """
    mmu.endless_spool_enabled = True
    mmu.endless_spool_groups = groups
    events = []

    def run_script_from_command(script):
        events.append(script)
        if script == failing_macro:
            raise CommandError("macro failed")

    mmu.gcode.run_script_from_command = run_script_from_command
    mmu.is_macro_defined = lambda name: name in macros
    mmu.printer.command_error = CommandError
    mmu.toolhead = types.SimpleNamespace(wait_moves=lambda: None)
    mmu.debug = False
    mmu.current_operation = None
    mmu.display_status_msg = mmu.messages.append

    def cmd_tx(gcmd, tool_id=0, gate=None):
        events.append(("T", tool_id, gate))
        if tx_result:
            mmu.loaded_gate = gate
        return tx_result

    mmu.cmd_tx = cmd_tx
    return events


def test_runout_marks_the_loaded_gate_empty_and_pauses() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY
    assert mmu.messages == [
        "Gate 2 ran out of filament, marked empty. Pausing the print."
    ]
    assert mmu.gcode.scripts == ["PAUSE"]


def test_runout_with_filament_in_finda_keeps_the_gate() -> None:
    # e.g. a sensor before the gears: the filament broke in the bowden
    mmu = make_mmu(finda=True)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert "FINDA still detects filament" in mmu.messages[0]
    assert mmu.gcode.scripts == ["PAUSE"]


def test_runout_with_filament_in_finda_does_not_use_endless_spool() -> None:
    mmu = make_mmu(finda=True)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert mmu.ttg_map == [0, 1, 2, 3, 4]


def test_runout_in_no_selector_mode_does_not_read_finda() -> None:
    mmu = make_mmu(finda=True)
    mmu.enable_no_selector_mode = True
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY


def test_runout_while_the_mmu_is_busy_is_ignored() -> None:
    mmu = make_mmu()
    mmu.action = ACTION_UNLOADING
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert mmu.messages == [f"MMU is busy ({ACTION_UNLOADING}), runout ignored."]
    assert mmu.gcode.scripts == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda mmu: setattr(mmu, "loaded_gate", None),
            "Filament runout, no filament loaded. Pausing the print.",
        ),
        (
            lambda mmu: setattr(mmu, "filament_pos", FilamentPos.AT_FINDA),
            "Filament runout, no filament loaded. Pausing the print.",
        ),
        (
            lambda mmu: setattr(mmu, "is_enabled", False),
            "Filament runout, the MMU is disabled. Pausing the print.",
        ),
    ],
)
def test_runout_the_mmu_can_not_handle_pauses_the_print(setup, message) -> None:
    mmu = make_mmu()
    setup(mmu)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert mmu.messages == [message]
    assert mmu.gcode.scripts == ["PAUSE"]


# ---------------------------------------------------------------------------
# endless spool
# ---------------------------------------------------------------------------
def test_endless_spool_continues_with_the_next_gate_of_the_group() -> None:
    mmu = make_mmu()
    mmu.selected_tool = 2
    mmu.gate_map.update(4, status=GATE_AVAILABLE)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY
    # parked by PAUSE, remapped and loaded like T2, then resumed
    assert events == ["PAUSE", ("T", 2, 4), "RESUME"]
    assert mmu.ttg_map == [0, 1, 4, 3, 4]
    assert mmu.messages == [
        "Gate 2 ran out of filament, marked empty. Endless spool: T2 "
        "continues with gate 4 (group A).",
        "Remapped T2 to gate 4.",
    ]


def test_endless_spool_remaps_the_tool_that_is_loaded() -> None:
    # T3 loads gate 2
    mmu = make_mmu()
    mmu.ttg_map = [0, 1, 3, 2, 4]
    mmu.selected_tool = 3
    events = with_endless_spool(mmu, [0, 0, 0, 1, 1])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE", ("T", 3, 0), "RESUME"]
    assert mmu.ttg_map == [0, 1, 3, 0, 4]


def test_endless_spool_pauses_when_no_gate_is_left() -> None:
    mmu = make_mmu()
    mmu.selected_tool = 2
    mmu.gate_map.update(0, status=GATE_EMPTY)
    mmu.gate_map.update(4, status=GATE_EMPTY)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert mmu.messages == [
        "Gate 2 ran out of filament, marked empty. Endless spool: no gate "
        "left for T2 in group A (checked gates: 4, 0). Pausing the print."
    ]


def test_endless_spool_pauses_when_the_gate_is_alone_in_its_group() -> None:
    mmu = make_mmu()
    events = with_endless_spool(mmu, [0, 1, 2, 3, 4])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert "(checked gates: none)" in mmu.messages[0]


@pytest.mark.parametrize("print_state", [PRINT_STATE_READY, "complete"])
def test_endless_spool_only_continues_a_print(print_state) -> None:
    mmu = make_mmu()
    mmu.print_state = print_state
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert mmu.gate_map.gates[2].status == GATE_EMPTY
    assert "endless spool only continues a print" in mmu.messages[0]


def test_endless_spool_works_while_the_print_is_paused() -> None:
    mmu = make_mmu()
    mmu.print_state = PRINT_STATE_PAUSED
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE", ("T", 2, 4), "RESUME"]


def test_endless_spool_pauses_when_no_tool_maps_to_the_gate() -> None:
    mmu = make_mmu()
    mmu.ttg_map = [0, 1, 0, 3, 4]
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert "No tool maps to gate 2" in mmu.messages[0]


def test_endless_spool_tries_unknown_gates() -> None:
    mmu = make_mmu()
    assert mmu.gate_map.gates[4].status == GATE_UNKNOWN
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert ("T", 2, 4) in events


def test_endless_spool_does_not_resume_when_the_tool_change_fails() -> None:
    # cmd_tx's auto_pause then pauses the MMU, RESUME_MMU retries the change
    mmu = make_mmu()
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0], tx_result=False)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is False
    assert events == ["PAUSE", ("T", 2, 4)]
    assert mmu.ttg_map == [0, 1, 4, 3, 4]


PRE_UNLOAD = "_MMU_ENDLESS_SPOOL_PRE_UNLOAD"
POST_LOAD = "_MMU_ENDLESS_SPOOL_POST_LOAD"


def test_endless_spool_runs_its_macros_around_the_tool_change() -> None:
    mmu = make_mmu()
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0], macros=(PRE_UNLOAD, POST_LOAD))
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE", PRE_UNLOAD, ("T", 2, 4), POST_LOAD, "RESUME"]


def test_endless_spool_skips_undefined_macros() -> None:
    mmu = make_mmu()
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0], macros=(POST_LOAD,))
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE", ("T", 2, 4), POST_LOAD, "RESUME"]


def test_endless_spool_stays_paused_when_the_pre_unload_macro_fails() -> None:
    mmu = make_mmu()
    events = with_endless_spool(
        mmu,
        [0, 1, 0, 1, 0],
        macros=(PRE_UNLOAD, POST_LOAD),
        failing_macro=PRE_UNLOAD,
    )
    assert mmu.cmd_mmu_runout(FakeGCmd()) is False
    assert events == ["PAUSE", PRE_UNLOAD]
    # not remapped, nothing was changed yet
    assert mmu.ttg_map == [0, 1, 2, 3, 4]
    assert f"{PRE_UNLOAD} failed: macro failed" in mmu.messages


def test_endless_spool_stays_paused_when_the_post_load_macro_fails() -> None:
    mmu = make_mmu()
    events = with_endless_spool(
        mmu, [0, 1, 0, 1, 0], macros=(POST_LOAD,), failing_macro=POST_LOAD
    )
    assert mmu.cmd_mmu_runout(FakeGCmd()) is False
    assert events == ["PAUSE", ("T", 2, 4), POST_LOAD]
    assert mmu.ttg_map == [0, 1, 4, 3, 4]


def test_endless_spool_does_not_run_the_post_load_macro_after_a_failure() -> None:
    mmu = make_mmu()
    events = with_endless_spool(
        mmu, [0, 1, 0, 1, 0], tx_result=False, macros=(PRE_UNLOAD, POST_LOAD)
    )
    assert mmu.cmd_mmu_runout(FakeGCmd()) is False
    assert events == ["PAUSE", PRE_UNLOAD, ("T", 2, 4)]


# ---------------------------------------------------------------------------
# runout before the filament switch sensor (e.g. a motion sensor)
# ---------------------------------------------------------------------------
def test_runout_before_the_switch_sensor_waits_for_the_end_of_the_filament() -> None:
    # FINDA is empty, the spool ran out, the switch sensor still sees the rest
    mmu = make_mmu(switch=True)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY
    # no pause, no tool change: the print uses up the rest of the filament
    assert events == []
    assert mmu.messages == [
        "Gate 2 ran out of filament, marked empty. Printing on until the end "
        "of the filament reaches the filament switch sensor."
    ]
    assert mmu.runout_tail_gate == 2
    assert mmu.runout_tail_start == 100.0
    assert mmu.reactor.timer_waketimes == [FakeReactor.NOW]


def test_runout_before_the_switch_sensor_with_endless_spool_off() -> None:
    mmu = make_mmu(switch=True)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gcode.scripts == []
    assert mmu.runout_tail_gate == 2


def test_a_second_early_runout_keeps_waiting_from_the_first() -> None:
    mmu = make_mmu(switch=True)
    mmu.cmd_mmu_runout(FakeGCmd())
    mmu.extruder_pos = 130.0
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.runout_tail_start == 100.0
    assert len(mmu.messages) == 1


def test_clog_before_the_switch_sensor_pauses() -> None:
    # e.g. a motion sensor on a clogged nozzle: FINDA still sees filament
    mmu = make_mmu(finda=True, switch=True)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert "The nozzle may be clogged" in mmu.messages[0]
    assert mmu.runout_tail_gate is None


def test_runout_before_the_switch_sensor_without_finda_pauses() -> None:
    mmu = make_mmu(finda=True, switch=True)
    mmu.enable_no_selector_mode = True
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE"]
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert "Without FINDA a clog can't be told from a runout" in mmu.messages[0]
    assert mmu.runout_tail_gate is None


def test_runout_before_the_switch_sensor_outside_a_print_pauses() -> None:
    mmu = make_mmu(switch=True)
    mmu.print_state = PRINT_STATE_READY
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gcode.scripts == ["PAUSE"]
    assert mmu.runout_tail_gate is None


def test_switch_sensor_runout_after_the_wait_continues_with_endless_spool() -> None:
    mmu = make_mmu(switch=True)
    events = with_endless_spool(mmu, [0, 1, 0, 1, 0])
    mmu.cmd_mmu_runout(FakeGCmd())
    # the end of the filament reaches the switch sensor
    mmu.is_filament_in_switch_sensor = lambda: False
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert events == ["PAUSE", ("T", 2, 4), "RESUME"]
    assert mmu.runout_tail_gate is None
    assert mmu.reactor.timer_waketimes[-1] == FakeReactor.NEVER


def test_tail_check_keeps_waiting_within_the_length() -> None:
    mmu = make_mmu(switch=True)
    mmu.cmd_mmu_runout(FakeGCmd())
    mmu.extruder_pos = 250.0  # exactly runout_tail_length used
    assert mmu._check_runout_tail(5.0) == 5.0 + 0.5
    assert mmu.reactor.callbacks == []
    assert mmu.runout_tail_gate == 2


def test_tail_check_pauses_when_the_end_of_the_filament_is_stuck() -> None:
    mmu = make_mmu(switch=True)
    mmu.cmd_mmu_runout(FakeGCmd())
    mmu.extruder_pos = 250.1
    assert mmu._check_runout_tail(5.0) == FakeReactor.NEVER
    assert mmu.runout_tail_gate is None
    assert len(mmu.reactor.callbacks) == 1
    mmu.reactor.callbacks[0](6.0)
    assert mmu.pause_resume.pause_commands == 1
    assert mmu.gcode.scripts == ["PAUSE"]
    assert mmu.messages[-1] == (
        "Gate 2 ran out of filament, but the filament switch sensor still "
        "detects filament after 150 mm. The end of the filament may be stuck "
        "in the bowden. Pausing the print."
    )


@pytest.mark.parametrize(
    "setup",
    [
        lambda mmu: setattr(mmu, "print_state", PRINT_STATE_READY),
        lambda mmu: setattr(mmu, "loaded_gate", 4),
        lambda mmu: setattr(mmu, "filament_pos", FilamentPos.UNLOADED),
        lambda mmu: setattr(mmu, "runout_tail_gate", None),
    ],
)
def test_tail_check_stops_when_there_is_nothing_to_wait_for(setup) -> None:
    mmu = make_mmu(switch=True)
    mmu.cmd_mmu_runout(FakeGCmd())
    setup(mmu)
    mmu.extruder_pos = 1000.0
    assert mmu._check_runout_tail(5.0) == FakeReactor.NEVER
    assert mmu.runout_tail_gate is None
    assert mmu.reactor.callbacks == []


# ---------------------------------------------------------------------------
# M702 / MMU_EJECT
# ---------------------------------------------------------------------------
class FakeSensor:
    """A switch / motion sensor stand-in."""

    def __init__(self) -> None:
        self.runout_helper = types.SimpleNamespace(sensor_enabled=True)

    def encoder_event(self, eventtime, state) -> None:
        pass


def test_m702_turns_off_the_runout_sensors_while_unloading() -> None:
    mmu = make_mmu()
    mmu.debug = False
    mmu.filament_switch_sensor = FakeSensor()
    mmu.filament_motion_sensor = FakeSensor()
    mmu.reactor = types.SimpleNamespace(monotonic=lambda: 1.0)
    mmu.toolhead = types.SimpleNamespace(wait_moves=lambda: None)
    mmu.display_status_msg = mmu.messages.append
    states = []

    def unload_gate():
        states.append(
            (
                mmu.filament_switch_sensor.runout_helper.sensor_enabled,
                mmu.filament_motion_sensor.runout_helper.sensor_enabled,
            )
        )
        return True

    mmu.unload_gate = unload_gate
    mmu.unselect_gate = lambda: True
    # skip the pause / stats / stepper decorators
    m702 = inspect.unwrap(MMU.cmd_m702)
    assert m702(mmu, FakeGCmd()) is True
    assert states == [(False, False)]
    assert mmu.filament_switch_sensor.runout_helper.sensor_enabled is True
    assert mmu.filament_motion_sensor.runout_helper.sensor_enabled is True
