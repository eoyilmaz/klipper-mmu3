"""Tests for the MMU3 Spoolman active spool sync and the MMU panel commands.

Covers ``sync_active_spool`` (Moonraker's active spool follows the loaded
gate), ``MMU_GATE_MAP`` parameter handling, the live ``action`` reporting of
the load / unload planner and the ``MMU_SELECT`` safety check.
"""

# Standard Library Imports
import json
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
from extras.mmu3 import MMU3, FilamentPos  # noqa: E402
from extras.mmu3_gate_map import GATE_AVAILABLE, GateMap  # noqa: E402
from extras.mmu3_hh_compat import (  # noqa: E402
    ACTION_IDLE,
    ACTION_LOADING,
    ACTION_LOADING_EXTRUDER,
    ACTION_SELECTING,
    ACTION_UNLOADING,
    SPOOLMAN_OFF,
    SPOOLMAN_READONLY,
)


class CommandError(Exception):
    """Stand-in for Klipper's ``printer.command_error``."""


class FakeWebhooks:
    """Records remote method calls, optionally failing them."""

    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    def call_remote_method(self, method, **kwargs):
        if self.fail:
            raise CommandError(f"Remote method '{method}' not registered")
        self.calls.append((method, kwargs))


class FakePrinter:
    """A ``printer`` stand-in providing ``webhooks`` and ``command_error``."""

    command_error = CommandError

    def __init__(self) -> None:
        self.webhooks = FakeWebhooks()

    def lookup_object(self, name, default=None):
        assert name == "webhooks"
        return self.webhooks


class FakeGCmd:
    """A ``GCodeCommand`` stand-in with Klipper's parameter parsing rules."""

    def __init__(self, **params) -> None:
        self.params = {k: str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        if name not in self.params:
            return default
        value = int(self.params[name])
        if minval is not None and value < minval:
            raise self.error(f"{name} must have minimum of {minval}")
        if maxval is not None and value > maxval:
            raise self.error(f"{name} must have maximum of {maxval}")
        return value

    class error(Exception):  # noqa: N801 - mirrors GCodeCommand.error
        pass


def make_mmu(num_tools: int = 5) -> MMU3:
    """Build a bare MMU3 instance with just what these tests need."""
    mmu = object.__new__(MMU3)
    mmu.printer = FakePrinter()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.spoolman_support = SPOOLMAN_READONLY
    mmu._active_spool_id = -1
    mmu._spoolman_error_reported = False
    mmu.current_tool = None
    mmu.current_filament = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.action = ACTION_IDLE
    mmu.messages = []
    mmu.respond_info = mmu.messages.append
    mmu.respond_debug = lambda msg: None
    mmu.scripts = []
    mmu.gcode = types.SimpleNamespace(run_script_from_command=mmu.scripts.append)
    mmu.save_variables = object()
    return mmu


def load(mmu: MMU3, gate: int) -> None:
    """Pretend ``gate`` is fully loaded."""
    mmu.current_tool = gate
    mmu.current_filament = gate
    mmu.filament_pos = FilamentPos.LOADED


def spool_calls(mmu: MMU3) -> list:
    """Return the spool ids sent to Moonraker, in order."""
    return [
        kwargs["spool_id"]
        for method, kwargs in mmu.printer.webhooks.calls
        if method == "spoolman_set_active_spool"
    ]


# ---------------------------------------------------------------------------
# sync_active_spool
# ---------------------------------------------------------------------------
def test_sync_sets_spool_of_loaded_gate() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, spool_id=7)
    load(mmu, 1)
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [7]


def test_sync_only_calls_moonraker_when_spool_changes() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, spool_id=7)
    load(mmu, 1)
    mmu.sync_active_spool()
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [7]


def test_sync_clears_spool_on_unload() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, spool_id=7)
    load(mmu, 1)
    mmu.sync_active_spool()
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.current_filament = None
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [7, None]


def test_sync_clears_spool_on_startup_when_nothing_loaded() -> None:
    # _active_spool_id starts as -1, so the first sync always reaches Moonraker
    mmu = make_mmu()
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [None]


@pytest.mark.parametrize(
    ("pos", "expected"),
    [
        (FilamentPos.AT_FINDA, None),
        (FilamentPos.AT_EXTRUDER, 7),
        (FilamentPos.IN_HOTEND, 7),
        (FilamentPos.LOADED, 7),
    ],
)
def test_spool_active_once_filament_reaches_extruder(pos, expected) -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, spool_id=7)
    mmu.current_filament = 1
    mmu.filament_pos = pos
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [expected]


def test_sync_gate_without_spool_clears_active_spool() -> None:
    mmu = make_mmu()
    mmu._active_spool_id = 7
    load(mmu, 2)
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [None]


