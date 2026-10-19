"""Tests for endless spool: gate selection, ``MMU_ENDLESS_SPOOL`` and status."""

# Standard Library Imports
import ast
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
    ENDLESS_SPOOL_ENABLED_VARIABLE,
    ENDLESS_SPOOL_GROUPS_VARIABLE,
    MMU,
    FilamentPos,
    OperationStats,
    default_endless_spool_groups,
    endless_spool_groups_from_saved,
    group_name,
    next_endless_spool_gate,
)
from extras.mmu_gate_map import (  # noqa: E402
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    GateMap,
)
from extras.mmu_hh_compat import ACTION_IDLE, MmuStatus  # noqa: E402

A, E, U = GATE_AVAILABLE, GATE_EMPTY, GATE_UNKNOWN


class CommandError(Exception):
    """Stand-in for Klipper's ``gcode.CommandError``."""


class FakeGCmd:
    """A ``GCodeCommand`` stand-in reading from a dict of parameters."""

    def __init__(self, **params) -> None:
        self.params = {k: str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = self.params.get(name)
        if value is None:
            return default
        try:
            value = int(value)
        except ValueError:
            raise self.error(f"Unable to parse '{value}' as a int") from None
        if (minval is not None and value < minval) or (
            maxval is not None and value > maxval
        ):
            raise self.error(f"{name} out of range")
        return value

    def error(self, msg):
        return CommandError(msg)


class FakeGCode:
    """Records the scripts run."""

    def __init__(self) -> None:
        self.scripts = []

    def run_script_from_command(self, script):
        self.scripts.append(script)


def make_mmu(num_tools: int = 5) -> MMU:
    """Build a bare MMU3 with endless spool at its defaults."""
    mmu = object.__new__(MMU)
    mmu.printer = types.SimpleNamespace(
        command_error=CommandError,
        lookup_object=lambda name, default=None: default,
    )
    mmu.gcode = FakeGCode()
    mmu.number_of_tools = num_tools
    mmu.gate_map = GateMap(num_tools)
    mmu.ttg_map = list(range(num_tools))
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.default_endless_spool_enabled = False
    mmu.default_endless_spool_groups = default_endless_spool_groups(num_tools)
    mmu.endless_spool_enabled = False
    mmu.endless_spool_groups = default_endless_spool_groups(num_tools)
    mmu.selected_tool = None
    mmu.save_variables = object()
    mmu.is_enabled = True
    mmu.is_paused = False
    mmu.is_homed = False
    mmu.current_gate = None
    mmu.loaded_gate = None
    mmu.filament_pos = FilamentPos.UNLOADED
    mmu.action = ACTION_IDLE
    mmu.current_operation = None
    mmu.pending_operation = None
    mmu.total_stats = OperationStats()
    mmu.job_stats = OperationStats()
    mmu.messages = []
    mmu.respond_info = mmu.messages.append
    return mmu


def saved_variables(mmu: MMU) -> dict:
    """Return the variables saved with ``SAVE_VARIABLE``, parsed like Klipper."""
    variables = {}
    for script in mmu.gcode.scripts:
        assert script.startswith("SAVE_VARIABLE VARIABLE=")
        name, value = script[len("SAVE_VARIABLE VARIABLE=") :].split(" VALUE=", 1)
        # save_variables parses VALUE with ast.literal_eval()
        value = ast.literal_eval(value)
        if isinstance(value, str):
            value = ast.literal_eval(value)
        variables[name] = value
    return variables


# ---------------------------------------------------------------------------
# gate selection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("gate", "groups", "statuses", "expected"),
    [
        # the next gate of the group, not the next gate
        (0, [0, 1, 0, 1, 0], [A, A, A, A, A], (2, [2])),
        # wraps around
        (4, [0, 1, 0, 1, 0], [A, A, A, A, A], (0, [0])),
        # skips empty gates of the group
        (0, [0, 1, 0, 1, 0], [A, A, E, A, A], (4, [2, 4])),
        # an unknown gate may have filament, it is tried
        (0, [0, 1, 0, 1, 0], [A, A, U, A, A], (2, [2])),
        # the gate that ran out is never picked again
        (1, [0, 1, 0, 1, 0], [A, A, A, E, A], (None, [3])),
        # alone in its group
        (2, [0, 1, 2, 3, 4], [A, A, A, A, A], (None, [])),
        # all gates in one group
        (3, [7, 7, 7, 7, 7], [E, A, E, A, E], (1, [4, 0, 1])),
        # every other gate of the group is empty
        (3, [7, 7, 7, 7, 7], [E, E, E, A, E], (None, [4, 0, 1, 2])),
        # an invalid gate
        (-1, [0, 0, 0], [A, A, A], (None, [])),
        (3, [0, 0, 0], [A, A, A], (None, [])),
    ],
)
def test_next_endless_spool_gate(gate, groups, statuses, expected) -> None:
    assert next_endless_spool_gate(gate, groups, statuses) == expected


