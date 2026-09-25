"""Happy Hare compatible status objects for the MMU3.

Mainsail (v2.15+) and Fluidd (v1.34+) ship an MMU panel that is shown whenever
Klipper has an object named ``mmu`` (and optionally ``mmu_machine``) with Happy
Hare's status fields. The classes here translate the MMU3 state into that
shape, so the MMU3 gets the native panel without a Mainsail / Fluidd fork.

Field names, constants and action strings mirror Mainsail's
``src/components/mixins/mmu.ts``.
"""

# Standard Library Imports
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from extras.mmu3 import MMU3


TOOL_GATE_UNKNOWN = -1

FILAMENT_POS_UNKNOWN = -1
FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_HOMED_GATE = 1
FILAMENT_POS_HOMED_ENTRY = 5
FILAMENT_POS_EXTRUDER_ENTRY = 7
FILAMENT_POS_IN_EXTRUDER = 9
FILAMENT_POS_LOADED = 10

DIRECTION_LOAD = 1
DIRECTION_UNKNOWN = 0
DIRECTION_UNLOAD = -1

ACTION_IDLE = "Idle"
ACTION_LOADING = "Loading"
ACTION_LOADING_EXTRUDER = "Loading Ext"
ACTION_UNLOADING = "Unloading"
ACTION_UNLOADING_EXTRUDER = "Unloading Ext"
ACTION_FORMING_TIP = "Forming Tip"
ACTION_CUTTING_TIP = "Cutting Tip"
ACTION_HEATING = "Heating"
ACTION_CHECKING = "Checking"
ACTION_HOMING = "Homing"
ACTION_SELECTING = "Selecting"
ACTION_CUTTING_FILAMENT = "Cutting Filament"

LOAD_ACTIONS = (ACTION_LOADING, ACTION_LOADING_EXTRUDER)
UNLOAD_ACTIONS = (ACTION_UNLOADING, ACTION_UNLOADING_EXTRUDER, ACTION_FORMING_TIP)

SPOOLMAN_OFF = "off"
SPOOLMAN_READONLY = "readonly"
SPOOLMAN_SUPPORT_VALUES = (SPOOLMAN_OFF, SPOOLMAN_READONLY)

# Klipper print_stats.state -> Happy Hare print_state
PRINT_STATE_MAP = {
    "standby": "ready",
    "printing": "printing",
    "paused": "paused",
    "complete": "complete",
    "cancelled": "cancelled",
    "error": "error",
}


def _or_unknown(value: None | int) -> int:
    """Return the value, or -1 (unknown) if it is None.

    Args:
        value (None | int): A tool or gate index.

    Returns:
        int: The value or -1.
    """
    return TOOL_GATE_UNKNOWN if value is None else value


