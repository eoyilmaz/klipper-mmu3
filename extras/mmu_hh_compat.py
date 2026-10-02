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
    from extras.mmu import MMU


TOOL_GATE_UNKNOWN = -1

FILAMENT_POS_UNKNOWN = -1
FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_HOMED_GATE = 1
FILAMENT_POS_IN_BOWDEN = 3
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

PRINT_STATE_READY = "ready"
PRINT_STATE_PRINTING = "printing"
PRINT_STATE_PAUSED = "paused"
PRINT_STATE_COMPLETE = "complete"

# Klipper print_stats.state -> Happy Hare print_state
PRINT_STATE_MAP = {
    "standby": PRINT_STATE_READY,
    "printing": PRINT_STATE_PRINTING,
    "paused": PRINT_STATE_PAUSED,
    "complete": PRINT_STATE_COMPLETE,
    "cancelled": "cancelled",
    "error": "error",
}
# the print_state values of a print in progress
IN_PRINT_STATES = (PRINT_STATE_PRINTING, PRINT_STATE_PAUSED)
# the end states MMU_PRINT_END accepts, same as Happy Hare
PRINT_END_STATES = ("complete", "cancelled", "error", "ready", "standby")


def _or_unknown(value: None | int) -> int:
    """Return the value, or -1 (unknown) if it is None.

    Args:
        value (None | int): A tool or gate index.

    Returns:
        int: The value or -1.
    """
    return TOOL_GATE_UNKNOWN if value is None else value


