"""Tests for the slicer tool map and ``MMU_SLICER_TOOL_MAP``."""

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
    is_matching_material,
    slicer_tool_map_warnings,
)
from extras.mmu_gate_map import (  # noqa: E402
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    GateMap,
)
from extras.mmu_hh_compat import MmuStatus  # noqa: E402
from tests.test_mmu_hh_status import make_mmu as make_status_mmu  # noqa: E402
from tests.test_mmu_ttg_map import CommandError, FakeGCmd  # noqa: E402
from tests.test_mmu_ttg_map import make_mmu as make_ttg_mmu  # noqa: E402


def make_mmu(num_tools: int = 5):
    """Build a bare MMU3 with a slicer tool map and a print state."""
    mmu = make_ttg_mmu(num_tools)
    mmu.slicer_tool_map = SlicerToolMap()
    mmu.print_state = "ready"
    return mmu


def run(mmu, **params) -> bool:
    """Run ``MMU_SLICER_TOOL_MAP`` with the given parameters."""
    return mmu.cmd_mmu_slicer_tool_map(FakeGCmd(**params))


# ---------------------------------------------------------------------------
# SlicerToolMap
# ---------------------------------------------------------------------------
def test_new_map_is_empty() -> None:
    slicer_tool_map = SlicerToolMap()
    assert slicer_tool_map.is_empty
    assert slicer_tool_map.to_dict() == {
        "tools": {},
        "referenced_tools": [],
        "initial_tool": None,
        "purge_volumes": [],
        "total_toolchanges": None,
        "skip_automap": False,
    }


def test_set_tool_records_the_filament_in_happy_hares_shape() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(
        2, color="#ff8000", material="PETG", temp=240, name="Generic PETG"
    )
    assert slicer_tool_map.to_dict()["tools"] == {
        "2": {
            "color": "FF8000",
            "material": "PETG",
            "temp": 240,
            "name": "Generic PETG",
            "in_use": True,
        }
    }
    assert slicer_tool_map.referenced_tools == [2]
    assert not slicer_tool_map.is_empty


def test_unused_tool_is_not_referenced() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(1, material="PLA", used=False)
    assert slicer_tool_map.tools[1]["in_use"] is False
    assert slicer_tool_map.referenced_tools == []


def test_referenced_tools_are_sorted_and_unique() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(3)
    slicer_tool_map.set_tool(0)
    slicer_tool_map.set_tool(3)
    assert slicer_tool_map.referenced_tools == [0, 3]


def test_initial_tool_is_a_referenced_tool() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(1)
    slicer_tool_map.set_initial_tool(4)
    assert slicer_tool_map.initial_tool == 4
    assert slicer_tool_map.referenced_tools == [1, 4]


def test_initial_tool_alone_is_not_empty() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_initial_tool(0)
    assert not slicer_tool_map.is_empty


def test_reset_forgets_everything() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(1, material="PLA")
    slicer_tool_map.set_initial_tool(1)
    slicer_tool_map.total_toolchanges = 5
    slicer_tool_map.reset()
    assert slicer_tool_map.is_empty
    assert slicer_tool_map.to_dict() == SlicerToolMap().to_dict()


def test_to_dict_builds_new_objects() -> None:
    # Klipper only pushes a status change to Moonraker for a new object
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(0, material="PLA")
    first = slicer_tool_map.to_dict()
    second = slicer_tool_map.to_dict()
    assert first == second
    assert first["tools"] is not second["tools"]
    assert first["tools"]["0"] is not second["tools"]["0"]
    assert first["referenced_tools"] is not second["referenced_tools"]


# ---------------------------------------------------------------------------
# is_matching_material
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("slicer_material", "gate_material"),
    [
        ("PLA", "PLA"),
        ("pla", "PLA"),
        (" PLA ", "PLA"),
        ("PLA+", "pla+"),
        ("PLA-CF", "PLA-CF"),
        # an unknown material always fits
        ("", "PLA"),
        ("unknown", "PETG"),
        ("PLA", ""),
    ],
)
def test_matching_materials(slicer_material: str, gate_material: str) -> None:
    assert is_matching_material(slicer_material, gate_material)