class MmuStatus:
    """The ``mmu`` status object, in Happy Hare's shape.

    Args:
        mmu3 (MMU3): The MMU3 instance to report the status of.
    """

    def __init__(self, mmu3: MMU3) -> None:
        self.mmu3 = mmu3

    @property
    def switch_sensor_key(self) -> str:
        """Return the Happy Hare sensor name of the filament switch sensor.

        A sensor after the extruder gears is a toolhead sensor, one at or
        before the gears is an extruder (entry) sensor.

        Returns:
            str: ``toolhead`` or ``extruder``.
        """
        # imported here, mmu3 imports this module
        from extras.mmu3 import FilamentSwitchSensorPosition

        if (
            self.mmu3.filament_switch_sensor_position
            == FilamentSwitchSensorPosition.PostGears
        ):
            return "toolhead"
        return "extruder"

    def filament_pos(self) -> int:
        """Return the filament position as a Happy Hare ``FILAMENT_POS_*`` value.

        Returns:
            int: The filament position.
        """
        name = self.mmu3.filament_pos.name
        if name == "AT_EXTRUDER":
            # homed at the extruder entry sensor, or sitting in the gears
            # before the toolhead sensor
            if self.switch_sensor_key == "extruder":
                return FILAMENT_POS_HOMED_ENTRY
            return FILAMENT_POS_EXTRUDER_ENTRY
        return {
            "UNLOADED": FILAMENT_POS_UNLOADED,
            "AT_FINDA": FILAMENT_POS_HOMED_GATE,
            "IN_HOTEND": FILAMENT_POS_IN_EXTRUDER,
            "LOADED": FILAMENT_POS_LOADED,
        }.get(name, FILAMENT_POS_UNKNOWN)

    def filament(self) -> str:
        """Return the Happy Hare ``filament`` state.

        Returns:
            str: ``Loaded``, ``Unloaded`` or ``Unknown`` (partially loaded).
        """
        name = self.mmu3.filament_pos.name
        if name == "LOADED":
            return "Loaded"
        if name == "UNLOADED":
            return "Unloaded"
        return "Unknown"

    def gate(self) -> int:
        """Return the selected gate, falling back to the loaded one.

        Returns:
            int: The gate index or -1.
        """
        mmu3 = self.mmu3
        if mmu3.current_tool is not None:
            return mmu3.current_tool
        return _or_unknown(mmu3.current_filament)

    def tool(self) -> int:
        """Return the loaded tool, falling back to the selected one.

        Returns:
            int: The tool index or -1.
        """
        mmu3 = self.mmu3
        if mmu3.current_filament is not None:
            return mmu3.current_filament
        return _or_unknown(mmu3.current_tool)

    def print_state(self, eventtime: float) -> str:
        """Return the Happy Hare ``print_state``.

        Returns:
            str: e.g. ``ready``, ``printing``, ``pause_locked``.
        """
        mmu3 = self.mmu3
        if mmu3.is_paused:
            # the MMU paused itself and waits for the operator
            return "pause_locked"
        if mmu3.print_stats is None:
            return "ready"
        state = mmu3.print_stats.get_status(eventtime).get("state", "standby")
        return PRINT_STATE_MAP.get(state, "ready")

    def filament_direction(self) -> int:
        """Return the direction the filament is currently moving.

        Returns:
            int: 1 loading, -1 unloading, 0 idle.
        """
        action = self.mmu3.action
        if action in LOAD_ACTIONS:
            return DIRECTION_LOAD
        if action in UNLOAD_ACTIONS:
            return DIRECTION_UNLOAD
        return DIRECTION_UNKNOWN

    def sensors(self) -> dict:
        """Return the sensor states.

        FINDA is the gate sensor. It is not queried here - querying an MCU
        endstop pauses the reactor, which ``get_status`` must never do - the
        tracked filament position (kept in sync with FINDA by
        ``assess_filament_pos``) is reported instead.

        Returns:
            dict: Happy Hare sensor name -> triggered.
        """
        mmu3 = self.mmu3
        sensors = {"mmu_gate": mmu3.filament_pos.name != "UNLOADED"}
        if mmu3.filament_switch_sensor is not None:
            sensors[self.switch_sensor_key] = bool(
                mmu3.filament_switch_sensor.get_status(None)["filament_detected"]
            )
        return sensors

    def num_toolchanges(self) -> int:
        """Return the successful tool changes in the current job.

        Returns:
            int: The count.
        """
        # imported here, mmu3 imports this module
        from extras.mmu3 import OperationKind

        stats = self.mmu3.job_stats
        return stats.attempts.get(OperationKind.TOOL_CHANGE, 0) - stats.failures.get(
            OperationKind.TOOL_CHANGE, 0
        )

    def active_filament(self) -> dict:
        """Return the metadata of the gate that is currently loaded.

        Returns:
            dict: The filament name, material, color, spool id and temperature.
        """
        gate_map = self.mmu3.gate_map
        gate = self.mmu3.current_filament
        if not gate_map.is_valid_gate(gate):
            return {
                "filament_name": "",
                "material": "",
                "color": "",
                "spool_id": -1,
                "temperature": -1,
            }
        info = gate_map[gate]
        return {
            "filament_name": info.name,
            "material": info.material,
            "color": info.color,
            "spool_id": info.spool_id,
            "temperature": info.temperature,
        }

    def get_status(self, eventtime: float) -> dict:
        """Return the status in Happy Hare's ``mmu`` shape.

        Every list/dict is built fresh on each call so Klipper's change
        detection pushes updates to Moonraker.

        Args:
            eventtime (float): The current event time.

        Returns:
            dict: The status.
        """
        mmu3 = self.mmu3
        num_gates = mmu3.number_of_tools
        gate_map = mmu3.gate_map
        operation = mmu3.current_operation
        is_tool_change = operation is not None and operation.kind.value == "tool_change"
        print_state = self.print_state(eventtime)
        return {
            "enabled": mmu3.is_enabled,
            "num_gates": num_gates,
            "is_homed": mmu3.is_homed,
            "is_locked": mmu3.is_paused,
            "is_paused": mmu3.is_paused,
            "is_in_print": print_state in ("printing", "paused", "pause_locked"),
            "print_state": print_state,
            "unit": 0,
            "gate": self.gate(),
            "tool": self.tool(),
            "last_tool": (
                _or_unknown(operation.from_tool)
                if is_tool_change
                else TOOL_GATE_UNKNOWN
            ),
            "next_tool": (
                _or_unknown(operation.to_tool) if is_tool_change else TOOL_GATE_UNKNOWN
            ),
            "num_toolchanges": self.num_toolchanges(),
            "active_filament": self.active_filament(),
            "action": mmu3.action,
            "filament": self.filament(),
            "filament_pos": self.filament_pos(),
            "filament_position": 0.0,
            "filament_direction": self.filament_direction(),
            "bowden_progress": -1,
            "reason_for_pause": (
                mmu3.pending_operation.describe()
                if mmu3.pending_operation is not None
                else ""
            ),
            "ttg_map": list(range(num_gates)),
            "endless_spool_groups": list(range(num_gates)),
            "endless_spool": 0,
            "endless_spool_enabled": 0,
            "gate_status": gate_map.statuses(),
            "gate_filament_name": gate_map.names(),
            "gate_material": gate_map.materials(),
            "gate_color": gate_map.colors(),
            "gate_temperature": gate_map.temperatures(),
            "gate_spool_id": gate_map.spool_ids(),
            "gate_speed_override": gate_map.speed_overrides(),
            "spoolman_support": mmu3.spoolman_support,
            "pending_spool_id": -1,
            "has_bypass": False,
            "sync_drive": False,
            "clog_detection": 0,
            "clog_detection_enabled": 0,
            "print_start_detection": 0,
            "sensors": self.sensors(),
        }


class MmuMachine:
    """The ``mmu_machine`` status object, in Happy Hare's shape.

    Mainsail takes the number of gates drawn per unit from here.

    Args:
        mmu3 (MMU3): The MMU3 instance to report the status of.
    """

    def __init__(self, mmu3: MMU3) -> None:
        self.mmu3 = mmu3

    def get_status(self, eventtime: float) -> dict:
        """Return the status in Happy Hare's ``mmu_machine`` shape.

        Args:
            eventtime (float): The current event time.

        Returns:
            dict: The status.
        """
        mmu3 = self.mmu3
        return {
            "num_units": 1,
            "unit_0": {
                "name": "MMU3",
                # not a vendor Mainsail has a logo for, it falls back to its
                # default MMU logo
                "vendor": "Prusa",
                "version": "3.0",
                "num_gates": mmu3.number_of_tools,
                "first_gate": 0,
                "selector_type": (
                    "VirtualSelector"
                    if mmu3.enable_no_selector_mode
                    else "LinearSelector"
                ),
                "variable_rotation_distances": False,
                "variable_bowden_lengths": False,
                "require_bowden_move": True,
                "filament_always_gripped": False,
                "can_crossload": False,
                "has_bypass": False,
                "multi_gear": False,
            },
        }
