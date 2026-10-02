"""Tests for MMU3 operation statistics.

Covers ``OperationStats`` (the counter bag) and its wiring into the
``track_operation`` decorator, which is the single choke point every
tool change / load / unload / home / cut / eject command passes through.
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
    GATE_MAP_VARIABLE,
    MMU,
    TOTAL_STATS_VARIABLE,
    Operation,
    OperationKind,
    OperationStats,
    track_operation,
)


def make_mmu() -> MMU:
    """Build a bare MMU3 instance, enough for track_operation to run."""
    mmu = object.__new__(MMU)
    mmu.ttg_map = list(range(5))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(5))
    mmu.selected_tool = None
    mmu.loaded_gate = None
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.total_stats = OperationStats()
    mmu.job_stats = OperationStats()
    mmu.save_calls = 0
    mmu.save_total_stats = lambda: setattr(mmu, "save_calls", mmu.save_calls + 1)
    mmu._pending_operation_resolved = MMU._pending_operation_resolved.__get__(mmu)
    return mmu


# ---------------------------------------------------------------------------
# OperationStats
# ---------------------------------------------------------------------------
def test_record_counts_attempts_regardless_of_kind() -> None:
    stats = OperationStats()
    stats.record(Operation(OperationKind.HOME), success=True)
    stats.record(Operation(OperationKind.HOME), success=True)
    stats.record(Operation(OperationKind.CUT), success=True)
    assert stats.attempts == {OperationKind.HOME: 2, OperationKind.CUT: 1}
    assert stats.failures == {}


def test_record_counts_failures_separately() -> None:
    stats = OperationStats()
    stats.record(Operation(OperationKind.LOAD, to_tool=1), success=False)
    stats.record(Operation(OperationKind.LOAD, to_tool=1), success=True)
    assert stats.attempts == {OperationKind.LOAD: 2}
    assert stats.failures == {OperationKind.LOAD: 1}


def test_record_tallies_successful_toolchanges_by_from_to() -> None:
    stats = OperationStats()
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=3), success=True
    )
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=3), success=True
    )
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=3, to_tool=1), success=True
    )
    assert stats.toolchanges == {(1, 3): 2, (3, 1): 1}


def test_record_does_not_tally_failed_toolchange() -> None:
    stats = OperationStats()
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=3), success=False
    )
    assert stats.toolchanges == {}


def test_record_ignores_toolchange_without_from_tool() -> None:
    # first ever load: from_tool is None (nothing was loaded before)
    stats = OperationStats()
    stats.record(Operation(OperationKind.TOOL_CHANGE, to_tool=1), success=True)
    assert stats.toolchanges == {}


def test_reset_clears_everything() -> None:
    stats = OperationStats()
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=2), success=True
    )
    stats.record(Operation(OperationKind.CUT, to_tool=2), success=False)
    stats.reset()
    assert stats.attempts == {}
    assert stats.failures == {}
    assert stats.toolchanges == {}


def test_to_dict_from_dict_roundtrip() -> None:
    stats = OperationStats()
    stats.record(
        Operation(OperationKind.TOOL_CHANGE, from_tool=1, to_tool=2), success=True
    )
    stats.record(Operation(OperationKind.CUT, to_tool=2), success=False)

    rebuilt = OperationStats.from_dict(stats.to_dict())

    assert rebuilt.attempts == stats.attempts
    assert rebuilt.failures == stats.failures
    assert rebuilt.toolchanges == stats.toolchanges


def test_from_dict_ignores_malformed_entries() -> None:
    data = {
        "attempts": {"tool_change": 5, "not_a_kind": 9, "cut": "nope"},
        "failures": {"tool_change": 1},
        "toolchanges": {"1->2": 3, "garbage": 4, "1->x": 2},
    }
    stats = OperationStats.from_dict(data)
    assert stats.attempts == {OperationKind.TOOL_CHANGE: 5}
    assert stats.failures == {OperationKind.TOOL_CHANGE: 1}
    assert stats.toolchanges == {(1, 2): 3}


def test_from_dict_empty_is_safe() -> None:
    stats = OperationStats.from_dict({})
    assert stats.attempts == {}
    assert stats.failures == {}
    assert stats.toolchanges == {}


# ---------------------------------------------------------------------------
# track_operation wiring
# ---------------------------------------------------------------------------
def test_track_operation_records_success_in_both_scopes() -> None:
    mmu = make_mmu()
    mmu.loaded_gate = 1

    @track_operation(OperationKind.TOOL_CHANGE)
    def cmd_tx(self, gcmd, tool_id=0):
        self.loaded_gate = tool_id
        return True

    assert cmd_tx(mmu, None, tool_id=2) is True
    assert mmu.total_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    assert mmu.total_stats.toolchanges == {(1, 2): 1}
    assert mmu.job_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    assert mmu.job_stats.toolchanges == {(1, 2): 1}
    assert mmu.current_operation is None
    assert mmu.save_calls == 1


def test_track_operation_records_failure_on_false_return() -> None:
    mmu = make_mmu()
    mmu.loaded_gate = 1

    @track_operation(OperationKind.TOOL_CHANGE)
    def cmd_tx(self, gcmd, tool_id=0):
        return False

    assert cmd_tx(mmu, None, tool_id=2) is False
    assert mmu.total_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    assert mmu.total_stats.failures == {OperationKind.TOOL_CHANGE: 1}
    assert mmu.total_stats.toolchanges == {}
    # a failure keeps current_operation set, for auto_pause to promote
    assert mmu.current_operation is not None


def test_track_operation_records_failure_when_command_raises() -> None:
    mmu = make_mmu()
    mmu.loaded_gate = 1

    @track_operation(OperationKind.TOOL_CHANGE)
    def cmd_tx(self, gcmd, tool_id=0):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cmd_tx(mmu, None, tool_id=2)

    assert mmu.total_stats.attempts == {OperationKind.TOOL_CHANGE: 1}
    assert mmu.total_stats.failures == {OperationKind.TOOL_CHANGE: 1}
    # still set, exactly as it was before stats were added - auto_pause
    # (the outer decorator, not exercised here) relies on this to promote
    # it to pending_operation.
    assert mmu.current_operation is not None


def test_track_operation_a_broken_save_total_stats_does_not_break_the_command() -> None:
    mmu = make_mmu()
    mmu.save_total_stats = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))

    @track_operation(OperationKind.HOME)
    def cmd_home(self, gcmd):
        return True

    assert cmd_home(mmu, None) is True


# ---------------------------------------------------------------------------
# save_variables
# ---------------------------------------------------------------------------
def make_saved_mmu(variables: dict) -> MMU:
    """Build a bare MMU3 instance with the given saved variables."""
    mmu = object.__new__(MMU)
    mmu.ttg_map = list(range(5))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = list(range(5))
    mmu.selected_tool = None
    mmu.save_variables = types.SimpleNamespace(allVariables=variables)
    return mmu


def test_load_variable_reads_the_saved_value() -> None:
    mmu = make_saved_mmu({"mmu_total_stats": {"attempts": {"home": 1}}})
    assert mmu.load_variable(TOTAL_STATS_VARIABLE) == {"attempts": {"home": 1}}


@pytest.mark.parametrize(
    ("name", "legacy_name"),
    [
        (TOTAL_STATS_VARIABLE, "mmu3_total_stats"),
        (GATE_MAP_VARIABLE, "mmu3_gate_map"),
    ],
)
def test_load_variable_falls_back_to_the_legacy_name(name, legacy_name) -> None:
    mmu = make_saved_mmu({legacy_name: {"old": 1}})
    assert mmu.load_variable(name) == {"old": 1}


def test_load_variable_prefers_the_new_name() -> None:
    mmu = make_saved_mmu({"mmu_gate_map": {"new": 1}, "mmu3_gate_map": {"old": 1}})
    assert mmu.load_variable(GATE_MAP_VARIABLE) == {"new": 1}


def test_load_variable_defaults_to_an_empty_dict() -> None:
    assert make_saved_mmu({}).load_variable(GATE_MAP_VARIABLE) == {}
