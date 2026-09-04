"""Tests for the MMU3 recovery state machine.

Covers the pure-logic pieces added for issue #41: the ``Operation`` record,
the ``FilamentPos`` ordering, the ``move_filament_to`` planner step selection
and ``assess_filament_pos`` sensor reconciliation.
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
)


def make_mmu() -> MMU3:
    """Build a bare MMU3 instance without running __init__ or touching Klipper."""
    mmu = object.__new__(MMU3)
    mmu.is_paused = False
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_tool = None
    mmu.current_filament = None
    mmu.enable_no_selector_mode = False
    mmu.calls = []
    mmu.display_status_msg = lambda msg: mmu.calls.append(("msg", msg))
    mmu.respond_debug = lambda msg: None
    mmu.respond_info = lambda msg: None

    def select_tool(tool_id: int) -> bool:
        mmu.calls.append(("select", tool_id))
        mmu.current_tool = tool_id
        return True

    mmu.select_tool = select_tool

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


# ---------------------------------------------------------------------------
# FilamentPos
# ---------------------------------------------------------------------------
def test_filament_pos_is_ordered() -> None:
    assert FilamentPos.UNLOADED < FilamentPos.AT_FINDA < FilamentPos.AT_EXTRUDER
    assert FilamentPos.AT_EXTRUDER < FilamentPos.IN_HOTEND < FilamentPos.LOADED
    assert max(FilamentPos.AT_FINDA, FilamentPos.LOADED) is FilamentPos.LOADED


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------
def test_operation_describe_tool_change_with_stage() -> None:
    op = Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=2)
    op.filament_pos_at_fail = FilamentPos.AT_EXTRUDER
    assert op.describe() == (
        "Tool change T1 => T2 - stopped with filament at the extruder"
    )


def test_operation_describe_first_load_has_no_from_tool() -> None:
    op = Operation(OperationKind.TOOL_CHANGE, from_tool=None, to_tool=3)
    assert op.describe() == "Load T3"


def test_operation_describe_includes_error() -> None:
    op = Operation(OperationKind.UNLOAD, from_tool=4)
    op.error = "FINDA still triggered"
    assert op.describe() == "Unload T4 (FINDA still triggered)"


@pytest.mark.parametrize(
    "kind,to_tool,expected",
    [
        (OperationKind.TOOL_CHANGE, 2, FilamentPos.LOADED),
        (OperationKind.LOAD, 0, FilamentPos.LOADED),
        (OperationKind.TOOL_CHANGE, None, FilamentPos.UNLOADED),
        (OperationKind.UNLOAD, None, FilamentPos.UNLOADED),
        (OperationKind.EJECT, None, FilamentPos.UNLOADED),
        (OperationKind.HOME, None, FilamentPos.UNLOADED),
    ],
)
def test_operation_target_pos(kind, to_tool, expected):
    assert Operation(kind, to_tool=to_tool).target_pos is expected


# ---------------------------------------------------------------------------
# move_filament_to planner
# ---------------------------------------------------------------------------
def test_full_load_from_unloaded_runs_every_step() -> None:
    mmu = make_mmu()
    assert mmu.move_filament_to(FilamentPos.LOADED, tool_id=2) is True
    assert mmu.calls == [
        ("select", 2),
        ("to_finda", FilamentPos.AT_FINDA),
        ("finda_to_extruder", FilamentPos.AT_EXTRUDER),
        ("to_hotend", FilamentPos.LOADED),
    ]


def test_load_resumes_from_broken_step_without_repeating_bowden() -> None:
    mmu = make_mmu()
    mmu.current_tool = 2
    mmu.filament_pos = FilamentPos.AT_EXTRUDER  # bowden move already done
    assert mmu.move_filament_to(FilamentPos.LOADED, tool_id=2) is True
    # only the hotend step runs, no re-select and no bowden move
    assert mmu.calls == [("to_hotend", FilamentPos.LOADED)]


def test_partial_load_stops_at_target() -> None:
    mmu = make_mmu()
    assert mmu.move_filament_to(FilamentPos.AT_EXTRUDER, tool_id=1) is True
    assert [c[0] for c in mmu.calls] == ["select", "to_finda", "finda_to_extruder"]


def test_full_unload_from_loaded_runs_every_step() -> None:
    mmu = make_mmu()
    mmu.filament_pos = FilamentPos.LOADED
    mmu.current_filament = 3
    assert mmu.move_filament_to(FilamentPos.UNLOADED) is True
    assert [c[0] for c in mmu.calls] == [
        "from_hotend",
        "extruder_to_finda",
        "from_finda",
    ]


def test_unload_resumes_from_finda_only() -> None:
    mmu = make_mmu()
    mmu.filament_pos = FilamentPos.AT_FINDA
    assert mmu.move_filament_to(FilamentPos.UNLOADED) is True
    assert [c[0] for c in mmu.calls] == ["from_finda"]


def test_move_is_a_noop_when_already_at_target() -> None:
    mmu = make_mmu()
    mmu.filament_pos = FilamentPos.LOADED
    assert mmu.move_filament_to(FilamentPos.LOADED, tool_id=1) is True
    assert mmu.calls == []


def test_move_fails_fast_when_paused() -> None:
    mmu = make_mmu()
    mmu.is_paused = True
    assert mmu.move_filament_to(FilamentPos.LOADED, tool_id=1) is False
    assert mmu.calls == []


def test_load_needs_a_tool() -> None:
    mmu = make_mmu()
    assert mmu.move_filament_to(FilamentPos.LOADED) is False
    assert ("msg", "Cannot load, no tool selected!") in mmu.calls


def test_load_step_failure_aborts_planner() -> None:
    mmu = make_mmu()

    def failing() -> bool:
        mmu.calls.append(("finda_to_extruder", "FAIL"))
        return False

    mmu.load_filament_from_finda_to_extruder = failing
    assert mmu.move_filament_to(FilamentPos.LOADED, tool_id=1) is False
    assert [c[0] for c in mmu.calls] == ["select", "to_finda", "finda_to_extruder"]


# ---------------------------------------------------------------------------
# assess_filament_pos
# ---------------------------------------------------------------------------
def assess_mmu(
    in_finda: bool,
    in_switch: bool,
    tracked: FilamentPos,
    sensor_position: FilamentSwitchSensorPosition = (
        FilamentSwitchSensorPosition.OnGears
    ),
) -> MMU3:
    mmu = object.__new__(MMU3)
    mmu.filament_pos = tracked
    mmu.current_tool = 1
    mmu.current_filament = 1
    mmu.filament_switch_sensor_position = sensor_position
    mmu.respond_debug = lambda msg: None
    mmu.is_filament_in_finda = lambda: in_finda
    mmu.is_filament_in_switch_sensor = lambda: in_switch
    return mmu


def test_assess_no_sensors_means_unloaded() -> None:
    mmu = assess_mmu(False, False, FilamentPos.LOADED)
    assert mmu.assess_filament_pos() is FilamentPos.UNLOADED
    assert mmu.current_filament is None


def test_assess_finda_only_means_at_finda() -> None:
    mmu = assess_mmu(True, False, FilamentPos.LOADED)
    assert mmu.assess_filament_pos() is FilamentPos.AT_FINDA


def test_assess_both_sensors_keeps_tracked_progress_clamped() -> None:
    mmu = assess_mmu(True, True, FilamentPos.LOADED)
    assert mmu.assess_filament_pos() is FilamentPos.LOADED

    mmu = assess_mmu(True, True, FilamentPos.AT_FINDA)
    assert mmu.assess_filament_pos() is FilamentPos.AT_EXTRUDER


def test_assess_recovers_current_filament_from_selected_tool() -> None:
    mmu = assess_mmu(True, False, FilamentPos.AT_FINDA)
    mmu.current_filament = None
    mmu.assess_filament_pos()
    assert mmu.current_filament == 1


# --- FilamentSwitchSensorPosition-specific inference ------------------------
@pytest.mark.parametrize(
    "sensor_position",
    [FilamentSwitchSensorPosition.PreGears, FilamentSwitchSensorPosition.OnGears],
)
def test_assess_pre_and_on_gears_cannot_confirm_hotend(sensor_position) -> None:
    # switch triggered, but a pre/on-gears sensor cannot tell IN_HOTEND from
    # LOADED - a stale UNLOADED is only bumped up to AT_EXTRUDER
    mmu = assess_mmu(True, True, FilamentPos.UNLOADED, sensor_position)
    assert mmu.assess_filament_pos() is FilamentPos.AT_EXTRUDER
    # ... and real forward progress is preserved
    mmu = assess_mmu(True, True, FilamentPos.LOADED, sensor_position)
    assert mmu.assess_filament_pos() is FilamentPos.LOADED


def test_assess_post_gears_confirms_past_the_gears() -> None:
    # switch after the gears: triggered proves the filament passed through them
    mmu = assess_mmu(
        True, True, FilamentPos.UNLOADED, FilamentSwitchSensorPosition.PostGears
    )
    assert mmu.assess_filament_pos() is FilamentPos.IN_HOTEND


def test_assess_post_gears_not_triggered_allows_tip_in_gears() -> None:
    # not past the gears yet, but the tip may be sitting in them
    mmu = assess_mmu(
        True, False, FilamentPos.AT_EXTRUDER, FilamentSwitchSensorPosition.PostGears
    )
    assert mmu.assess_filament_pos() is FilamentPos.AT_EXTRUDER


@pytest.mark.parametrize(
    "sensor_position",
    [
        FilamentSwitchSensorPosition.PreGears,
        FilamentSwitchSensorPosition.OnGears,
        FilamentSwitchSensorPosition.PostGears,
    ],
)
def test_assess_switch_clear_rules_out_stale_hotend_progress(sensor_position) -> None:
    # tracked says IN_HOTEND but the switch is clear and only FINDA sees
    # filament - every sensor position agrees the tip is no further than the
    # gears, and pre/on-gears pull it all the way back to AT_FINDA
    mmu = assess_mmu(True, False, FilamentPos.IN_HOTEND, sensor_position)
    result = mmu.assess_filament_pos()
    if sensor_position == FilamentSwitchSensorPosition.PostGears:
        assert result is FilamentPos.AT_EXTRUDER
    else:
        assert result is FilamentPos.AT_FINDA