@pytest.mark.parametrize(
    ("slicer_material", "gate_material"),
    [
        ("PLA", "PETG"),
        ("PLA", "ASA+"),
        ("PLA", "TPU"),
        # similar names are different materials
        ("PLA", "PLA+"),
        ("PLA+", "PLA+HS"),
        ("PLA-CF", "PLA+"),
        ("PC", "PCTG"),
        ("PA", "PA-CF"),
    ],
)
def test_different_materials(slicer_material: str, gate_material: str) -> None:
    assert not is_matching_material(slicer_material, gate_material)


# ---------------------------------------------------------------------------
# slicer_tool_map_warnings
# ---------------------------------------------------------------------------
def make_gate_map() -> GateMap:
    """Return a 5 gate map: PLA, PETG, empty PLA, unknown ABS, no material."""
    gate_map = GateMap(5)
    gate_map.update(0, material="PLA", status=GATE_AVAILABLE)
    gate_map.update(1, material="PETG", status=GATE_AVAILABLE)
    gate_map.update(2, material="PLA", status=GATE_EMPTY)
    gate_map.update(3, material="ABS", status=GATE_UNKNOWN)
    gate_map.update(4, status=GATE_AVAILABLE)
    return gate_map


def warnings_for(tools: dict, ttg_map: None | list[int] = None) -> list[str]:
    """Return the warnings for the given ``{tool: material}`` used tools."""
    slicer_tool_map = SlicerToolMap()
    for tool, material in tools.items():
        slicer_tool_map.set_tool(tool, material=material)
    return slicer_tool_map_warnings(
        slicer_tool_map, ttg_map or list(range(5)), make_gate_map()
    )


def test_no_warnings_when_the_gates_match() -> None:
    assert warnings_for({0: "PLA", 1: "PETG"}) == []


def test_material_comparison_ignores_case_and_spaces() -> None:
    assert warnings_for({0: " pla ", 1: "petg"}) == []


def test_empty_gate_is_reported() -> None:
    assert warnings_for({2: "PLA"}) == ["T2 loads gate 2, which is empty."]


def test_different_material_is_reported() -> None:
    assert warnings_for({1: "PLA"}) == [
        "T1 is PLA in the print, but gate 1 has PETG."
    ]


def test_gate_of_unknown_status_is_checked_for_the_material_only() -> None:
    assert warnings_for({3: "ABS"}) == []
    assert warnings_for({3: "ASA"}) == ["T3 is ASA in the print, but gate 3 has ABS."]


@pytest.mark.parametrize("material", ["", "unknown", "Unknown"])
def test_unknown_slicer_material_is_not_reported(material: str) -> None:
    assert warnings_for({1: material}) == []


def test_gate_without_material_is_not_reported() -> None:
    assert warnings_for({4: "PLA"}) == []


def test_tools_are_checked_through_the_ttg_map() -> None:
    # T0 loads gate 2 (empty), T1 loads gate 0 (PLA)
    assert warnings_for({0: "PLA", 1: "PETG"}, ttg_map=[2, 0, 1, 3, 4]) == [
        "T0 loads gate 2, which is empty.",
        "T1 is PETG in the print, but gate 0 has PLA.",
    ]


def test_unused_tools_are_not_checked() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_tool(2, material="PLA", used=False)
    slicer_tool_map.set_tool(1, material="ABS", used=False)
    assert slicer_tool_map_warnings(
        slicer_tool_map, list(range(5)), make_gate_map()
    ) == []


def test_initial_tool_without_filament_is_checked_for_an_empty_gate() -> None:
    slicer_tool_map = SlicerToolMap()
    slicer_tool_map.set_initial_tool(2)
    assert slicer_tool_map_warnings(
        slicer_tool_map, list(range(5)), make_gate_map()
    ) == ["T2 loads gate 2, which is empty."]


# ---------------------------------------------------------------------------
# MMU_SLICER_TOOL_MAP
# ---------------------------------------------------------------------------
def test_command_is_registered_with_a_description() -> None:
    mmu = make_mmu()
    mmu.register_commands()
    assert "MMU_SLICER_TOOL_MAP" in mmu.gcode.handlers
    names = [name for name, _, _ in mmu.command_table()]
    assert "MMU_SLICER_TOOL_MAP" in names


