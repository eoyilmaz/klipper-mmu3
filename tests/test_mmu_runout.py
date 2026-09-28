"""Tests for ``MMU_RUNOUT`` and the runout sensors during ``M702``."""

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
from extras.mmu import MMU, FilamentPos  # noqa: E402
from extras.mmu_gate_map import (  # noqa: E402
    GATE_AVAILABLE,
    GATE_EMPTY,
    GateMap,
)
from extras.mmu_hh_compat import ACTION_IDLE, ACTION_UNLOADING  # noqa: E402


class FakeGCmd:
    """A ``GCodeCommand`` stand-in, ``MMU_RUNOUT`` takes no parameters."""


def make_mmu(num_tools: int = 5, finda: bool = False) -> MMU:
    """Build a bare MMU3 with gate 2 loaded and available."""
    mmu = object.__new__(MMU)
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.gate_map.update(2, status=GATE_AVAILABLE)
    mmu.save_variables = None
    mmu.is_enabled = True
    mmu.current_gate = 2
    mmu.loaded_gate = 2
    mmu.filament_pos = FilamentPos.LOADED
    mmu.action = ACTION_IDLE
    mmu.enable_no_selector_mode = False
    mmu.messages = []
    mmu.respond_info = mmu.messages.append
    mmu.is_filament_in_finda = lambda: finda
    return mmu


def test_runout_marks_the_loaded_gate_empty() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY
    assert mmu.messages == ["Gate 2 ran out of filament, marked empty."]


def test_runout_with_filament_in_finda_keeps_the_gate() -> None:
    # e.g. a sensor before the gears: the filament broke in the bowden
    mmu = make_mmu(finda=True)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert "FINDA still detects filament" in mmu.messages[0]


def test_runout_in_no_selector_mode_does_not_read_finda() -> None:
    mmu = make_mmu(finda=True)
    mmu.enable_no_selector_mode = True
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_EMPTY


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda mmu: setattr(mmu, "action", ACTION_UNLOADING),
            f"MMU is busy ({ACTION_UNLOADING}), runout ignored.",
        ),
        (
            lambda mmu: setattr(mmu, "loaded_gate", None),
            "No filament loaded, runout ignored.",
        ),
        (
            lambda mmu: setattr(mmu, "filament_pos", FilamentPos.AT_FINDA),
            "No filament loaded, runout ignored.",
        ),
        (
            lambda mmu: setattr(mmu, "is_enabled", False),
            "MMU is disabled, runout ignored.",
        ),
    ],
)
def test_runout_is_ignored(setup, message) -> None:
    mmu = make_mmu()
    setup(mmu)
    assert mmu.cmd_mmu_runout(FakeGCmd()) is True
    assert mmu.gate_map.gates[2].status == GATE_AVAILABLE
    assert mmu.messages == [message]


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
