"""Tests for the standalone ``MMU_FORM_TIP`` and ``MMU_CUT`` commands.

Both run the tip forming / cutting step of an unload on its own and leave the
filament in the extruder, no longer ``LOADED``.
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
    CUT_TIP_MACRO,
    FORM_TIP_MACRO,
    MMU,
    FilamentPos,
    FilamentSwitchSensorPosition,
)
from extras.mmu_hh_compat import (  # noqa: E402
    ACTION_CUTTING_TIP,
    ACTION_FORMING_TIP,
    ACTION_IDLE,
)


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


def make_mmu(cutter: bool = False, hot: bool = True) -> MMU:
    """Build a bare MMU3 with T1 loaded whose moves only record what was done.

    Args:
        cutter: The ``enable_filament_cutter`` setting.
        hot: Whether the extruder is hot enough.
    """
    mmu = object.__new__(MMU)
    mmu.ttg_map = list(range(5))
    mmu.selected_tool = None
    mmu.printer = types.SimpleNamespace(
        command_error=CommandError, lookup_object=lambda name, default=None: default
    )
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.filament_pos = FilamentPos.LOADED
    mmu.current_gate = None
    mmu.loaded_gate = 1
    mmu.enable_filament_cutter = cutter
    mmu.filament_switch_sensor = None
    mmu.filament_motion_sensor = None
    mmu.filament_switch_sensor_position = FilamentSwitchSensorPosition.PostGears
    mmu.reactor = None
    mmu.action = ACTION_IDLE
    mmu.in_finda = True
    mmu.in_switch = True
    mmu.calls = []
    mmu.display_status_msg = lambda msg: mmu.calls.append(("msg", msg))
    mmu.respond_debug = lambda msg: None
    mmu.respond_info = lambda msg: mmu.calls.append(("info", msg))
    mmu.validate_extruder_is_hot_enough = lambda: hot
    mmu.is_filament_in_finda = lambda: mmu.in_finda
    mmu.is_filament_in_switch_sensor = lambda: mmu.in_switch
    mmu.disable_steppers = lambda: mmu.calls.append(("steppers_off",))

    def run_script_from_command(script):
        mmu.calls.append(("macro", script.split()[0], mmu.action))

    mmu.gcode = types.SimpleNamespace(run_script_from_command=run_script_from_command)
    mmu.toolhead = types.SimpleNamespace(wait_moves=lambda: None)

    def unselect_gate() -> bool:
        mmu.calls.append(("unselect", mmu.current_gate))
        mmu.current_gate = None
        return True

    mmu.unselect_gate = unselect_gate
    return mmu


def macro_calls(mmu: MMU) -> list:
    """The macros ``mmu`` ran with the action reported at the time."""
    return [call[1:] for call in mmu.calls if call[0] == "macro"]


def test_form_tip_rams_and_leaves_the_filament_in_the_hotend() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_form_tip(None) is True
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]
    assert mmu.filament_pos == FilamentPos.IN_HOTEND
    assert mmu.loaded_gate == 1
    assert mmu.action == ACTION_IDLE
    assert mmu.calls[-1] == ("steppers_off",)


def test_form_tip_rams_even_with_a_cutter() -> None:
    mmu = make_mmu(cutter=True)
    assert mmu.cmd_mmu_form_tip(None) is True
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]


def test_cut_cuts_and_leaves_the_filament_in_the_hotend() -> None:
    mmu = make_mmu(cutter=True)
    assert mmu.cmd_mmu_cut(None) is True
    assert macro_calls(mmu) == [("_MMU_CUT_TIP", ACTION_CUTTING_TIP)]
    assert mmu.filament_pos == FilamentPos.IN_HOTEND
    assert mmu.action == ACTION_IDLE


def test_cut_needs_the_filament_cutter() -> None:
    mmu = make_mmu(cutter=False)
    assert mmu.cmd_mmu_cut(None) is False
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.LOADED


@pytest.mark.parametrize("cut", [False, True])
def test_the_sensors_lower_filament_pos_after_the_move(cut) -> None:
    mmu = make_mmu(cutter=True)

    def run_script_from_command(script):
        # the retraction pulled the filament out of the post gears sensor
        mmu.in_switch = False

    mmu.gcode.run_script_from_command = run_script_from_command
    assert mmu.form_tip_standalone(cut=cut) is True
    assert mmu.filament_pos == FilamentPos.AT_EXTRUDER


@pytest.mark.parametrize("cut", [False, True])
def test_refused_when_the_extruder_is_cold(cut) -> None:
    mmu = make_mmu(cutter=True, hot=False)
    assert mmu.form_tip_standalone(cut=cut) is False
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.LOADED


@pytest.mark.parametrize(
    "pos", [FilamentPos.UNLOADED, FilamentPos.AT_EXTRUDER, FilamentPos.IN_HOTEND]
)
@pytest.mark.parametrize("cut", [False, True])
def test_refused_when_the_filament_is_not_loaded(pos, cut) -> None:
    mmu = make_mmu(cutter=True)
    mmu.filament_pos = pos
    assert mmu.form_tip_standalone(cut=cut) is False
    assert macro_calls(mmu) == []


def test_refused_when_the_sensors_do_not_see_the_filament() -> None:
    mmu = make_mmu()
    mmu.in_finda = False
    mmu.in_switch = False
    assert mmu.form_tip_standalone() is False
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.UNLOADED


def test_refused_when_paused() -> None:
    mmu = make_mmu()
    mmu.is_paused = True
    assert mmu.form_tip_standalone() is False
    assert macro_calls(mmu) == []


def test_refused_when_disabled() -> None:
    mmu = make_mmu()
    mmu.is_enabled = False
    assert mmu.form_tip_standalone() is False
    assert macro_calls(mmu) == []


def test_the_idler_is_parked_first() -> None:
    mmu = make_mmu()
    mmu.current_gate = 1
    assert mmu.form_tip_standalone() is True
    assert mmu.calls.index(("unselect", 1)) < mmu.calls.index(
        ("macro", "_MMU_FORM_TIP", ACTION_FORMING_TIP)
    )


def test_unload_with_ramming_uses_the_same_steps() -> None:
    mmu = make_mmu()
    mmu.unload_filament_from_hotend = lambda: True
    assert mmu.unload_filament_from_hotend_with_ramming() is True
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]

    mmu = make_mmu(cutter=True)
    mmu.unload_filament_from_hotend = lambda: True
    assert mmu.unload_filament_from_hotend_with_ramming() is True
    assert macro_calls(mmu) == [("_MMU_CUT_TIP", ACTION_CUTTING_TIP)]


def test_commands_are_registered() -> None:
    mmu = make_mmu()
    names = {name for name, _, _ in mmu.command_table()}
    assert {"MMU_FORM_TIP", "MMU_CUT"} <= names


class ConfigError(Exception):
    """Stand-in for Klipper's ``configfile.error``."""


@pytest.mark.parametrize(
    ("cutter", "macros", "old_name"),
    [
        (False, {FORM_TIP_MACRO}, None),
        (True, {FORM_TIP_MACRO, CUT_TIP_MACRO}, None),
        (False, set(), "RAMMING_SLICER"),
        (False, {"RAMMING_SLICER"}, "RAMMING_SLICER"),
        (True, {CUT_TIP_MACRO}, "RAMMING_SLICER"),
        (True, {FORM_TIP_MACRO}, "CUT_FILAMENT_IN_EXTRUDER"),
        (
            True,
            {FORM_TIP_MACRO, "CUT_FILAMENT_IN_EXTRUDER"},
            "CUT_FILAMENT_IN_EXTRUDER",
        ),
    ],
)
def test_missing_tip_macro_stops_klipper(cutter, macros, old_name) -> None:
    mmu = make_mmu(cutter=cutter)
    mmu.printer.config_error = ConfigError
    mmu.printer.lookup_object = lambda name, default=None: (
        object() if name.removeprefix("gcode_macro ") in macros else default
    )
    if old_name is None:
        mmu.check_tip_macros()
        return
    with pytest.raises(ConfigError, match=f"{old_name}\\] was renamed"):
        mmu.check_tip_macros()