def test_sync_off_never_calls_moonraker() -> None:
    mmu = make_mmu()
    mmu.spoolman_support = SPOOLMAN_OFF
    mmu.gate_map.update(1, spool_id=7)
    load(mmu, 1)
    mmu.sync_active_spool()
    assert mmu.printer.webhooks.calls == []


def test_sync_survives_missing_spoolman_and_retries() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, spool_id=7)
    load(mmu, 1)
    mmu.printer.webhooks.fail = True
    mmu.sync_active_spool()
    mmu.sync_active_spool()
    # reported once only
    assert len(mmu.messages) == 1
    assert "[spoolman]" in mmu.messages[0]
    # the failed spool is not remembered, so the next sync retries
    mmu.printer.webhooks.fail = False
    mmu.sync_active_spool()
    assert spool_calls(mmu) == [7]


def test_quiet_sync_does_not_report_failure() -> None:
    mmu = make_mmu()
    mmu.printer.webhooks.fail = True
    mmu.sync_active_spool(quiet=True)
    assert mmu.messages == []
    assert mmu._spoolman_error_reported is False


# ---------------------------------------------------------------------------
# MMU_GATE_MAP
# ---------------------------------------------------------------------------
def test_parse_gate_map_fields() -> None:
    gcmd = FakeGCmd(
        NAME="'Bob's PLA'",
        MATERIAL='"PLA"',
        COLOR="#ff0000",
        TEMP=215,
        SPOOLID=3,
        AVAILABLE=1,
        SPEED=80,
    )
    assert MMU3.parse_gate_map_fields(gcmd) == {
        "name": "Bobs PLA",
        "material": "PLA",
        "color": "#ff0000",
        "temperature": 215,
        "spool_id": 3,
        "status": 1,
        "speed_override": 80,
    }


def test_parse_gate_map_fields_only_returns_given_fields() -> None:
    assert MMU3.parse_gate_map_fields(FakeGCmd(GATE=1, TEMP=240)) == {
        "temperature": 240
    }


def test_parse_gate_map_available_from_buffer_is_available() -> None:
    fields = MMU3.parse_gate_map_fields(FakeGCmd(AVAILABLE=2))
    assert fields == {"status": GATE_AVAILABLE}


@pytest.mark.parametrize("params", [{"SPEED": 5}, {"AVAILABLE": 3}, {"TEMP": -2}])
def test_parse_gate_map_fields_rejects_out_of_range(params) -> None:
    with pytest.raises(FakeGCmd.error):
        MMU3.parse_gate_map_fields(FakeGCmd(**params))


def test_gate_map_command_updates_saves_and_syncs() -> None:
    mmu = make_mmu()
    load(mmu, 1)
    mmu.cmd_mmu_gate_map(FakeGCmd(GATE=1, SPOOLID=7, MATERIAL="PETG", QUIET=1))
    assert mmu.gate_map[1].material == "PETG"
    assert spool_calls(mmu) == [7]
    assert len(mmu.scripts) == 1
    assert mmu.scripts[0].startswith("SAVE_VARIABLE VARIABLE=mmu3_gate_map VALUE='")
    saved = json.loads(mmu.scripts[0].split("VALUE='", 1)[1][:-1])
    assert saved["1"]["spool_id"] == 7
    assert mmu.messages == []


def test_gate_map_command_moves_spool_between_gates() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(0, spool_id=7)
    mmu.cmd_mmu_gate_map(FakeGCmd(GATE=3, SPOOLID=7, QUIET=1))
    assert mmu.gate_map.spool_ids() == [-1, -1, -1, 7, -1]


def test_gate_map_command_without_changes_does_not_save() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(0, material="PLA")
    mmu.cmd_mmu_gate_map(FakeGCmd(GATE=0, MATERIAL="PLA", QUIET=1))
    assert mmu.scripts == []


def test_gate_map_command_invalid_gate() -> None:
    with pytest.raises(FakeGCmd.error):
        make_mmu().cmd_mmu_gate_map(FakeGCmd(GATE=5, MATERIAL="PLA"))


def test_gate_map_command_reset() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(2, material="ABS", spool_id=4)
    mmu.cmd_mmu_gate_map(FakeGCmd(RESET=1))
    assert mmu.gate_map.materials() == [""] * 5
    assert mmu.gate_map.spool_ids() == [-1] * 5
    assert len(mmu.scripts) == 1


def test_gate_map_command_prints_map() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, material="PETG", spool_id=7, status=GATE_AVAILABLE)
    mmu.current_filament = 1
    mmu.cmd_mmu_gate_map(FakeGCmd())
    assert "Gate 1: PETG spool=7 [available] <- loaded" in mmu.messages[0]


def test_gate_map_command_map_param_not_supported() -> None:
    mmu = make_mmu()
    mmu.cmd_mmu_gate_map(FakeGCmd(MAP="{}"))
    assert "not supported" in mmu.messages[0]
    assert mmu.scripts == []


def test_save_gate_map_without_save_variables_is_noop() -> None:
    mmu = make_mmu()
    mmu.save_variables = None
    mmu.save_gate_map()
    assert mmu.scripts == []


