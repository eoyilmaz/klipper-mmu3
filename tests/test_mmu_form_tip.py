"""Tests for the standalone ``MMU_FORM_TIP`` / ``MMU_TEST_FORM_TIP`` and ``MMU_CUT``.

They run the tip forming / cutting step of an unload on its own and leave the
filament in the extruder, no longer ``LOADED``. ``MMU_TEST_FORM_TIP`` (and its
alias ``MMU_FORM_TIP``) also change the ``_MMU_FORM_TIP_VARS`` at runtime.
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
    SlicerToolMap,
    CUT_TIP_MACRO,
    FORM_TIP_MACRO,
    FORM_TIP_VARS_MACRO,
    MMU,
    FilamentPos,
    FilamentSwitchSensorPosition,
    parse_macro_variable,
)
from extras.mmu_hh_compat import (  # noqa: E402
    ACTION_CUTTING_TIP,
    ACTION_FORMING_TIP,
    ACTION_IDLE,
)


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCmd:
    """A ``GCodeCommand`` stand-in reading from a dict of parameters."""

    def __init__(self, command: str = "MMU_FORM_TIP", **params) -> None:
        self.command = command
        self.params = {k: str(v) for k, v in params.items()}

    def get_command(self) -> str:
        return self.command

    def get_command_parameters(self) -> dict:
        return dict(self.params)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name)
        if value is None:
            return default
        value = int(value)
        if (minval is not None and value < minval) or (
            maxval is not None and value > maxval
        ):
            raise self.error(f"{name} out of range")
        return value

    def error(self, msg):
        return CommandError(msg)


class FakeMacro:
    """A ``gcode_macro`` stand-in, only its ``variables``."""

    def __init__(self, **variables) -> None:
        self.variables = variables


def form_tip_vars() -> FakeMacro:
    """A ``_MMU_FORM_TIP_VARS`` with some of the config's variables."""
    return FakeMacro(
        ramming_volume_standalone=23,
        cooling_moves=4,
        use_skinnydip=False,
        toolchange_fan_name="",
    )


def make_mmu(cutter: bool = False, hot: bool = True, macros: dict | None = None) -> MMU:
    """Build a bare MMU3 with T1 loaded whose moves only record what was done.

    Args:
        cutter: The ``enable_filament_cutter`` setting.
        hot: Whether the extruder is hot enough.
        macros: The ``gcode_macro`` objects by name, ``_MMU_FORM_TIP`` and
            ``_MMU_FORM_TIP_VARS`` by default.
    """
    if macros is None:
        macros = {FORM_TIP_MACRO: FakeMacro(), FORM_TIP_VARS_MACRO: form_tip_vars()}
    mmu = object.__new__(MMU)
    mmu.ttg_map = list(range(5))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(5))
    mmu.selected_tool = None
    mmu.macros = macros
    mmu.printer = types.SimpleNamespace(
        command_error=CommandError,
        lookup_object=lambda name, default=None: macros.get(
            name.removeprefix("gcode_macro "), default
        ),
    )
    mmu.form_tip_defaults = None
    mmu.tip_formed = False
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
    mmu.enable_steppers = lambda: None

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
    assert mmu.cmd_mmu_form_tip(FakeGCmd()) is True
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]
    assert mmu.filament_pos == FilamentPos.IN_HOTEND
    assert mmu.loaded_gate == 1
    assert mmu.action == ACTION_IDLE
    assert mmu.calls[-1] == ("steppers_off",)


def test_form_tip_rams_even_with_a_cutter() -> None:
    mmu = make_mmu(cutter=True)
    assert mmu.cmd_mmu_form_tip(FakeGCmd()) is True
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


