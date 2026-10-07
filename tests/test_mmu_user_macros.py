"""Tests for the optional user macros called around loads and unloads.

``_MMU_PRE_UNLOAD`` / ``_MMU_POST_UNLOAD`` / ``_MMU_PRE_LOAD`` /
``_MMU_POST_LOAD`` and ``_MMU_ACTION_CHANGED`` are called only if defined, as
in Happy Hare.
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
    ACTION_CHANGED_MACRO,
    MMU,
    POST_LOAD_MACRO,
    POST_UNLOAD_MACRO,
    PRE_LOAD_MACRO,
    PRE_UNLOAD_MACRO,
    FilamentPos,
    Operation,
    OperationKind,
)
from extras.mmu_hh_compat import (  # noqa: E402
    ACTION_IDLE,
    ACTION_LOADING,
    ACTION_LOADING_EXTRUDER,
)

HOOKS = [PRE_UNLOAD_MACRO, POST_UNLOAD_MACRO, PRE_LOAD_MACRO, POST_LOAD_MACRO]


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakePrinter:
    """A ``printer`` stand-in knowing which ``gcode_macro``s are defined."""

    command_error = CommandError

    def __init__(self) -> None:
        self.macros = set()

    def lookup_object(self, name, default=None):
        prefix = "gcode_macro "
        if name.startswith(prefix) and name[len(prefix) :] in self.macros:
            return object()
        return default


def make_mmu(macros=(), failing=()) -> MMU:
    """Build a bare MMU3 whose moves and macros only record what was done.

    Args:
        macros: The names of the defined user macros.
        failing: The names of the defined user macros that raise an error.
    """
    mmu = object.__new__(MMU)
    mmu.ttg_map = list(range(5))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(5))
    mmu.selected_tool = None
    mmu.printer = FakePrinter()
    mmu.printer.macros = set(macros) | set(failing)
    mmu.is_paused = False
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_gate = None
    mmu.loaded_gate = None
    mmu.current_operation = None
    mmu.enable_no_selector_mode = False
    mmu.enable_filament_cutter = False
    mmu.force_form_tip_standalone = False
    mmu.tip_formed = False
    mmu.action = ACTION_IDLE
    mmu.calls = []
    mmu.display_status_msg = lambda msg: mmu.calls.append(("msg", msg))
    mmu.respond_debug = lambda msg: None
    mmu.respond_info = lambda msg: mmu.calls.append(("info", msg))
    mmu.validate_extruder_is_hot_enough = lambda: True
    mmu.is_filament_in_switch_sensor = lambda: False

    def run_script_from_command(script):
        mmu.calls.append(("macro", script))
        if script.split()[0] in failing:
            raise CommandError("Boom")

    mmu.gcode = types.SimpleNamespace(run_script_from_command=run_script_from_command)
    mmu.toolhead = types.SimpleNamespace(wait_moves=lambda: None)

    def select_gate(gate: int) -> bool:
        mmu.calls.append(("select", gate))
        mmu.current_gate = gate
        return True

    mmu.select_gate = select_gate

    # each fake sub-step records itself and advances/retreats filament_pos
    def step(name: str, reached: FilamentPos):
        def _step() -> bool:
            mmu.calls.append((name, reached))
            mmu.filament_pos = reached
            return True

        return _step

    mmu.load_filament_to_finda = step("to_finda", FilamentPos.AT_FINDA)
    mmu.load_filament_from_finda_to_extruder = step(
        "finda_to_extruder", FilamentPos.AT_EXTRUDER
    )
    mmu.load_filament_to_hotend = step("to_hotend", FilamentPos.LOADED)
    mmu.unload_filament_from_hotend = step("from_hotend", FilamentPos.AT_EXTRUDER)
    mmu.unload_filament_from_extruder_to_finda = step(
        "extruder_to_finda", FilamentPos.AT_FINDA
    )
    mmu.unload_filament_from_finda = step("from_finda", FilamentPos.UNLOADED)
    return mmu


def make_loaded_mmu(macros=(), failing=()) -> MMU:
    """Build a bare MMU3 with T1 loaded."""
    mmu = make_mmu(macros, failing)
    mmu.filament_pos = FilamentPos.LOADED
    mmu.current_gate = 1
    mmu.loaded_gate = 1
    return mmu


def macro_calls(mmu: MMU) -> list:
    """The macros ``mmu`` ran, in order."""
    return [call[1] for call in mmu.calls if call[0] == "macro"]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def test_load_calls_pre_and_post_load_around_the_moves() -> None:
    mmu = make_mmu(macros=HOOKS)
    assert mmu.load_gate(2) is True
    assert mmu.calls == [
        ("macro", PRE_LOAD_MACRO),
        ("select", 2),
        ("to_finda", FilamentPos.AT_FINDA),
        ("finda_to_extruder", FilamentPos.AT_EXTRUDER),
        ("to_hotend", FilamentPos.LOADED),
        ("macro", POST_LOAD_MACRO),
    ]


def test_load_without_macros_only_moves() -> None:
    mmu = make_mmu()
    assert mmu.load_gate(2) is True
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.LOADED


def test_load_when_already_loaded_calls_no_macros() -> None:
    mmu = make_loaded_mmu(macros=HOOKS)
    assert mmu.load_gate(1) is True
    assert macro_calls(mmu) == []


def test_failing_pre_load_fails_the_load_before_any_move() -> None:
    mmu = make_mmu(failing=[PRE_LOAD_MACRO])
    mmu.current_operation = Operation(OperationKind.LOAD, to_tool=2)
    assert mmu.load_gate(2) is False
    assert mmu.filament_pos == FilamentPos.UNLOADED
    assert ("select", 2) not in mmu.calls
    assert mmu.current_operation.error == f"{PRE_LOAD_MACRO} failed: Boom"


def test_failing_post_load_fails_the_load() -> None:
    mmu = make_mmu(failing=[POST_LOAD_MACRO])
    assert mmu.load_gate(2) is False
    assert mmu.filament_pos == FilamentPos.LOADED
    assert ("msg", f"{POST_LOAD_MACRO} failed: Boom") in mmu.calls


def test_failed_load_move_skips_post_load() -> None:
    mmu = make_mmu(macros=HOOKS)
    mmu.load_filament_from_finda_to_extruder = lambda: False
    assert mmu.load_gate(2) is False
    assert macro_calls(mmu) == [PRE_LOAD_MACRO]


# ---------------------------------------------------------------------------
# unload
# ---------------------------------------------------------------------------
def test_unload_calls_pre_and_post_unload_around_the_moves() -> None:
    mmu = make_loaded_mmu(macros=HOOKS)
    assert mmu.unload_gate() is True
    assert mmu.calls == [
        ("macro", PRE_UNLOAD_MACRO),
        ("from_hotend", FilamentPos.AT_EXTRUDER),
        ("extruder_to_finda", FilamentPos.AT_FINDA),
        ("from_finda", FilamentPos.UNLOADED),
        ("macro", POST_UNLOAD_MACRO),
    ]


def test_pre_unload_runs_before_the_filament_cut() -> None:
    mmu = make_loaded_mmu(macros=HOOKS)
    mmu.enable_filament_cutter = True
    mmu.is_filament_in_switch_sensor = lambda: True
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == [
        PRE_UNLOAD_MACRO,
        "_MMU_CUT_TIP",
        POST_UNLOAD_MACRO,
    ]


def make_mmu_forming_tips(**kwargs) -> MMU:
    """A loaded MMU3 with ``force_form_tip_standalone: True``."""
    mmu = make_loaded_mmu(**kwargs)
    mmu.force_form_tip_standalone = True
    mmu.is_filament_in_switch_sensor = lambda: True

    def unselect_gate() -> bool:
        mmu.calls.append(("unselect", mmu.current_gate))
        mmu.current_gate = None
        return True

    mmu.unselect_gate = unselect_gate
    return mmu


def test_force_form_tip_standalone_forms_the_tip_before_the_unload() -> None:
    mmu = make_mmu_forming_tips(macros=HOOKS)
    mmu.current_gate = 1
    assert mmu.unload_gate() is True
    assert mmu.calls == [
        ("macro", PRE_UNLOAD_MACRO),
        ("unselect", 1),
        ("macro", "_MMU_FORM_TIP"),
        ("from_hotend", FilamentPos.AT_EXTRUDER),
        ("extruder_to_finda", FilamentPos.AT_FINDA),
        ("from_finda", FilamentPos.UNLOADED),
        ("macro", POST_UNLOAD_MACRO),
    ]


def test_force_form_tip_standalone_does_not_ram_a_formed_tip_again() -> None:
    # e.g. after MMU_TEST_FORM_TIP, or a retry of a failed unload
    mmu = make_mmu_forming_tips()
    mmu.filament_pos = FilamentPos.IN_HOTEND
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []


def test_force_form_tip_standalone_retry_does_not_ram_again() -> None:
    mmu = make_mmu_forming_tips()
    mmu.unload_filament_from_hotend = lambda: False
    assert mmu.unload_gate() is False
    assert mmu.filament_pos == FilamentPos.IN_HOTEND
    assert mmu.unload_gate() is False
    assert macro_calls(mmu) == ["_MMU_FORM_TIP"]


def test_force_form_tip_standalone_cuts_with_a_cutter() -> None:
    mmu = make_mmu_forming_tips()
    mmu.enable_filament_cutter = True
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == ["_MMU_CUT_TIP"]


def test_a_tip_formed_before_the_tool_change_is_not_cut() -> None:
    # MMU_FORM_TIP in the slicer's change filament G-code, then Tn
    mmu = make_mmu_forming_tips()
    mmu.enable_filament_cutter = True
    mmu.tip_formed = True
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []
    assert mmu.tip_formed is False


def test_a_tip_formed_before_the_tool_change_is_not_rammed_again() -> None:
    mmu = make_mmu_forming_tips()
    mmu.tip_formed = True
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []


def test_a_retry_of_the_unload_does_not_cut_again() -> None:
    mmu = make_mmu_forming_tips()
    mmu.enable_filament_cutter = True
    mmu.unload_filament_from_hotend = lambda: False
    assert mmu.unload_gate() is False
    assert mmu.tip_formed is True
    assert mmu.unload_gate() is False
    assert macro_calls(mmu) == ["_MMU_CUT_TIP"]


def test_without_force_form_tip_standalone_the_slicer_rams() -> None:
    mmu = make_mmu_forming_tips()
    mmu.force_form_tip_standalone = False
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []


def test_unload_without_macros_only_moves() -> None:
    mmu = make_loaded_mmu()
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []
    assert mmu.filament_pos == FilamentPos.UNLOADED


def test_unload_when_already_unloaded_calls_no_macros() -> None:
    mmu = make_mmu(macros=HOOKS)
    mmu.loaded_gate = 1
    assert mmu.unload_gate() is True
    assert macro_calls(mmu) == []


def test_failing_pre_unload_fails_the_unload_before_any_move() -> None:
    mmu = make_loaded_mmu(failing=[PRE_UNLOAD_MACRO])
    assert mmu.unload_gate() is False
    assert mmu.filament_pos == FilamentPos.LOADED


def test_failing_post_unload_fails_the_unload() -> None:
    mmu = make_loaded_mmu(failing=[POST_UNLOAD_MACRO])
    assert mmu.unload_gate() is False
    assert mmu.filament_pos == FilamentPos.UNLOADED


# ---------------------------------------------------------------------------
# _MMU_ACTION_CHANGED
# ---------------------------------------------------------------------------
def test_action_changed_is_called_on_every_change() -> None:
    mmu = make_mmu(macros=[ACTION_CHANGED_MACRO])
    assert mmu.load_gate(2) is True
    assert macro_calls(mmu) == [
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_LOADING}' OLD_ACTION='{ACTION_IDLE}'",
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_IDLE}' OLD_ACTION='{ACTION_LOADING}'",
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_LOADING}' OLD_ACTION='{ACTION_IDLE}'",
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_IDLE}' OLD_ACTION='{ACTION_LOADING}'",
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_LOADING_EXTRUDER}' "
        f"OLD_ACTION='{ACTION_IDLE}'",
        f"{ACTION_CHANGED_MACRO} ACTION='{ACTION_IDLE}' "
        f"OLD_ACTION='{ACTION_LOADING_EXTRUDER}'",
    ]


def test_action_changed_is_not_called_when_the_action_stays_the_same() -> None:
    mmu = make_mmu(macros=[ACTION_CHANGED_MACRO])
    with mmu.running_action(ACTION_LOADING), mmu.running_action(ACTION_LOADING):
        pass
    assert len(macro_calls(mmu)) == 2


def test_action_changed_is_not_called_when_not_defined() -> None:
    mmu = make_mmu()
    with mmu.running_action(ACTION_LOADING):
        assert mmu.action == ACTION_LOADING
    assert mmu.action == ACTION_IDLE
    assert macro_calls(mmu) == []


def test_failing_action_changed_is_reported_but_does_not_fail() -> None:
    mmu = make_mmu(failing=[ACTION_CHANGED_MACRO])
    assert mmu.load_gate(2) is True
    assert mmu.action == ACTION_IDLE
    assert ("info", f"{ACTION_CHANGED_MACRO} failed: Boom") in mmu.calls


@pytest.mark.parametrize("macro", HOOKS)
def test_hook_is_not_called_when_only_other_hooks_are_defined(macro) -> None:
    others = [m for m in HOOKS if m != macro]
    mmu = make_loaded_mmu(macros=others)
    assert mmu.unload_gate() is True
    assert mmu.load_gate(2) is True
    # HOOKS is in the order of an unload followed by a load
    assert macro_calls(mmu) == others