class MmuStatus:
    """The Happy Hare fields of the ``mmu`` status, in Happy Hare's shape.

    :meth:`extras.mmu.MMU.get_status` adds the MMU3 specific fields to these.

    Args:
        mmu (MMU): The MMU instance to report the status of.
    """

    def __init__(self, mmu: MMU) -> None:
        self.mmu = mmu

    @property
    def switch_sensor_key(self) -> str:
        """Return the Happy Hare sensor name of the filament switch sensor.

        A sensor after the extruder gears is a toolhead sensor, one at or
        before the gears is an extruder (entry) sensor.

        Returns:
            str: ``toolhead`` or ``extruder``.
        """
        # imported here, mmu imports this module
        from extras.mmu import FilamentSwitchSensorPosition

        if (
            self.mmu.filament_switch_sensor_position
            == FilamentSwitchSensorPosition.PostGears
        ):
            return "toolhead"
        return "extruder"

    def filament_pos(self) -> int:
        """Return the filament position as a Happy Hare ``FILAMENT_POS_*`` value.

        While the filament moves between FINDA and the extruder it is in the
        bowden, the panel then draws it at ``bowden_progress``.

        Returns:
            int: The filament position.
        """
        if self.mmu.filament_tracker.is_bowden_move:
            return FILAMENT_POS_IN_BOWDEN
        name = self.mmu.filament_pos.name
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
        name = self.mmu.filament_pos.name
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
        mmu = self.mmu
        if mmu.current_gate is not None:
            return mmu.current_gate
        return _or_unknown(mmu.loaded_gate)

    def tool(self) -> int:
        """Return the loaded tool, falling back to the selected one.

        The tool that maps to the loaded gate (or the selected gate) in the
        tool-to-gate map, see :meth:`extras.mmu.MMU.gate_to_tool`.

        Returns:
            int: The tool index or -1.
        """
        mmu = self.mmu
        gate = mmu.loaded_gate if mmu.loaded_gate is not None else mmu.current_gate
        return _or_unknown(mmu.gate_to_tool(gate))

    def print_state(self, eventtime: float) -> str:
        """Return the Happy Hare ``print_state``.

        Follows ``print_stats`` first, so the panel sees a print start / end
        right away instead of on the next poll.

        Returns:
            str: e.g. ``ready``, ``printing``, ``pause_locked``.
        """
        mmu = self.mmu
        mmu.follow_print_stats(eventtime)
        if mmu.is_paused:
            # the MMU paused itself and waits for the operator
            return "pause_locked"
        return mmu.print_state

    def filament_direction(self) -> int:
        """Return the direction the filament is currently moving.

        Returns:
            int: 1 loading, -1 unloading, 0 idle.
        """
        action = self.mmu.action
        if action in LOAD_ACTIONS:
            return DIRECTION_LOAD
        if action in UNLOAD_ACTIONS:
            return DIRECTION_UNLOAD
        return DIRECTION_UNKNOWN

    def sensors(self) -> dict:
        """Return the sensor states.

        FINDA is the gate sensor. It is not queried here - querying an MCU
        endstop pauses the reactor, which ``get_status`` must never do - the
        state the MCU reports on every change of the FINDA pin is reported
        instead.

        Returns:
            dict: Happy Hare sensor name -> triggered.
        """
        mmu = self.mmu
        sensors = {"mmu_gate": mmu.finda_triggered}
        if mmu.filament_switch_sensor is not None:
            sensors[self.switch_sensor_key] = bool(
                mmu.filament_switch_sensor.get_status(None)["filament_detected"]
            )
        return sensors

    def num_toolchanges(self) -> int:
        """Return the successful tool changes in the current job.

        Returns:
            int: The count.
        """
        # imported here, mmu imports this module
        from extras.mmu import OperationKind

        stats = self.mmu.job_stats
        return stats.attempts.get(OperationKind.TOOL_CHANGE, 0) - stats.failures.get(
            OperationKind.TOOL_CHANGE, 0
        )

    def active_filament(self) -> dict:
        """Return the metadata of the gate that is currently loaded.

        Returns:
            dict: The filament name, material, color, spool id and temperature.
        """
        gate_map = self.mmu.gate_map
        gate = self.mmu.loaded_gate
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
        mmu = self.mmu
        num_gates = mmu.number_of_tools
        gate_map = mmu.gate_map
        operation = mmu.current_operation
        is_tool_change = operation is not None and operation.kind.value == "tool_change"
        print_state = self.print_state(eventtime)
        return {
            "enabled": mmu.is_enabled,
            "num_gates": num_gates,
            "is_homed": mmu.is_homed,
            "is_locked": mmu.is_paused,
            "is_paused": mmu.is_paused,
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
            "action": mmu.action,
            "filament": self.filament(),
            "filament_pos": self.filament_pos(),
            "filament_position": round(mmu.filament_tracker.position(eventtime), 1),
            "filament_direction": self.filament_direction(),
            "bowden_progress": mmu.filament_tracker.bowden_progress(eventtime),
            "reason_for_pause": (
                mmu.pending_operation.describe()
                if mmu.pending_operation is not None
                else ""
            ),
            "ttg_map": list(mmu.ttg_map),
            "slicer_tool_map": mmu.slicer_tool_map.to_dict(),
            "endless_spool_groups": list(mmu.endless_spool_groups),
            # Happy Hare's deprecated name of endless_spool_enabled
            "endless_spool": int(mmu.endless_spool_enabled),
            "endless_spool_enabled": int(mmu.endless_spool_enabled),
            "gate_status": gate_map.statuses(),
            "gate_filament_name": gate_map.names(),
            "gate_material": gate_map.materials(),
            "gate_color": gate_map.colors(),
            "gate_temperature": gate_map.temperatures(),
            "gate_spool_id": gate_map.spool_ids(),
            "gate_speed_override": gate_map.speed_overrides(),
            "spoolman_support": mmu.spoolman_support,
            "pending_spool_id": -1,
            "has_bypass": False,
            "sync_drive": False,
            "clog_detection": 0,
            "clog_detection_enabled": 0,
            "print_start_detection": int(mmu.print_start_detection),
            "sensors": self.sensors(),
        }


class MmuMachine:
    """The ``mmu_machine`` status object, in Happy Hare's shape.

    Mainsail takes the number of gates drawn per unit from here.

    Args:
        mmu (MMU): The MMU instance to report the status of.
    """

    def __init__(self, mmu: MMU) -> None:
        self.mmu = mmu

    def get_status(self, eventtime: float) -> dict:
        """Return the status in Happy Hare's ``mmu_machine`` shape.

        Args:
            eventtime (float): The current event time.

        Returns:
            dict: The status.
        """
        mmu = self.mmu
        return {
            "num_units": 1,
            "unit_0": {
                "name": "MMU3",
                # not a vendor Mainsail has a logo for, it falls back to its
                # default MMU logo
                "vendor": "Prusa",
                "version": "3.0",
                "num_gates": mmu.number_of_tools,
                "first_gate": 0,
                "selector_type": (
                    "VirtualSelector"
                    if mmu.enable_no_selector_mode
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