def make_mmu_with_filament_by_hand(**kwargs) -> MMU:
    """An MMU3 with nothing loaded and a filament pushed into the extruder."""
    mmu = make_mmu(**kwargs)
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.loaded_gate = None
    mmu.in_finda = False
    scripts = mmu.scripts = []

    def run_script_from_command(script):
        scripts.append(script)
        mmu.calls.append(("macro", script.split()[0], mmu.action))
        # the final eject pulled the filament out of the extruder
        mmu.in_switch = False

    mmu.gcode.run_script_from_command = run_script_from_command
    return mmu


def test_filament_pushed_by_hand_is_formed_and_ejected() -> None:
    mmu = make_mmu_with_filament_by_hand()
    mmu.current_gate = 2
    assert mmu.cmd_mmu_form_tip(FakeGCmd("MMU_TEST_FORM_TIP")) is True
    assert mmu.scripts == ["_MMU_FORM_TIP FINAL_EJECT=1"]
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]
    # the MMU does not move
    assert ("unselect", 2) not in mmu.calls
    assert mmu.current_gate == 2
    assert mmu.filament_pos == FilamentPos.UNLOADED
    assert mmu.loaded_gate is None


@pytest.mark.parametrize("cut", [False, True])
def test_the_unload_does_not_form_or_cut_the_tip_again(cut) -> None:
    mmu = make_mmu(cutter=True)
    assert mmu.form_tip_standalone(cut=cut) is True
    assert mmu.tip_formed is True


def test_a_filament_pushed_by_hand_is_not_marked_formed() -> None:
    mmu = make_mmu_with_filament_by_hand()
    assert mmu.form_tip_standalone() is True
    assert mmu.tip_formed is False


def test_filament_pushed_by_hand_needs_a_hot_extruder() -> None:
    mmu = make_mmu_with_filament_by_hand(hot=False)
    assert mmu.form_tip_standalone() is False
    assert mmu.scripts == []


def test_filament_pushed_by_hand_is_not_cut() -> None:
    mmu = make_mmu_with_filament_by_hand(cutter=True)
    assert mmu.form_tip_standalone(cut=True) is False
    assert mmu.scripts == []


def test_filament_loaded_by_the_mmu_is_not_ejected() -> None:
    mmu = make_mmu()
    scripts = []
    mmu.gcode.run_script_from_command = scripts.append
    assert mmu.form_tip_standalone() is True
    assert scripts == ["_MMU_FORM_TIP"]


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
    assert {"MMU_FORM_TIP", "MMU_TEST_FORM_TIP", "MMU_CUT"} <= names


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


def info_messages(mmu: MMU) -> list:
    """The ``respond_info`` messages of ``mmu``."""
    return [call[1] for call in mmu.calls if call[0] == "info"]


def variables(mmu: MMU) -> dict:
    """The current ``_MMU_FORM_TIP_VARS`` variables."""
    return mmu.macros[FORM_TIP_VARS_MACRO].variables


def test_form_tip_without_parameters_does_not_list_the_variables() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_form_tip(FakeGCmd()) is True
    assert info_messages(mmu) == []


def test_test_form_tip_lists_the_variables_and_forms_the_tip() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_form_tip(FakeGCmd("MMU_TEST_FORM_TIP")) is True
    assert info_messages(mmu) == [
        "Tip forming variables:\n"
        "variable_cooling_moves: 4\n"
        "variable_ramming_volume_standalone: 23\n"
        "variable_toolchange_fan_name: ''\n"
        "variable_use_skinnydip: False"
    ]
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]
    assert mmu.filament_pos == FilamentPos.IN_HOTEND


@pytest.mark.parametrize("command", ["MMU_TEST_FORM_TIP", "MMU_FORM_TIP"])
def test_overrides_set_the_variables_and_form_the_tip(command) -> None:
    mmu = make_mmu()
    gcmd = FakeGCmd(
        command,
        COOLING_MOVES=3,
        VARIABLE_USE_SKINNYDIP="True",
        TOOLCHANGE_FAN_NAME="fan_generic fan0",
    )
    assert mmu.cmd_mmu_form_tip(gcmd) is True
    assert variables(mmu) == {
        "ramming_volume_standalone": 23,
        "cooling_moves": 3,
        "use_skinnydip": True,
        "toolchange_fan_name": "fan_generic fan0",
    }
    assert info_messages(mmu)[0].startswith(
        "Tip forming variables (changed, RESET=1 restores):\n"
    )
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]