def test_tool_sets_the_filament_quietly() -> None:
    mmu = make_mmu()
    assert run(
        mmu, TOOL=1, COLOR="#00ff00", MATERIAL="PETG", TEMP=240, NAME="Green PETG"
    )
    assert mmu.slicer_tool_map.tools[1] == {
        "color": "00FF00",
        "material": "PETG",
        "temp": 240,
        "name": "Green PETG",
        "in_use": True,
    }
    assert mmu.slicer_tool_map.referenced_tools == [1]
    assert mmu.messages == []


def test_tool_defaults_like_happy_hare() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0)
    assert mmu.slicer_tool_map.tools[0] == {
        "color": "",
        "material": "unknown",
        "temp": 0,
        "name": "",
        "in_use": True,
    }


def test_used_0_records_an_unused_tool() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=3, MATERIAL="PLA", USED=0)
    assert mmu.slicer_tool_map.tools[3]["in_use"] is False
    assert mmu.slicer_tool_map.referenced_tools == []


@pytest.mark.parametrize(
    "params",
    [
        {"TOOL": 5},
        {"TOOL": -1},
        {"INITIAL_TOOL": 5},
        {"TOTAL_TOOLCHANGES": -1},
        {"TOOL": 0, "USED": 2},
        {"TOOL": 0, "TEMP": -5},
    ],
)
def test_invalid_values_are_rejected(params: dict) -> None:
    mmu = make_mmu()
    with pytest.raises(CommandError):
        run(mmu, **params)
    assert mmu.slicer_tool_map.is_empty


def test_start_gcode_lines_build_the_map() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=4, MATERIAL="PLA")
    run(mmu, RESET=1, INITIAL_TOOL=1, TOTAL_TOOLCHANGES=12)
    run(mmu, TOOL=1, MATERIAL="PLA", COLOR="#FF0000")
    run(mmu, TOOL=3, MATERIAL="PETG", COLOR="#0000FF")
    assert mmu.messages == []
    assert mmu.slicer_tool_map.to_dict() == {
        "tools": {
            "1": {
                "color": "FF0000",
                "material": "PLA",
                "temp": 0,
                "name": "",
                "in_use": True,
            },
            "3": {
                "color": "0000FF",
                "material": "PETG",
                "temp": 0,
                "name": "",
                "in_use": True,
            },
        },
        "referenced_tools": [1, 3],
        "initial_tool": 1,
        "purge_volumes": [],
        "total_toolchanges": 12,
        "skip_automap": False,
    }


def test_no_arguments_prints_that_no_map_is_loaded() -> None:
    mmu = make_mmu()
    assert run(mmu)
    assert mmu.messages == ["No slicer tool map loaded."]


def test_no_arguments_prints_the_map_with_the_warnings() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(0, material="PLA", status=GATE_AVAILABLE)
    mmu.gate_map.update(2, material="PLA", status=GATE_EMPTY)
    mmu.ttg_map = [0, 2, 2, 3, 4]
    run(mmu, INITIAL_TOOL=0, TOTAL_TOOLCHANGES=3)
    run(mmu, TOOL=0, MATERIAL="PLA", COLOR="#FF0000", TEMP=215, NAME="Red PLA")
    run(mmu, TOOL=1, MATERIAL="PLA")
    run(mmu, TOOL=4, MATERIAL="ABS", USED=0)
    run(mmu)
    assert mmu.messages == [
        "Slicer tool map:\n"
        "2 color print, 3 tool changes\n"
        "T0 -> gate 0: PLA Red PLA color=FF0000 215C\n"
        "T1 -> gate 2: PLA\n"
        "Initial tool: T0\n"
        "Warnings:\n"
        "T1 loads gate 2, which is empty."
    ]


def test_single_color_print() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0, MATERIAL="PLA")
    run(mmu)
    assert mmu.messages == ["Slicer tool map:\nSingle color print\nT0 -> gate 0: PLA"]


def test_detail_also_lists_the_unused_tools() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0, MATERIAL="PLA")
    run(mmu, TOOL=1, MATERIAL="ABS", USED=0)
    run(mmu, DETAIL=1)
    assert mmu.messages == [
        "Slicer tool map:\n"
        "Single color print\n"
        "T0 -> gate 0: PLA\n"
        "T1 -> gate 1: ABS (not used)"
    ]


def test_quiet_does_not_print() -> None:
    mmu = make_mmu()
    run(mmu, QUIET=1)
    assert mmu.messages == []