def test_next_endless_spool_gate_uses_the_gate_map() -> None:
    mmu = make_mmu()
    mmu.endless_spool_groups = [0, 1, 0, 1, 0]
    mmu.gate_map.update(2, status=GATE_EMPTY)
    assert mmu.next_endless_spool_gate(0) == (4, [2, 4])


@pytest.mark.parametrize(
    ("group", "expected"), [(0, "A"), (1, "B"), (25, "Z"), (26, "26")]
)
def test_group_name(group, expected) -> None:
    assert group_name(group) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([0, 0, 1], [0, 0, 1]),
        ((5, 5, 5), [5, 5, 5]),
        (None, [9, 9, 9]),
        ([0, 0], [9, 9, 9]),
        ([0, 0, -1], [9, 9, 9]),
        ([0, 0, "1"], [9, 9, 9]),
        ([0, 0, True], [9, 9, 9]),
    ],
)
def test_endless_spool_groups_from_saved(value, expected) -> None:
    assert endless_spool_groups_from_saved(3, value, [9, 9, 9]) == expected


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def test_load_endless_spool_reads_the_saved_settings() -> None:
    mmu = make_mmu()
    mmu.load_endless_spool(
        {
            ENDLESS_SPOOL_ENABLED_VARIABLE: 1,
            ENDLESS_SPOOL_GROUPS_VARIABLE: [0, 0, 1, 1, 0],
        }
    )
    assert mmu.endless_spool_enabled is True
    assert mmu.endless_spool_groups == [0, 0, 1, 1, 0]


@pytest.mark.parametrize(
    "variables",
    [
        {},
        {ENDLESS_SPOOL_ENABLED_VARIABLE: 2, ENDLESS_SPOOL_GROUPS_VARIABLE: [0, 0]},
        {ENDLESS_SPOOL_ENABLED_VARIABLE: "yes", ENDLESS_SPOOL_GROUPS_VARIABLE: {}},
    ],
)
def test_load_endless_spool_keeps_the_config_defaults(variables) -> None:
    mmu = make_mmu()
    mmu.default_endless_spool_groups = [1, 1, 1, 2, 2]
    mmu.endless_spool_enabled = True
    mmu.load_endless_spool(variables)
    assert mmu.endless_spool_enabled is True
    assert mmu.endless_spool_groups == [1, 1, 1, 2, 2]


def test_saved_settings_load_back() -> None:
    mmu = make_mmu()
    mmu.set_endless_spool(enabled=True, groups=[3, 3, 0, 0, 3])
    other = make_mmu()
    other.load_endless_spool(saved_variables(mmu))
    assert other.endless_spool_enabled is True
    assert other.endless_spool_groups == [3, 3, 0, 0, 3]


def test_set_endless_spool_saves_only_changes() -> None:
    mmu = make_mmu()
    mmu.set_endless_spool(enabled=False, groups=[0, 1, 2, 3, 4])
    assert mmu.gcode.scripts == []


def test_set_endless_spool_without_save_variables() -> None:
    mmu = make_mmu()
    mmu.save_variables = None
    mmu.set_endless_spool(enabled=True)
    assert mmu.endless_spool_enabled is True
    assert mmu.gcode.scripts == []


# ---------------------------------------------------------------------------
# MMU_ENDLESS_SPOOL
# ---------------------------------------------------------------------------
def test_mmu_endless_spool_without_arguments_prints_the_settings() -> None:
    mmu = make_mmu()
    mmu.endless_spool_groups = [0, 1, 0, 1, 4]
    assert mmu.cmd_mmu_endless_spool(FakeGCmd()) is True
    assert mmu.messages == [
        "Endless spool is disabled.\n"
        "Endless spool groups:\n"
        "Group A: gates 0, 2\n"
        "Group B: gates 1, 3\n"
        "Group E: gates 4"
    ]
    assert mmu.gcode.scripts == []


def test_mmu_endless_spool_enable() -> None:
    mmu = make_mmu()
    assert mmu.cmd_mmu_endless_spool(FakeGCmd(ENABLE=1)) is True
    assert mmu.endless_spool_enabled is True
    assert saved_variables(mmu)[ENDLESS_SPOOL_ENABLED_VARIABLE] == 1
    assert mmu.messages[0].startswith("Endless spool is enabled.")

    assert mmu.cmd_mmu_endless_spool(FakeGCmd(ENABLE=0, QUIET=1)) is True
    assert mmu.endless_spool_enabled is False
    assert saved_variables(mmu)[ENDLESS_SPOOL_ENABLED_VARIABLE] == 0
    assert len(mmu.messages) == 1