def test_overrides_replace_the_variables_dict() -> None:
    # Klipper only pushes a status change for a new object, like
    # SET_GCODE_VARIABLE does
    mmu = make_mmu()
    before = variables(mmu)
    mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=3))
    assert variables(mmu) is not before
    assert before["cooling_moves"] == 4


def test_unknown_variable_changes_and_runs_nothing() -> None:
    mmu = make_mmu()
    gcmd = FakeGCmd("MMU_TEST_FORM_TIP", COOLING_MOVES=3, COOLING_MOVE=2)
    with pytest.raises(
        CommandError, match="Unknown tip forming variable 'cooling_move'"
    ):
        mmu.cmd_mmu_form_tip(gcmd)
    assert variables(mmu)["cooling_moves"] == 4
    assert mmu.form_tip_defaults is None
    assert macro_calls(mmu) == []


def test_run_0_only_sets_the_variables() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=2, RUN=0)) is True
    assert variables(mmu)["cooling_moves"] == 2
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.LOADED


def test_show_lists_the_variables_without_a_loaded_filament() -> None:
    mmu = make_mmu(hot=False)
    mmu.filament_pos = FilamentPos.UNLOADED
    assert mmu.cmd_mmu_form_tip(FakeGCmd(SHOW=1)) is True
    assert info_messages(mmu)[0].startswith("Tip forming variables:\n")
    assert macro_calls(mmu) == []


def test_reset_restores_the_values_before_the_first_override() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=3, RUN=0))
    mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=2, USE_SKINNYDIP="True", RUN=0))
    mmu.calls.clear()
    assert mmu.cmd_mmu_form_tip(FakeGCmd("MMU_TEST_FORM_TIP", RESET=1)) is True
    assert variables(mmu) == form_tip_vars().variables
    assert mmu.form_tip_defaults is None
    assert info_messages(mmu)[0] == (
        "Tip forming variables reset to the config values."
    )
    assert info_messages(mmu)[1].startswith("Tip forming variables:\n")
    assert macro_calls(mmu) == []


def test_reset_without_overrides_only_lists_the_variables() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_form_tip(FakeGCmd(RESET=1)) is True
    assert variables(mmu) == form_tip_vars().variables
    assert len(info_messages(mmu)) == 1
    assert macro_calls(mmu) == []


def test_without_vars_macro_the_macros_own_variables_are_used() -> None:
    form_tip = FakeMacro(cooling_moves=4)
    mmu = make_mmu(macros={FORM_TIP_MACRO: form_tip})
    assert mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=1)) is True
    assert form_tip.variables == {"cooling_moves": 1}


def test_a_plain_form_tip_macro_has_nothing_to_tune() -> None:
    mmu = make_mmu(macros={FORM_TIP_MACRO: FakeMacro()})
    assert mmu.cmd_mmu_form_tip(FakeGCmd("MMU_TEST_FORM_TIP")) is True
    assert info_messages(mmu) == ["_MMU_FORM_TIP has no variables to tune."]
    assert macro_calls(mmu) == [("_MMU_FORM_TIP", ACTION_FORMING_TIP)]
    with pytest.raises(CommandError, match="Unknown tip forming variable"):
        mmu.cmd_mmu_form_tip(FakeGCmd(COOLING_MOVES=1))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3", 3),
        ("2.5", 2.5),
        ("True", True),
        ("False", False),
        ("'quoted'", "quoted"),
        ("fan_generic fan0", "fan_generic fan0"),
        ("", ""),
    ],
)
def test_parse_macro_variable(value, expected) -> None:
    assert parse_macro_variable(value) == expected
    assert type(parse_macro_variable(value)) is type(expected)