def test_reset_is_quiet() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0)
    run(mmu, RESET=1)
    assert mmu.slicer_tool_map.is_empty
    assert mmu.messages == []


def test_unknown_happy_hare_parameters_are_ignored() -> None:
    mmu = make_mmu()
    assert run(mmu, TOOL=0, MATERIAL="PLA", AUTOMAP="color", PURGE_VOLUMES="1,2")
    assert mmu.slicer_tool_map.referenced_tools == [0]


# ---------------------------------------------------------------------------
# MMU_PRINT_START / MMU_PRINT_END
# ---------------------------------------------------------------------------
def test_print_start_warns_about_mismatches() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(1, material="PETG", status=GATE_EMPTY)
    mmu.gate_map.update(3, material="PETG", status=GATE_AVAILABLE)
    run(mmu, TOOL=1, MATERIAL="PETG")
    run(mmu, TOOL=3, MATERIAL="PLA")
    assert mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.print_state == "printing"
    assert mmu.messages == [
        "Warning: the print doesn't match the gates:\n"
        "T1 loads gate 1, which is empty.\n"
        "T3 is PLA in the print, but gate 3 has PETG.\n"
        "Check the gates (MMU_GATE_MAP) or remap the tools (MMU_TTG_MAP)."
    ]


def test_print_start_is_silent_when_the_gates_match() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(0, material="PLA", status=GATE_AVAILABLE)
    run(mmu, TOOL=0, MATERIAL="PLA")
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.messages == []


def test_print_start_is_silent_without_a_map() -> None:
    mmu = make_mmu()
    mmu.gate_map.update(0, status=GATE_EMPTY)
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.messages == []


def test_print_start_checks_when_print_stats_already_started_the_job() -> None:
    mmu = make_mmu()
    mmu.print_state = "printing"
    mmu.gate_map.update(0, status=GATE_EMPTY)
    run(mmu, TOOL=0)
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert len(mmu.messages) == 1
    assert "T0 loads gate 0, which is empty." in mmu.messages[0]


def test_print_start_does_not_check_while_the_mmu_is_disabled() -> None:
    mmu = make_mmu()
    mmu.is_enabled = False
    mmu.gate_map.update(0, status=GATE_EMPTY)
    run(mmu, TOOL=0)
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.messages == []


def test_print_start_keeps_the_map() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0, MATERIAL="PLA")
    mmu.cmd_mmu_print_start(FakeGCmd())
    assert mmu.slicer_tool_map.referenced_tools == [0]


def test_print_end_clears_the_map() -> None:
    mmu = make_mmu()
    run(mmu, TOOL=0, MATERIAL="PLA")
    mmu.cmd_mmu_print_start(FakeGCmd())
    mmu.cmd_mmu_print_end(FakeGCmd(STATE="cancelled"))
    assert mmu.print_state == "cancelled"
    assert mmu.slicer_tool_map.is_empty


def test_print_end_outside_a_print_keeps_the_map() -> None:
    # the start G-code sets the map before MMU_PRINT_START
    mmu = make_mmu()
    run(mmu, TOOL=0, MATERIAL="PLA")
    mmu.cmd_mmu_print_end(FakeGCmd())
    assert mmu.slicer_tool_map.referenced_tools == [0]


# ---------------------------------------------------------------------------
# printer.mmu status
# ---------------------------------------------------------------------------
def test_status_reports_the_slicer_tool_map() -> None:
    mmu = make_status_mmu()
    mmu.slicer_tool_map.set_initial_tool(0)
    mmu.slicer_tool_map.set_tool(0, color="#123456", material="PLA", temp=210)
    status = MmuStatus(mmu).get_status(0.0)
    assert status["slicer_tool_map"] == {
        "tools": {
            "0": {
                "color": "123456",
                "material": "PLA",
                "temp": 210,
                "name": "",
                "in_use": True,
            }
        },
        "referenced_tools": [0],
        "initial_tool": 0,
        "purge_volumes": [],
        "total_toolchanges": None,
        "skip_automap": False,
    }


def test_status_reports_an_empty_map() -> None:
    status = MmuStatus(make_status_mmu()).get_status(0.0)
    assert status["slicer_tool_map"]["tools"] == {}
    assert status["slicer_tool_map"]["referenced_tools"] == []
