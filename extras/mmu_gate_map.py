"""Per-gate filament metadata for the MMU3.

Holds what is loaded in each MMU3 gate (filament name, material, color,
temperature, Spoolman spool id, availability), in the shape Happy Hare exposes
it (``gate_filament_name``, ``gate_material``, ``gate_color``, ...) so the
Mainsail / Fluidd MMU panels can render it.

Pure Python with no Klipper dependencies, so it can be unit tested standalone.
"""

# Standard Library Imports
from __future__ import annotations

import contextlib
import re

GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1

NO_SPOOL = -1
UNKNOWN_TEMPERATURE = -1
DEFAULT_SPEED_OVERRIDE = 100

HEX_COLOR = re.compile(r"^#?([0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?)$")


def normalize_color(color: None | str) -> str:
    """Normalize a color to Happy Hare's format.

    Hex colors are returned uppercase without the leading ``#``
    (``RRGGBB`` or ``RRGGBBAA``), named colors (e.g. ``red``) are returned
    lowercase and anything empty becomes ``""``.

    Args:
        color (None | str): The color as given by the user or Mainsail.

    Returns:
        str: The normalized color.
    """
    if not color:
        return ""
    color = str(color).strip().strip("'\"")
    match = HEX_COLOR.match(color)
    if match:
        return match.group(1).upper()
    return color.lower()


class GateInfo:
    """The filament metadata of a single gate.

    Args:
        name (str): The filament name.
        material (str): The filament material, e.g. ``PLA``.
        color (str): The filament color, see :func:`normalize_color`.
        temperature (int): The filament print temperature, -1 if unknown.
        spool_id (int): The Spoolman spool id, -1 if no spool is assigned.
        status (int): -1 unknown, 0 empty, 1 available.
        speed_override (int): The load/unload speed override percentage.
    """

    FIELDS = (
        "name",
        "material",
        "color",
        "temperature",
        "spool_id",
        "status",
        "speed_override",
    )

    def __init__(
        self,
        name: str = "",
        material: str = "",
        color: str = "",
        temperature: int = UNKNOWN_TEMPERATURE,
        spool_id: int = NO_SPOOL,
        status: int = GATE_UNKNOWN,
        speed_override: int = DEFAULT_SPEED_OVERRIDE,
    ) -> None:
        self.name = name
        self.material = material
        self.color = normalize_color(color)
        self.temperature = temperature
        self.spool_id = spool_id
        self.status = status
        self.speed_override = speed_override

    def to_dict(self) -> dict:
        """Return a JSON/``save_variables``-friendly snapshot.

        Returns:
            dict: The gate fields.
        """
        return {field: getattr(self, field) for field in self.FIELDS}

    @classmethod
    def from_dict(cls, data: dict) -> GateInfo:
        """Rebuild a gate from a snapshot produced by :meth:`to_dict`.

        Fields with the wrong type are skipped so a corrupt ``save_variables``
        entry can never prevent startup.

        Args:
            data (dict): The snapshot.

        Returns:
            GateInfo: The rebuilt gate.
        """
        gate = cls()
        if not isinstance(data, dict):
            return gate
        for field in ("name", "material"):
            if isinstance(data.get(field), str):
                setattr(gate, field, data[field])
        if isinstance(data.get("color"), str):
            gate.color = normalize_color(data["color"])
        for field in ("temperature", "spool_id", "status", "speed_override"):
            value = data.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(gate, field, value)
        return gate


class GateMap:
    """The filament metadata of every gate of the MMU3.

    Args:
        num_gates (int): The number of gates.
    """

    def __init__(self, num_gates: int) -> None:
        self.num_gates = num_gates
        self.gates: list[GateInfo] = [GateInfo() for _ in range(num_gates)]

    def __getitem__(self, gate: int) -> GateInfo:
        """Return the gate with the given index.

        Args:
            gate (int): The gate index.

        Returns:
            GateInfo: The gate.
        """
        return self.gates[gate]

    def is_valid_gate(self, gate: None | int) -> bool:
        """Return whether the given gate index exists.

        Args:
            gate (None | int): The gate index.

        Returns:
            bool: True if the gate exists.
        """
        return gate is not None and 0 <= gate < self.num_gates

    def update(self, gate: int, **fields) -> bool:
        """Update the given fields of a gate.

        Args:
            gate (int): The gate index.
            **fields: Any of :attr:`GateInfo.FIELDS`.

        Raises:
            IndexError: If the gate does not exist.
            KeyError: If an unknown field is given.

        Returns:
            bool: True if anything changed.
        """
        if not self.is_valid_gate(gate):
            raise IndexError(f"Invalid gate: {gate}")
        info = self.gates[gate]
        changed = False
        for field, value in fields.items():
            if field not in GateInfo.FIELDS:
                raise KeyError(f"Unknown gate field: {field}")
            if field == "color":
                value = normalize_color(value)
            if getattr(info, field) != value:
                setattr(info, field, value)
                changed = True
        return changed

    def reset(self, gate: None | int = None) -> None:
        """Reset one gate, or all gates, to the defaults.

        Args:
            gate (None | int): The gate to reset, None resets all gates.
        """
        if gate is None:
            self.gates = [GateInfo() for _ in range(self.num_gates)]
        else:
            self.gates[gate] = GateInfo()

    def gates_with_spool(self, spool_id: int) -> list[int]:
        """Return the gates the given spool is assigned to.

        Args:
            spool_id (int): The Spoolman spool id.

        Returns:
            list[int]: The gate indices.
        """
        return [i for i, info in enumerate(self.gates) if info.spool_id == spool_id]

    def to_dict(self) -> dict:
        """Return a JSON/``save_variables``-friendly snapshot.

        Returns:
            dict: The gates keyed by their index as a string.
        """
        return {str(i): info.to_dict() for i, info in enumerate(self.gates)}

    @classmethod
    def from_dict(cls, num_gates: int, data: dict) -> GateMap:
        """Rebuild a gate map from a snapshot produced by :meth:`to_dict`.

        Gates outside ``range(num_gates)`` (e.g. after switching from a 12x to a
        5x setup) and malformed entries are skipped.

        Args:
            num_gates (int): The number of gates.
            data (dict): The snapshot.

        Returns:
            GateMap: The rebuilt gate map.
        """
        gate_map = cls(num_gates)
        if not isinstance(data, dict):
            return gate_map
        for key, value in data.items():
            with contextlib.suppress(ValueError, TypeError):
                gate = int(key)
                if gate_map.is_valid_gate(gate):
                    gate_map.gates[gate] = GateInfo.from_dict(value)
        return gate_map

    # Happy Hare shaped arrays. Each call returns a new list so Klipper's
    # status change detection sees a new object and pushes the update.
    def names(self) -> list[str]:
        """Return the filament name of every gate."""
        return [info.name for info in self.gates]

    def materials(self) -> list[str]:
        """Return the filament material of every gate."""
        return [info.material for info in self.gates]

    def colors(self) -> list[str]:
        """Return the filament color of every gate."""
        return [info.color for info in self.gates]

    def temperatures(self) -> list[int]:
        """Return the filament temperature of every gate."""
        return [info.temperature for info in self.gates]

    def spool_ids(self) -> list[int]:
        """Return the Spoolman spool id of every gate."""
        return [info.spool_id for info in self.gates]

    def statuses(self) -> list[int]:
        """Return the availability status of every gate."""
        return [info.status for info in self.gates]

    def speed_overrides(self) -> list[int]:
        """Return the speed override of every gate."""
        return [info.speed_override for info in self.gates]