def test_set_gate_status_saves_only_on_change() -> None:
    mmu = make_mmu()
    mmu.set_gate_status(2, GATE_AVAILABLE)
    mmu.set_gate_status(2, GATE_AVAILABLE)
    mmu.set_gate_status(None, GATE_AVAILABLE)
    assert mmu.gate_map.statuses()[2] == GATE_AVAILABLE
    assert len(mmu.scripts) == 1


# ---------------------------------------------------------------------------
# action reporting
# ---------------------------------------------------------------------------
def test_running_action_restores_previous_action() -> None:
    mmu = make_mmu()
    with mmu.running_action(ACTION_LOADING):
        assert mmu.action == ACTION_LOADING
        with mmu.running_action(ACTION_SELECTING):
            assert mmu.action == ACTION_SELECTING
        assert mmu.action == ACTION_LOADING
    assert mmu.action == ACTION_IDLE


def test_running_action_restores_on_error() -> None:
    mmu = make_mmu()
    with pytest.raises(RuntimeError), mmu.running_action(ACTION_LOADING):
        raise RuntimeError
    assert mmu.action == ACTION_IDLE


def make_planner_mmu(seen: list) -> MMU3:
    """Build an MMU3 whose load / unload steps record the reported action."""
    mmu = make_mmu()
    mmu.current_tool = 1

    def step(pos: FilamentPos):
        def run() -> bool:
            seen.append(mmu.action)
            mmu.filament_pos = pos
            return True

        return run

    mmu._load_path = lambda: [
        (FilamentPos.AT_FINDA, step(FilamentPos.AT_FINDA), ACTION_LOADING),
        (FilamentPos.AT_EXTRUDER, step(FilamentPos.AT_EXTRUDER), ACTION_LOADING),
        (FilamentPos.LOADED, step(FilamentPos.LOADED), ACTION_LOADING_EXTRUDER),
    ]
    mmu._unload_path = lambda: [
        (FilamentPos.AT_EXTRUDER, step(FilamentPos.AT_EXTRUDER), "Unloading Ext"),
        (FilamentPos.AT_FINDA, step(FilamentPos.AT_FINDA), ACTION_UNLOADING),
        (FilamentPos.UNLOADED, step(FilamentPos.UNLOADED), ACTION_UNLOADING),
    ]
    return mmu


def test_load_reports_action_per_step() -> None:
    seen = []
    mmu = make_planner_mmu(seen)
    assert mmu._load_toward(FilamentPos.LOADED, 1) is True
    assert seen == [ACTION_LOADING, ACTION_LOADING, ACTION_LOADING_EXTRUDER]
    assert mmu.action == ACTION_IDLE


def test_unload_reports_action_per_step() -> None:
    seen = []
    mmu = make_planner_mmu(seen)
    mmu.filament_pos = FilamentPos.LOADED
    assert mmu._unload_toward(FilamentPos.UNLOADED) is True
    assert seen == ["Unloading Ext", ACTION_UNLOADING, ACTION_UNLOADING]
    assert mmu.action == ACTION_IDLE


def test_failed_step_restores_idle() -> None:
    mmu = make_mmu()
    mmu.current_tool = 1
    mmu._load_path = lambda: [(FilamentPos.AT_FINDA, lambda: False, ACTION_LOADING)]
    assert mmu._load_toward(FilamentPos.LOADED, 1) is False
    assert mmu.action == ACTION_IDLE


# ---------------------------------------------------------------------------
# MMU_SELECT
# ---------------------------------------------------------------------------
def make_select_mmu() -> MMU3:
    """Build an MMU3 whose ``cmd_select_tool`` only records the call."""
    mmu = make_mmu()
    mmu.selected = []
    mmu.cmd_select_tool = lambda gcmd: mmu.selected.append(gcmd) or True
    return mmu


def test_select_refuses_while_another_gate_is_loaded() -> None:
    mmu = make_select_mmu()
    load(mmu, 1)
    assert mmu.cmd_mmu_select(FakeGCmd(GATE=2)) is False
    assert mmu.selected == []
    assert "unload it" in mmu.messages[0]


@pytest.mark.parametrize("params", [{"GATE": 2}, {"VALUE": 2}])
def test_select_when_unloaded(params) -> None:
    mmu = make_select_mmu()
    assert mmu.cmd_mmu_select(FakeGCmd(**params)) is True
    assert len(mmu.selected) == 1


def test_select_same_gate_while_loaded() -> None:
    mmu = make_select_mmu()
    load(mmu, 1)
    assert mmu.cmd_mmu_select(FakeGCmd(GATE=1)) is True


def test_select_bypass_not_supported() -> None:
    mmu = make_select_mmu()
    assert mmu.cmd_mmu_select(FakeGCmd(BYPASS=1)) is False
    assert mmu.selected == []
