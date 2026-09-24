"""Tests for the MMU3 gate map (per-gate filament metadata)."""

# Third-Party Imports
import pytest

# Local Imports
from extras.mmu3_gate_map import (
    DEFAULT_SPEED_OVERRIDE,
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    NO_SPOOL,
    UNKNOWN_TEMPERATURE,
    GateInfo,
    GateMap,
    normalize_color,
)


# ---------------------------------------------------------------------------
# normalize_color
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("color", "expected"),
    [
        (None, ""),
        ("", ""),
        ("#ff0000", "FF0000"),
        ("ff0000", "FF0000"),
        ("#Ff0000Aa", "FF0000AA"),
        ("'#00ff00'", "00FF00"),
        ("  Red ", "red"),
        ("#fff", "#fff"),
    ],
)
def test_normalize_color(color, expected) -> None:
    assert normalize_color(color) == expected


# ---------------------------------------------------------------------------
# GateInfo
# ---------------------------------------------------------------------------
def test_gate_info_defaults() -> None:
    info = GateInfo()
    assert info.to_dict() == {
        "name": "",
        "material": "",
        "color": "",
        "temperature": UNKNOWN_TEMPERATURE,
        "spool_id": NO_SPOOL,
        "status": GATE_UNKNOWN,
        "speed_override": DEFAULT_SPEED_OVERRIDE,
    }


def test_gate_info_normalizes_color_on_init() -> None:
    assert GateInfo(color="#abcdef").color == "ABCDEF"


def test_gate_info_from_dict_skips_wrong_types() -> None:
    info = GateInfo.from_dict(
        {
            "name": 5,
            "material": "PETG",
            "color": "#00ff00",
            "temperature": "240",
            "spool_id": True,
            "status": 1,
        }
    )
    assert info.name == ""
    assert info.material == "PETG"
    assert info.color == "00FF00"
    assert info.temperature == UNKNOWN_TEMPERATURE
    assert info.spool_id == NO_SPOOL
    assert info.status == GATE_AVAILABLE


def test_gate_info_from_dict_non_dict_returns_defaults() -> None:
    assert GateInfo.from_dict("garbage").to_dict() == GateInfo().to_dict()


# ---------------------------------------------------------------------------
# GateMap
# ---------------------------------------------------------------------------
def test_gate_map_has_one_entry_per_gate() -> None:
    gate_map = GateMap(5)
    assert len(gate_map.gates) == 5
    for getter in (
        gate_map.names,
        gate_map.materials,
        gate_map.colors,
        gate_map.temperatures,
        gate_map.spool_ids,
        gate_map.statuses,
        gate_map.speed_overrides,
    ):
        assert len(getter()) == 5


@pytest.mark.parametrize(
    ("gate", "expected"), [(None, False), (-1, False), (0, True), (4, True), (5, False)]
)
def test_is_valid_gate(gate, expected) -> None:
    assert GateMap(5).is_valid_gate(gate) is expected


def test_update_reports_changes() -> None:
    gate_map = GateMap(5)
    assert gate_map.update(2, material="PLA", color="#ff0000", spool_id=7) is True
    assert gate_map.update(2, material="PLA", color="FF0000", spool_id=7) is False
    assert gate_map.materials()[2] == "PLA"
    assert gate_map.colors()[2] == "FF0000"
    assert gate_map.spool_ids() == [-1, -1, 7, -1, -1]


def test_update_invalid_gate_raises() -> None:
    with pytest.raises(IndexError):
        GateMap(5).update(5, material="PLA")


def test_update_unknown_field_raises() -> None:
    with pytest.raises(KeyError):
        GateMap(5).update(0, weight=1000)


def test_reset_single_gate() -> None:
    gate_map = GateMap(3)
    gate_map.update(0, material="PLA")
    gate_map.update(1, material="PETG")
    gate_map.reset(1)
    assert gate_map.materials() == ["PLA", "", ""]


def test_reset_all_gates() -> None:
    gate_map = GateMap(3)
    gate_map.update(0, material="PLA", status=GATE_EMPTY)
    gate_map.reset()
    assert gate_map.materials() == ["", "", ""]
    assert gate_map.statuses() == [GATE_UNKNOWN] * 3


def test_gates_with_spool() -> None:
    gate_map = GateMap(4)
    gate_map.update(1, spool_id=3)
    gate_map.update(3, spool_id=3)
    assert gate_map.gates_with_spool(3) == [1, 3]
    assert gate_map.gates_with_spool(9) == []


def test_round_trip() -> None:
    gate_map = GateMap(3)
    gate_map.update(
        1,
        name="Galaxy Black",
        material="PETG",
        color="#101010",
        temperature=240,
        spool_id=12,
        status=GATE_AVAILABLE,
        speed_override=80,
    )
    restored = GateMap.from_dict(3, gate_map.to_dict())
    assert restored.to_dict() == gate_map.to_dict()


def test_to_dict_uses_string_keys() -> None:
    assert list(GateMap(2).to_dict()) == ["0", "1"]


def test_from_dict_skips_out_of_range_and_malformed_entries() -> None:
    data = {
        "0": {"material": "PLA"},
        "4": {"material": "ABS"},
        "x": {"material": "ASA"},
        "1": "garbage",
    }
    gate_map = GateMap.from_dict(2, data)
    assert gate_map.materials() == ["PLA", ""]


def test_from_dict_non_dict_returns_empty_map() -> None:
    assert GateMap.from_dict(2, None).to_dict() == GateMap(2).to_dict()


def test_getters_return_new_lists() -> None:
    gate_map = GateMap(2)
    first = gate_map.colors()
    second = gate_map.colors()
    assert first == second
    assert first is not second