@pytest.mark.parametrize("groups", ["1,1,0,0,1", '"1,1,0,0,1"', "1, 1, 0, 0, 1"])
def test_mmu_endless_spool_groups(groups) -> None:
    # Mainsail's tool mapping dialog sends GROUPS="..." QUIET=1
    mmu = make_mmu()
    assert mmu.cmd_mmu_endless_spool(FakeGCmd(GROUPS=groups, QUIET=1)) is True
    assert mmu.endless_spool_groups == [1, 1, 0, 0, 1]
    assert saved_variables(mmu)[ENDLESS_SPOOL_GROUPS_VARIABLE] == [1, 1, 0, 0, 1]
    assert mmu.messages == []


def test_mmu_endless_spool_groups_is_a_new_list() -> None:
    # Klipper only pushes a status change if the object changed
    mmu = make_mmu()
    before = mmu.endless_spool_groups
    mmu.cmd_mmu_endless_spool(FakeGCmd(GROUPS="0,0,0,0,0", QUIET=1))
    assert mmu.endless_spool_groups is not before


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ("0,0,0,0", "GROUPS= has 4 groups"),
        ("0,0,0,0,0,0", "GROUPS= has 6 groups"),
        ("0,0,x,0,0", "Invalid group in GROUPS=: x"),
        ("0,0,-1,0,0", "Invalid group in GROUPS=: -1"),
    ],
)
def test_mmu_endless_spool_rejects_invalid_groups(groups, message) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError, match=message):
        mmu.cmd_mmu_endless_spool(FakeGCmd(GROUPS=groups))
    assert mmu.endless_spool_groups == [0, 1, 2, 3, 4]
    assert mmu.gcode.scripts == []


def test_mmu_endless_spool_reset_goes_back_to_the_config() -> None:
    mmu = make_mmu()
    mmu.default_endless_spool_enabled = True
    mmu.default_endless_spool_groups = [0, 0, 0, 1, 1]
    mmu.endless_spool_groups = [4, 3, 2, 1, 0]
    # Mainsail's reset button sends MMU_ENDLESS_SPOOL RESET=1
    assert mmu.cmd_mmu_endless_spool(FakeGCmd(RESET=1)) is True
    assert mmu.endless_spool_enabled is True
    assert mmu.endless_spool_groups == [0, 0, 0, 1, 1]
    assert mmu.endless_spool_groups is not mmu.default_endless_spool_groups
    assert saved_variables(mmu) == {
        ENDLESS_SPOOL_ENABLED_VARIABLE: 1,
        ENDLESS_SPOOL_GROUPS_VARIABLE: [0, 0, 0, 1, 1],
    }


def test_mmu_endless_spool_is_registered() -> None:
    mmu = make_mmu()
    names = [name for name, _, _ in mmu.command_table()]
    assert "MMU_ENDLESS_SPOOL" in names


# ---------------------------------------------------------------------------
# MMU_TTG_MAP shows the groups
# ---------------------------------------------------------------------------
def test_ttg_map_shows_the_groups_when_enabled() -> None:
    mmu = make_mmu()
    mmu.endless_spool_enabled = True
    mmu.endless_spool_groups = [0, 1, 0, 1, 0]
    mmu.print_ttg_map()
    lines = mmu.messages[0].splitlines()
    assert lines[1].endswith("group A: 0 > 2 > 4")
    assert lines[3].endswith("group A: 2 > 4 > 0")
    assert lines[4].endswith("group B: 3 > 1")


def test_ttg_map_hides_the_groups_when_disabled() -> None:
    mmu = make_mmu()
    mmu.print_ttg_map()
    assert "group" not in mmu.messages[0]


# ---------------------------------------------------------------------------
# printer.mmu
# ---------------------------------------------------------------------------
def test_status_reports_endless_spool() -> None:
    mmu = make_mmu()
    mmu.filament_tracker = types.SimpleNamespace(
        is_bowden_move=False, position=lambda t: 0.0, bowden_progress=lambda t: -1
    )
    mmu.filament_switch_sensor = None
    mmu.print_stats = None
    mmu.print_state = "ready"
    mmu.spoolman_support = "off"
    mmu.print_start_detection = True
    mmu.is_handling_runout = False
    mmu.finda_triggered = False
    mmu.endless_spool_enabled = True
    mmu.endless_spool_groups = [0, 0, 1, 1, 0]
    status = MmuStatus(mmu).get_status(0.0)
    assert status["endless_spool_enabled"] == 1
    assert status["endless_spool"] == 1
    assert status["endless_spool_groups"] == [0, 0, 1, 1, 0]
    assert status["endless_spool_groups"] is not mmu.endless_spool_groups
