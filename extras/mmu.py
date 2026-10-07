"""MMU3 multi-material unit management."""

# Standard Library Imports
from __future__ import annotations

import ast
import configparser
import contextlib
import enum
import json
import logging
import re
import time
from functools import partial, wraps
from typing import TYPE_CHECKING, Callable

# Klipper Imports
from extras.manual_stepper import ManualStepper

# Local Imports
from extras.mmu_gate_map import (
    GATE_AVAILABLE,
    GATE_EMPTY,
    GATE_UNKNOWN,
    NO_SPOOL,
    GateMap,
    normalize_color,
)
from extras.mmu_hh_compat import (
    ACTION_CHECKING,
    ACTION_CUTTING_FILAMENT,
    ACTION_CUTTING_TIP,
    ACTION_FORMING_TIP,
    ACTION_HEATING,
    ACTION_HOMING,
    ACTION_IDLE,
    ACTION_LOADING,
    ACTION_LOADING_EXTRUDER,
    ACTION_SELECTING,
    ACTION_UNLOADING,
    ACTION_UNLOADING_EXTRUDER,
    IN_PRINT_STATES,
    PRINT_END_STATES,
    PRINT_STATE_MAP,
    PRINT_STATE_PAUSED,
    PRINT_STATE_PRINTING,
    PRINT_STATE_READY,
    SPOOLMAN_OFF,
    SPOOLMAN_READONLY,
    SPOOLMAN_SUPPORT_VALUES,
    MmuMachine,
    MmuStatus,
)
from extras.mmu_mainsail_prompts import (
    Button,
    ButtonGroup,
    FooterButton,
    Prompt,
    Text,
)

if TYPE_CHECKING:
    import sys

    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

    from collections.abc import Iterator
    from types import TracebackType

    from configfile import ConfigWrapper
    from gcode import GCodeCommand, GCodeDispatch
    from kinematics.extruder import PrinterExtruder
    from klippy import Printer
    from mcu import MCU_endstop
    from reactor import PollReactor as Reactor
    from stepper import MCU_stepper
    from toolhead import ToolHead

    from extras.display_status import DisplayStatus
    from extras.filament_motion_sensor import EncoderSensor
    from extras.filament_switch_sensor import SwitchSensor
    from extras.gcode_move import GCodeMove
    from extras.heaters import Heater, PrinterHeaters
    from extras.motion_queuing import PrinterMotionQueuing
    from extras.query_endstops import QueryEndstops


IDLER_STEPPER_NAME = "manual_stepper idler_stepper"
PULLEY_STEPPER_NAME = "manual_stepper pulley_stepper"
SELECTOR_STEPPER_NAME = "manual_stepper selector_stepper"

STEPPER_NAME_MAP = {
    PULLEY_STEPPER_NAME: "FINDA",
    SELECTOR_STEPPER_NAME: "Selector",
}

IS_DIGIT = re.compile(r"[0-9\-.]+")

TOTAL_STATS_VARIABLE = "mmu_total_stats"
GATE_MAP_VARIABLE = "mmu_gate_map"
TTG_MAP_VARIABLE = "mmu_ttg_map"
ENDLESS_SPOOL_ENABLED_VARIABLE = "mmu_endless_spool_enabled"
ENDLESS_SPOOL_GROUPS_VARIABLE = "mmu_endless_spool_groups"
# Happy Hare's material of a slicer tool when MATERIAL= isn't given
SLICER_MATERIAL_UNKNOWN = "unknown"
# the names before the [mmu3 MMU3] -> [mmu] rename, read when the new
# variable isn't saved yet
LEGACY_VARIABLES = {
    TOTAL_STATS_VARIABLE: "mmu3_total_stats",
    GATE_MAP_VARIABLE: "mmu3_gate_map",
}

GATE_STATUS_TEXT = {
    GATE_UNKNOWN: "unknown",
    GATE_EMPTY: "empty",
    GATE_AVAILABLE: "available",
}

# optional user macros, called only if defined, named as in Happy Hare
PRE_UNLOAD_MACRO = "_MMU_PRE_UNLOAD"
POST_UNLOAD_MACRO = "_MMU_POST_UNLOAD"
PRE_LOAD_MACRO = "_MMU_PRE_LOAD"
POST_LOAD_MACRO = "_MMU_POST_LOAD"
# around an endless spool tool change, while the print is paused
ENDLESS_SPOOL_PRE_UNLOAD_MACRO = "_MMU_ENDLESS_SPOOL_PRE_UNLOAD"
ENDLESS_SPOOL_POST_LOAD_MACRO = "_MMU_ENDLESS_SPOOL_POST_LOAD"
ACTION_CHANGED_MACRO = "_MMU_ACTION_CHANGED"
# tip forming (ramming) and the in-extruder cut, Happy Hare's names for them
FORM_TIP_MACRO = "_MMU_FORM_TIP"
CUT_TIP_MACRO = "_MMU_CUT_TIP"
# the settings of _MMU_FORM_TIP, changed at runtime by MMU_TEST_FORM_TIP
FORM_TIP_VARS_MACRO = "_MMU_FORM_TIP_VARS"
# the MMU_TEST_FORM_TIP parameters that are not tip forming variables
TEST_FORM_TIP_PARAMS = ("RESET", "SHOW", "RUN")

logger = logging.getLogger(__name__)
PRINT_STATS_POLL_INTERVAL = 10.0
# how often the extruder is checked while waiting for the end of the filament
# to reach the filament switch sensor
RUNOUT_TAIL_CHECK_INTERVAL = 0.5


class FilamentPos(enum.IntEnum):
    """Where the filament tip currently sits along the MMU -> nozzle path.

    Ordered: a larger value means the filament has moved further from the
    MMU toward the nozzle, so the planner can compare positions directly.
    """

    UNLOADED = 0
    AT_FINDA = 1
    AT_EXTRUDER = 2
    IN_HOTEND = 3
    LOADED = 4

    def __repr__(self) -> str:
        """Return the enum name for str().

        Returns:
            str: The name as the string representation.
        """
        return self.name

    __str__ = __repr__


class OperationKind(enum.Enum):
    """The kind of high level MMU operation that can be pending recovery."""

    TOOL_CHANGE = "tool_change"
    LOAD = "load"
    UNLOAD = "unload"
    HOME = "home"
    CUT = "cut"

    def __repr__(self) -> str:
        """Return the enum name for str().

        Returns:
            str: The name as the string representation.
        """
        return self.name

    __str__ = __repr__


class Operation:
    """A record of the high level MMU operation currently in progress.

    Held on ``MMU.current_operation`` while a top level command runs and
    promoted to ``MMU.pending_operation`` by :func:`auto_pause` when the
    command fails, so recovery (``MMU_RETRY`` / ``RESUME_MMU``) knows what the
    operator was trying to do without them having to remember it.

    Args:
        kind (OperationKind): The kind of operation.
        from_tool (None | int): The tool that was loaded when the operation
            started.
        to_tool (None | int): The tool the operation is trying to end up with
            loaded. ``None`` for pure unloads / homing, or a gate no tool maps
            to.
        from_gate (None | int): The gate that was loaded when the operation
            started.
        to_gate (None | int): The gate the operation is trying to end up with
            loaded. ``None`` for pure unloads / homing.
    """

    def __init__(
        self,
        kind: OperationKind,
        from_tool: None | int = None,
        to_tool: None | int = None,
        from_gate: None | int = None,
        to_gate: None | int = None,
    ) -> None:
        self.kind = kind
        self.from_tool = from_tool
        self.to_tool = to_tool
        self.from_gate = from_gate
        self.to_gate = to_gate
        self.filament_pos_at_fail: None | FilamentPos = None
        self.error: str = ""

    @property
    def target_pos(self) -> FilamentPos:
        """Return the filament position this operation is trying to reach."""
        loads = (OperationKind.TOOL_CHANGE, OperationKind.LOAD)
        if self.kind in loads and self.to_gate is not None:
            return FilamentPos.LOADED
        return FilamentPos.UNLOADED

    def describe(self) -> str:
        """Return a short human readable summary for prompts and status.

        Returns:
            str: e.g. ``"Tool change T1 => T2 - stopped with filament at the
            extruder"``.
        """
        from_text = tool_text(self.from_tool, self.from_gate)
        to_text = tool_text(self.to_tool, self.to_gate)
        if self.kind == OperationKind.TOOL_CHANGE and from_text:
            what = f"Tool change {from_text} => {to_text}"
        elif self.kind in (OperationKind.TOOL_CHANGE, OperationKind.LOAD):
            what = f"Load {to_text}"
        elif self.kind == OperationKind.UNLOAD:
            what = f"Unload {from_text}" if from_text else "Unload filament"
        elif self.kind == OperationKind.CUT:
            what = f"Cut {to_text}"
        else:
            what = "Home MMU"

        stage = {
            FilamentPos.UNLOADED: "at the MMU",
            FilamentPos.AT_FINDA: "at FINDA",
            FilamentPos.AT_EXTRUDER: "at the extruder",
            FilamentPos.IN_HOTEND: "in the hotend",
            FilamentPos.LOADED: "loaded",
        }.get(self.filament_pos_at_fail)
        if stage:
            what = f"{what} - stopped with filament {stage}"
        if self.error:
            what = f"{what} ({self.error})"
        return what


class OperationStats:
    """Aggregate counters for :class:`Operation` runs.

    Used for both the lifetime-of-the-printer totals (``MMU.total_stats``,
    persisted via ``save_variables``) and the current-job counters
    (``MMU.job_stats``, reset when a new print starts).
    """

    def __init__(self) -> None:
        self.attempts: dict[OperationKind, int] = {}
        self.failures: dict[OperationKind, int] = {}
        self.toolchanges: dict[tuple[int, int], int] = {}

    def record(self, operation: Operation, success: bool) -> None:
        """Record the outcome of a finished operation.

        Args:
            operation (Operation): The operation that just finished.
            success (bool): Whether it completed successfully.
        """
        self.attempts[operation.kind] = self.attempts.get(operation.kind, 0) + 1
        if not success:
            self.failures[operation.kind] = self.failures.get(operation.kind, 0) + 1
            return
        if (
            operation.kind == OperationKind.TOOL_CHANGE
            and operation.from_tool is not None
            and operation.to_tool is not None
        ):
            key = (operation.from_tool, operation.to_tool)
            self.toolchanges[key] = self.toolchanges.get(key, 0) + 1

    def reset(self) -> None:
        """Clear all counters."""
        self.attempts.clear()
        self.failures.clear()
        self.toolchanges.clear()

    def to_dict(self) -> dict:
        """Return a JSON/``save_variables``-friendly snapshot.

        Returns:
            dict: The counters keyed by plain strings.
        """
        return {
            "attempts": {k.value: v for k, v in self.attempts.items()},
            "failures": {k.value: v for k, v in self.failures.items()},
            "toolchanges": {
                f"{from_tool}->{to_tool}": count
                for (from_tool, to_tool), count in self.toolchanges.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> OperationStats:
        """Rebuild counters from a snapshot produced by :meth:`to_dict`.

        Anything malformed (unknown kind, unparsable toolchange key, wrong
        types) is silently skipped so a corrupt or stale ``save_variables``
        entry can never prevent startup.

        Args:
            data (dict): The snapshot, as previously returned by
                :meth:`to_dict`.

        Returns:
            OperationStats: The rebuilt instance.
        """
        stats = cls()
        kinds_by_value = {k.value: k for k in OperationKind}
        for field_name, target in (
            ("attempts", stats.attempts),
            ("failures", stats.failures),
        ):
            for key, value in data.get(field_name, {}).items():
                kind = kinds_by_value.get(key)
                if kind is not None and isinstance(value, int):
                    target[kind] = value
        for key, value in data.get("toolchanges", {}).items():
            if not isinstance(value, int):
                continue
            from_str, _, to_str = str(key).partition("->")
            with contextlib.suppress(ValueError):
                stats.toolchanges[(int(from_str), int(to_str))] = value
        return stats


def tool_text(tool: None | int, gate: None | int) -> str:
    """Return how a tool / gate pair is named in messages.

    Args:
        tool (None | int): The tool, None if unknown.
        gate (None | int): The gate, None if unknown.

    Returns:
        str: ``T<tool>``, ``gate <gate>`` if no tool maps to the gate, or an
            empty string if both are unknown.
    """
    if tool is not None:
        return f"T{tool}"
    if gate is not None:
        return f"gate {gate}"
    return ""


def default_ttg_map(num_gates: int) -> list[int]:
    """Return the default tool-to-gate map, tool n is gate n.

    Args:
        num_gates (int): The number of gates (and tools).

    Returns:
        list[int]: The identity map.
    """
    return list(range(num_gates))


def ttg_map_from_saved(num_gates: int, value: object) -> list[int]:
    """Return the tool-to-gate map saved with ``save_variables``.

    A missing, corrupt or stale map (e.g. saved with a different number of
    gates) falls back to the default map.

    Args:
        num_gates (int): The number of gates (and tools).
        value (object): The saved value.

    Returns:
        list[int]: The tool-to-gate map.
    """
    if (
        isinstance(value, (list, tuple))
        and len(value) == num_gates
        and all(
            isinstance(gate, int)
            and not isinstance(gate, bool)
            and 0 <= gate < num_gates
            for gate in value
        )
    ):
        return list(value)
    return default_ttg_map(num_gates)


def map_tool_to_gate(tool: int, ttg_map: None | list[int]) -> int:
    """Return the gate a tool maps to.

    Args:
        tool (int): The tool.
        ttg_map (None | list[int]): The tool-to-gate map, None for the
            default map.

    Returns:
        int: The mapped gate, or the tool itself if it is not in the map
            (e.g. ``-1`` or an invalid tool, left for the caller to reject).
    """
    if ttg_map is not None and 0 <= tool < len(ttg_map):
        return ttg_map[tool]
    return tool


def parse_macro_variable(value: str) -> object:
    """Parse a ``gcode_macro`` variable value given on the command line.

    Same as Klipper's ``SET_GCODE_VARIABLE`` and ``variable_*`` config
    options (a Python literal), but a value that isn't one is kept as a
    string, so ``TOOLCHANGE_FAN_NAME="fan_generic fan0"`` needs no extra
    quotes.

    Args:
        value (str): The value as given.

    Returns:
        object: The parsed value.
    """
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def default_endless_spool_groups(num_gates: int) -> list[int]:
    """Return the default endless spool groups, each gate in its own group.

    Args:
        num_gates (int): The number of gates.

    Returns:
        list[int]: The group of each gate.
    """
    return list(range(num_gates))


def is_valid_endless_spool_groups(num_gates: int, value: object) -> bool:
    """Return True if ``value`` is a valid list of endless spool groups.

    Args:
        num_gates (int): The number of gates.
        value (object): The groups to check, one non-negative int per gate.

    Returns:
        bool: True if valid.
    """
    return (
        isinstance(value, (list, tuple))
        and len(value) == num_gates
        and all(
            isinstance(group, int) and not isinstance(group, bool) and group >= 0
            for group in value
        )
    )


def endless_spool_groups_from_saved(
    num_gates: int, value: object, default: list[int]
) -> list[int]:
    """Return the endless spool groups saved with ``save_variables``.

    A missing, corrupt or stale value (e.g. saved with a different number of
    gates) falls back to ``default``.

    Args:
        num_gates (int): The number of gates.
        value (object): The saved value.
        default (list[int]): The groups to use if the saved value is invalid.

    Returns:
        list[int]: The group of each gate.
    """
    if is_valid_endless_spool_groups(num_gates, value):
        return list(value)
    return list(default)


def group_name(group: int) -> str:
    """Return how an endless spool group is named in messages, like Happy Hare.

    Args:
        group (int): The group.

    Returns:
        str: ``A`` for group 0, ``B`` for 1, ... and the number past ``Z``.
    """
    if 0 <= group < 26:
        return chr(ord("A") + group)
    return str(group)


def next_endless_spool_gate(
    gate: int, groups: list[int], gate_statuses: list[int]
) -> tuple[None | int, list[int]]:
    """Return the gate endless spool continues with after ``gate`` runs out.

    Same as Happy Hare: the gates after ``gate`` are checked in order,
    wrapping around, and the first one in the same group that is not empty
    (available or unknown) is picked.

    Args:
        gate (int): The gate that ran out.
        groups (list[int]): The endless spool group of each gate.
        gate_statuses (list[int]): The status of each gate.

    Returns:
        tuple[None | int, list[int]]: The next gate (None if no gate is
            left) and the gates of the group that were checked.
    """
    num_gates = len(groups)
    if not 0 <= gate < num_gates:
        return None, []
    checked = []
    for offset in range(1, num_gates):
        candidate = (gate + offset) % num_gates
        if groups[candidate] != groups[gate]:
            continue
        checked.append(candidate)
        if gate_statuses[candidate] != GATE_EMPTY:
            return candidate, checked
    return None, checked


def is_known_material(material: str) -> bool:
    """Return True if a material is set, Happy Hare's ``unknown`` is not.

    Args:
        material (str): The material.

    Returns:
        bool: True if the material is set.
    """
    return material.strip().lower() not in ("", SLICER_MATERIAL_UNKNOWN)


def is_matching_material(slicer_material: str, gate_material: str) -> bool:
    """Return True if a gate's material is the material the slicer asks for.

    The materials must be the same, ignoring case and surrounding spaces:
    ``PLA`` doesn't fit ``PLA+`` (similar names can print at very different
    temperatures, e.g. ``PC`` and ``PCTG``). An unknown material on either
    side always fits.

    Args:
        slicer_material (str): The material of the tool in the print.
        gate_material (str): The material of the gate.

    Returns:
        bool: True if the materials fit.
    """
    if not (is_known_material(slicer_material) and is_known_material(gate_material)):
        return True
    return slicer_material.strip().lower() == gate_material.strip().lower()


class SlicerToolMap:
    """The tools the print file uses, as the slicer's start G-code gives them.

    Filled by ``MMU_SLICER_TOOL_MAP`` and reported as Happy Hare's
    ``slicer_tool_map``, see :meth:`to_dict`.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the tools of the previous print."""
        self.tools: dict[int, dict] = {}
        self.referenced_tools: list[int] = []
        self.initial_tool: None | int = None
        self.total_toolchanges: None | int = None

    @property
    def is_empty(self) -> bool:
        """Return True if no tool or initial tool is set.

        Returns:
            bool: True if the map is empty.
        """
        return not self.tools and self.initial_tool is None

    def set_tool(
        self,
        tool: int,
        color: str = "",
        material: str = SLICER_MATERIAL_UNKNOWN,
        temp: int = 0,
        name: str = "",
        used: bool = True,
    ) -> None:
        """Set the filament of a tool, like Happy Hare.

        Args:
            tool (int): The tool.
            color (str): The filament color, see :func:`normalize_color`.
            material (str): The filament material.
            temp (int): The print temperature.
            name (str): The filament name.
            used (bool): Whether the print uses the tool.
        """
        self.tools[tool] = {
            "color": normalize_color(color),
            "material": material,
            "temp": temp,
            "name": name,
            "in_use": used,
        }
        if used:
            self.add_referenced_tool(tool)

    def set_initial_tool(self, tool: int) -> None:
        """Set the tool the print starts with, it is also a used tool.

        Args:
            tool (int): The tool.
        """
        self.initial_tool = tool
        self.add_referenced_tool(tool)

    def add_referenced_tool(self, tool: int) -> None:
        """Record that the print uses a tool.

        Args:
            tool (int): The tool.
        """
        self.referenced_tools = sorted({*self.referenced_tools, tool})

    def to_dict(self) -> dict:
        """Return the map in Happy Hare's ``slicer_tool_map`` shape.

        Built fresh on each call so Klipper's change detection pushes updates
        to Moonraker. ``purge_volumes`` is always empty and ``skip_automap``
        always False, the MMU3 doesn't calculate purge volumes or map the
        tools to gates automatically.

        Returns:
            dict: ``tools`` (keyed by the tool as a string),
                ``referenced_tools``, ``initial_tool``, ``purge_volumes``,
                ``total_toolchanges`` and ``skip_automap``.
        """
        return {
            "tools": {
                str(tool): dict(info) for tool, info in sorted(self.tools.items())
            },
            "referenced_tools": list(self.referenced_tools),
            "initial_tool": self.initial_tool,
            "purge_volumes": [],
            "total_toolchanges": self.total_toolchanges,
            "skip_automap": False,
        }


def slicer_tool_map_warnings(
    slicer_tool_map: SlicerToolMap, ttg_map: list[int], gate_map: GateMap
) -> list[str]:
    """Return what doesn't match between the print's tools and the gates.

    A tool the print uses is reported if it maps to an empty gate, or to a
    gate whose material doesn't fit, see :func:`is_matching_material`. Only
    gates known to be empty are reported as empty.

    Args:
        slicer_tool_map (SlicerToolMap): The tools the print uses.
        ttg_map (list[int]): The tool-to-gate map.
        gate_map (GateMap): The filament of each gate.

    Returns:
        list[str]: One message per mismatch, empty if everything matches.
    """
    warnings = []
    for tool in slicer_tool_map.referenced_tools:
        gate = map_tool_to_gate(tool, ttg_map)
        if not gate_map.is_valid_gate(gate):
            continue
        info = gate_map[gate]
        if info.status == GATE_EMPTY:
            warnings.append(f"T{tool} loads gate {gate}, which is empty.")
            continue
        material = slicer_tool_map.tools.get(tool, {}).get("material", "")
        if not is_matching_material(material, info.material):
            warnings.append(
                f"T{tool} is {material} in the print, but gate {gate} has "
                f"{info.material}."
            )
    return warnings


def get_tool_and_gate_params(
    gcmd: None | GCodeCommand,
    ttg_map: None | list[int] = None,
    minval: None | int = None,
) -> tuple[None | int, None | int]:
    """Return the tool and gate given with ``TOOL=`` and ``GATE=``.

    ``GATE=`` bypasses the tool-to-gate map, a ``TOOL=`` alone is resolved to
    its gate through it.

    Args:
        gcmd (None | GCodeCommand): The G-code command.
        ttg_map (None | list[int]): The tool-to-gate map, None for the
            default map.
        minval (None | int): The smallest accepted value.

    Returns:
        tuple[None | int, None | int]: The tool (None if no ``TOOL=``) and
            the gate (None if no parameter is given).
    """
    if gcmd is None:
        return None, None
    gate = gcmd.get_int("GATE", None, minval=minval)
    tool = gcmd.get_int("TOOL", None, minval=minval)
    if gate is None and tool is not None:
        gate = map_tool_to_gate(tool, ttg_map)
    return tool, gate


def get_gate_param(
    gcmd: None | GCodeCommand,
    minval: None | int = None,
    ttg_map: None | list[int] = None,
) -> None | int:
    """Return the gate given with ``GATE=`` or ``TOOL=``.

    See :func:`get_tool_and_gate_params`, ``TOOL=`` is resolved to its gate
    through ``ttg_map``.

    Args:
        gcmd (None | GCodeCommand): The G-code command.
        minval (None | int): The smallest accepted value.
        ttg_map (None | list[int]): The tool-to-gate map, None for the
            default map.

    Returns:
        None | int: The gate, None if no parameter is given.
    """
    return get_tool_and_gate_params(gcmd, ttg_map=ttg_map, minval=minval)[1]


def get_gate_list_param(gcmd: GCodeCommand, name: str) -> None | list[int]:
    """Return a comma separated list of gates, e.g. ``GATES=0,2,3``.

    Args:
        gcmd (GCodeCommand): The G-code command.
        name (str): The parameter name.

    Raises:
        gcmd.error: If an item is not an integer.

    Returns:
        None | list[int]: The gates, None if the parameter is not given.
    """
    value = gcmd.get(name, None)
    if value is None:
        return None
    try:
        return [int(g) for g in value.split(",") if g.strip()]
    except ValueError:
        raise gcmd.error(f"Invalid {name}: {value}") from None


def measure_duration(f: Callable) -> Callable:
    """Report command duration.

    Args:
        f (Callable): The function to decorate.

    Returns:
        Callable: The wrapped function.
    """

    @wraps(f)
    def wrapped_f(self: MMU, gcmd: GCodeCommand, *args, **kwargs) -> None:
        start_time = time.time()
        result = f(self, gcmd, *args, **kwargs)
        duration = time.time() - start_time
        # condition the function name
        f_name = {
            "cmd_tx": "T",
            "cmd_load_gate": "MMU_LOAD",
            "cmd_unload_gate": "MMU_UNLOAD",
            "cmd_select_gate": "MMU_SELECT",
            "cmd_unselect_gate": "MMU_UNSELECT",
            "cmd_calibrate_pulley_rotation_distance": (
                "MMU_CALIBRATE_PULLEY_ROTATION_DISTANCE"
            ),
            "cmd_home_mmu": "MMU_HOME",
        }.get(f.__name__, f.__name__)
        if f_name in ["T"]:
            # replace with the proper command
            f_name = f"{f_name}{kwargs['tool_id']}"
        elif f_name in ["MMU_LOAD", "MMU_SELECT"]:
            gate = kwargs.get("gate", get_gate_param(gcmd, ttg_map=self.ttg_map))
            f_name = f"{f_name} {gate}"
        self.display_status_msg(f"{f_name} took {duration:0.1f} seconds")
        return result

    return wrapped_f


def auto_pause(f: Callable) -> Callable:
    """Decorator to automatically pause the MMU3 on command failure.

    If any of the decorated commands fail (return False), the MMU3 instance is
    paused automatically.

    On failure, the recovery prompt is (re-)shown for the resulting
    ``pending_operation``. Of the recovery dialog's buttons (see
    :meth:`MMU.show_recovery_prompt`), only "Retry" and "Resume" close it
    (via the ``PROMPT_CLOSE_AND_RUN_COMMAND`` macro sending
    ``action:prompt_end``); "Unlock MMU", "Unload" and "Home MMU" run
    their gcode with the dialog left open server-side, so a *successful* run
    of one of those must not re-send the prompt - Mainsail visibly closes and
    reopens an already-open dialog when it receives a fresh
    ``action:prompt_begin``/``action:prompt_show``, which is just UI flicker
    for a dialog that never went away.

    Args:
        f (Callable): The function to wrap.

    Returns:
        Callable: The wrapped function.
    """

    @wraps(f)
    def wrapped_f(self: MMU, gcmd: GCodeCommand, *args, **kwargs) -> None:
        if not self.is_enabled:
            self.display_status_msg("MMU is not enabled!")
            return False

        error_msg = ""
        try:
            result = f(self, gcmd, *args, **kwargs)
        except self.printer.command_error as e:
            self.respond_debug(f"{f.__name__} raised an error: {e}")
            self.display_status_msg(str(e))
            error_msg = str(e)
            result = False

        if not result:
            if not self.is_paused:
                # remember what the operator was trying to do so recovery
                # (MMU_RETRY / RESUME_MMU) does not depend on their memory
                if self.current_operation is not None:
                    self.current_operation.filament_pos_at_fail = self.filament_pos
                    if error_msg:
                        self.current_operation.error = error_msg
                    self.pending_operation = self.current_operation
                    self.current_operation = None
                self.pause()
            if self.pending_operation is not None:
                self.show_recovery_prompt()
        return result

    return wrapped_f


def track_operation(kind: OperationKind) -> Callable:
    """Decorator factory that records the high level operation in progress.

    Sets ``self.current_operation`` to a fresh :class:`Operation` on entry and
    clears ``current_operation`` when the wrapped command succeeds. Must be
    stacked *inside* :func:`auto_pause` so that, on failure, ``auto_pause``
    still sees a populated ``current_operation`` to promote.

    ``pending_operation`` is only cleared here if this command's success
    actually satisfies it (see :meth:`MMU._pending_operation_resolved`).
    Recovery-dialog buttons (``MMU_UNLOCK``, ``MMU_UNLOAD``, ``MMU_HOME``, ...) are
    unrelated one-off commands from the operator's point of view - each is a
    diagnostic/manual-recovery step, not necessarily a completion of whatever
    originally failed, so a lone success here must not silently discard a
    still-unfinished ``pending_operation``. Only :meth:`MMU.retry_pending_operation`
    (``MMU_RETRY`` / ``RESUME_MMU``) and a forced resume actually clear it
    unconditionally.

    Args:
        kind (OperationKind): The kind of operation the wrapped command performs.

    Returns:
        Callable: The actual decorator.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped_f(self: MMU, gcmd: GCodeCommand, *args, **kwargs) -> None:
            to_tool = kwargs.get("tool_id")
            to_gate = kwargs.get("gate")
            if to_gate is None and to_tool is not None:
                to_gate = self.tool_to_gate(to_tool)
            if to_gate is None and gcmd is not None:
                with contextlib.suppress(Exception):
                    to_tool, to_gate = get_tool_and_gate_params(gcmd, self.ttg_map)
            if to_gate is None and kind == OperationKind.LOAD:
                # MMU_LOAD without a gate loads the selected one
                with contextlib.suppress(Exception):
                    to_gate = self.default_gate()
            if to_tool is None:
                to_tool = self.gate_to_tool(to_gate)
            operation = Operation(
                kind=kind,
                from_tool=self.loaded_tool,
                to_tool=to_tool,
                from_gate=self.loaded_gate,
                to_gate=to_gate,
            )
            self.current_operation = operation
            result = False
            try:
                result = f(self, gcmd, *args, **kwargs)
            finally:
                # a raised command_error still reaches auto_pause's except
                # clause below - record it as a failure here too, and never
                # let a stats bug mask the real exception in flight.
                with contextlib.suppress(Exception):
                    self.total_stats.record(operation, success=bool(result))
                    self.job_stats.record(operation, success=bool(result))
                    self.save_total_stats()
                if result:
                    self.current_operation = None
                    if self._pending_operation_resolved():
                        self.pending_operation = None
                # point Spoolman at the spool now in the extruder, if any
                with contextlib.suppress(Exception):
                    self.sync_active_spool()
            return result

        return wrapped_f

    return decorator


def reports_action(action: str) -> Callable:
    """Decorator factory that reports ``action`` while the method runs.

    See :meth:`MMU.running_action`.

    Args:
        action (str): One of the Happy Hare ``ACTION_*`` strings.

    Returns:
        Callable: The actual decorator.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped_f(self: MMU, *args, **kwargs) -> bool:
            with self.running_action(action):
                return f(self, *args, **kwargs)

        return wrapped_f

    return decorator


def auto_disable_steppers(f: Callable) -> Callable:
    """Decorator to enable the steppers for a command and disable them after.

    Klipper only enables a disabled stepper when it moves, so a selected gate
    (an idler / selector move of zero distance) would leave the idler free to
    be dragged out of its position by the pulley. The idler and selector are
    enabled before the command and all the steppers disabled after it.

    Args:
        f (Callable): The function to wrap.

    Returns:
        Callable: The wrapped function.
    """

    @wraps(f)
    def wrapped_f(self: MMU, gcmd: GCodeCommand, *args, **kwargs) -> None:
        try:
            self.enable_steppers()
            result = f(self, gcmd, *args, **kwargs)
        finally:
            self.disable_steppers()
        return result

    return wrapped_f


class FilamentSwitchSensorPosition(enum.Enum):
    """The position of the filament switch sensor."""

    PreGears = "pre_gears"
    OnGears = "on_gears"
    PostGears = "post_gears"

    def __repr__(self) -> str:
        """Return the enum name for str().

        Returns:
            str: The name as the string representation.
        """
        return self.name

    __str__ = __repr__

    @classmethod
    def to_switch_sensor_position(
        cls, position: str | FilamentSwitchSensorPosition
    ) -> FilamentSwitchSensorPosition:
        """Convert the given position value to a FilamentSwitchSensorPosition enum.

        Args:
            position (str | FilamentSwitchSensorPosition]): The value to convert to a
                FilamentSwitchSensorPosition.

        Raises:
            TypeError: Input value type is invalid.
            ValueError: Input value is invalid.

        Returns:
            FilamentSwitchSensorPosition: The enum.
        """
        valid = [e.name for e in cls] + [e.value for e in cls]
        if not isinstance(position, (str, FilamentSwitchSensorPosition)):
            raise TypeError(
                "position should be a FilamentSwitchSensorPosition enum value "
                f"or one of {valid}, "
                f"not {position.__class__.__name__}: '{position}'"
            )
        if isinstance(position, str):
            position_name_lut = {e.name.lower(): e.name for e in cls}
            position_name_lut.update({e.value: e.name for e in cls})
            position_lower_case = position.lower()
            if position_lower_case not in position_name_lut:
                raise ValueError(
                    "position should be a FilamentSwitchSensorPosition enum "
                    f"value or one of {valid}, not '{position}'"
                )

            return cls.__members__[position_name_lut[position_lower_case]]

        return position


class FilamentSwitchSensorManager:
    """This is a context manager to safely enable/disable filament switch sensors.

    Args:
        filament_switch_sensor (SwitchSensor): The filament switch sensor.
        state (bool): The desired state of the sensor inside the context.
    """

    def __init__(
        self,
        filament_switch_sensor: SwitchSensor,
        desired_state: bool = False,
        respond_debug: None | Callable = None,
        reactor: None | Reactor = None,  # noqa: UP037
        toolhead: None | ToolHead = None,
    ) -> None:
        self.filament_switch_sensor = filament_switch_sensor
        self.initial_state = False
        self.desired_state = desired_state
        if respond_debug is None:
            respond_debug = print
        self.respond_debug = respond_debug
        self.reactor = reactor
        self.toolhead = toolhead

    def __enter__(self) -> Self:
        """Enter to the context."""
        if self.filament_switch_sensor:
            # Synchronize: Wait for all queued moves to finish
            # physically before we turn the sensor back on/off.
            if self.toolhead:
                self.toolhead.wait_moves()

            # store the state
            self.initial_state = (
                self.filament_switch_sensor.runout_helper.sensor_enabled
            )
            self.respond_debug(
                "{} filament runout sensor!".format(
                    "Enabling" if self.desired_state else "Disabling"
                )
            )
            # set the desired state
            self.filament_switch_sensor.runout_helper.sensor_enabled = (
                self.desired_state
            )
        return self

    def __exit__(
        self,
        exc_type: None | type[BaseException],
        exc_value: None | BaseException,
        tb: None | TracebackType,
    ) -> None:
        """Exit the context.

        Ignore the exceptions, if any, Klipper will handle it.
        """
        if not self.filament_switch_sensor:
            return

        # Synchronize: Wait for all queued moves to finish
        # physically before we turn the sensor back on/off.
        if self.toolhead:
            self.toolhead.wait_moves()

        # restore the initial state
        self.respond_debug(
            "Re-{} filament runout sensor!".format(
                "Enabling" if self.initial_state else "Disabling"
            )
        )
        self.filament_switch_sensor.runout_helper.sensor_enabled = self.initial_state
        return


class FilamentMotionSensorManager:
    """This is a context manager to safely enable/disable filament motion sensors.

    Args:
        filament_motion_sensor (EncoderSensor): The filament motion sensor.
        state (bool): The desired state of the sensor inside the context.
    """

    def __init__(
        self,
        filament_motion_sensor: None | EncoderSensor,
        desired_state: bool = False,
        respond_debug: None | Callable = None,
        reactor: None | Reactor = None,  # noqa: UP037
        toolhead: None | ToolHead = None,
    ) -> None:
        self.filament_motion_sensor = filament_motion_sensor
        self.initial_state = None
        self.desired_state = desired_state
        if respond_debug is None:
            respond_debug = print
        self.respond_debug = respond_debug
        self.reactor = reactor
        self.toolhead = toolhead

    def __enter__(self) -> Self:
        """Enter to the context."""
        if not self.filament_motion_sensor:
            return self

        # Synchronize: Wait for all queued moves to finish
        # physically before we turn the sensor back on/off.
        if self.toolhead:
            self.toolhead.wait_moves()

        # store the state
        self.initial_state = self.filament_motion_sensor.runout_helper.sensor_enabled
        self.respond_debug(
            "{} filament motion sensor!".format(
                "Enabling" if self.desired_state else "Disabling"
            )
        )
        # set the desired state
        self.filament_motion_sensor.runout_helper.sensor_enabled = self.desired_state

        # also update the event time
        # so that the runout doesn't trigger as soon as it is enabled again
        event_time = self.reactor.monotonic() or self.toolhead.get_last_move_time()
        self.filament_motion_sensor.encoder_event(event_time, None)

        return self

    def __exit__(
        self,
        exc_type: None | type[BaseException],
        exc_value: None | BaseException,
        tb: None | TracebackType,
    ) -> None:
        """Exit the context.

        Ignore the exceptions, if any, Klipper will handle it.
        """
        if not self.filament_motion_sensor:
            return

        # Synchronize: Wait for all queued moves to finish
        # physically before we turn the sensor back on/off.
        if self.toolhead:
            self.toolhead.wait_moves()

        # restore the initial state
        self.respond_debug(
            "Re-{} filament motion sensor!".format(
                "Enabling" if self.initial_state else "Disabling"
            )
        )
        # also update the event time so that the runout doesn't trigger
        event_time = self.reactor.monotonic() or self.toolhead.get_last_move_time()
        self.filament_motion_sensor.encoder_event(event_time, None)
        self.filament_motion_sensor.runout_helper.sensor_enabled = self.initial_state
        return


class ExtruderSynchronizer:
    """Context manager to safely synchronize a manual stepper with the extruder.

    Args:
        mmu (MMU): The MMU instance.
        manual_stepper (ManualStepper): The stepper to synchronize with the extruder.
    """

    def __init__(self, mmu: MMU, manual_stepper: ManualStepper) -> None:
        self.mmu = mmu
        self.manual_stepper = manual_stepper
        self.orig_trapq = None

    def __enter__(self) -> Self:
        """Enter the context."""
        self.orig_trapq = self.mmu.sync_stepper_to_extruder(self.manual_stepper)
        return self

    def __exit__(
        self,
        exc_type: None | type[BaseException],
        exc_value: None | BaseException,
        tb: None | TracebackType,
    ) -> None:
        """Exit the context.

        Ignore the exceptions, if any, Klipper will handle it.
        """
        if self.orig_trapq is not None:
            self.mmu.unsync_stepper_from_extruder(self.manual_stepper, self.orig_trapq)


class FilamentTracker:
    """Track how far the filament tip is from FINDA, for the MMU panel.

    FINDA is the origin: it is the first point the MMU homes the filament to,
    anything before it is unknown. While a load / unload step runs, the
    distance is followed live from the pulley stepper, which also drives the
    filament while it is synced to the extruder. Between steps the distance
    measured by the last step is reported, or a nominal one derived from the
    configured lengths when ``filament_pos`` was set some other way.

    ``position()`` and ``bowden_progress()`` are called from ``get_status()``
    and only read the host side step history of the pulley stepper, they
    never query the MCU.

    Args:
        mmu (MMU): The MMU instance to track the filament of.
    """

    def __init__(self, mmu: MMU) -> None:
        self.mmu = mmu
        # (filament_pos, mm) measured at the end of the last tracked step
        self._measured: None | tuple[FilamentPos, float] = None
        # (pulley steps, mm) at the start of the running step
        self._start: None | tuple[int, float] = None
        self._is_bowden_move = False

    @property
    def is_tracking(self) -> bool:
        """Return True while a load / unload step is tracked."""
        return self._start is not None

    @property
    def is_bowden_move(self) -> bool:
        """Return True while the filament moves between FINDA and the extruder."""
        return self.is_tracking and self._is_bowden_move

    @property
    def bowden_length(self) -> float:
        """Return the nominal FINDA to extruder distance."""
        return float(self.mmu.bowden_load_length1)

    def nominal_position(self, filament_pos: FilamentPos) -> float:
        """Return the nominal distance of a filament position from FINDA.

        Args:
            filament_pos (FilamentPos): The filament position.

        Returns:
            float: The distance in mm.
        """
        mmu = self.mmu
        position = 0.0
        if filament_pos >= FilamentPos.AT_EXTRUDER:
            position += mmu.bowden_load_length1
        if filament_pos >= FilamentPos.IN_HOTEND:
            position += mmu.bowden_load_length3
        if filament_pos >= FilamentPos.LOADED:
            position += mmu.extra_load_length
        return position

    def _pulley_mcu_stepper(self) -> MCU_stepper:
        """Return the pulley stepper's MCU stepper."""
        return self.mmu.pulley_stepper.get_steppers()[0]

    def _queued_steps(self) -> int:
        """Return the pulley step count once every queued move completes."""
        self.mmu.toolhead.flush_step_generation()
        return self._pulley_mcu_stepper().get_mcu_position()

    def _moved(self, steps: int) -> float:
        """Return the distance the pulley moved since the step started.

        Args:
            steps (int): The current pulley step count.

        Returns:
            float: The distance in mm, negative when unloading.
        """
        start_steps, _ = self._start
        return (steps - start_steps) * self._pulley_mcu_stepper().get_step_dist()

    def _stored_position(self) -> float:
        """Return the position when no step is tracked."""
        filament_pos = self.mmu.filament_pos
        if filament_pos <= FilamentPos.AT_FINDA:
            # FINDA is the origin, and what is before it is not known
            return 0.0
        if self._measured is not None and self._measured[0] == filament_pos:
            return self._measured[1]
        return self.nominal_position(filament_pos)

    def position(self, eventtime: float) -> float:
        """Return the distance of the filament tip from FINDA.

        Args:
            eventtime (float): The current event time.

        Returns:
            float: The distance in mm.
        """
        if not self.is_tracking:
            return self._stored_position()
        mcu_stepper = self._pulley_mcu_stepper()
        print_time = mcu_stepper.get_mcu().estimated_print_time(eventtime)
        steps = mcu_stepper.get_past_mcu_position(print_time)
        _, start_position = self._start
        return max(0.0, start_position + self._moved(steps))

    def bowden_progress(self, eventtime: float) -> int:
        """Return how far the bowden move has got, in Happy Hare's convention.

        Args:
            eventtime (float): The current event time.

        Returns:
            int: 0 at FINDA to 100 at the extruder, -1 when not in a bowden
                move.
        """
        if not self.is_bowden_move or self.bowden_length <= 0:
            return -1
        progress = self.position(eventtime) / self.bowden_length * 100
        return round(max(0.0, min(100.0, progress)))

    def advance(self, distance: float) -> None:
        """Add a move the pulley did not see, e.g. an extruder only push.

        Args:
            distance (float): The distance in mm, negative when unloading.
        """
        if self.is_tracking:
            start_steps, start_position = self._start
            self._start = (start_steps, start_position + distance)

    @contextlib.contextmanager
    def track(self, is_bowden_move: bool = False) -> Iterator[None]:
        """Follow the filament live while the body runs.

        Nested calls are merged into the outermost one.

        Args:
            is_bowden_move (bool): The body moves the filament between FINDA
                and the extruder, report ``bowden_progress`` meanwhile.

        Yields:
            None: Control to the body.
        """
        if self.is_tracking:
            yield
            return
        self._start = (self._queued_steps(), self._stored_position())
        self._is_bowden_move = is_bowden_move
        try:
            yield
            _, start_position = self._start
            position = max(0.0, start_position + self._moved(self._queued_steps()))
            self._measured = (self.mmu.filament_pos, position)
        finally:
            self._start = None
            self._is_bowden_move = False


def tracks_filament(is_bowden_move: bool = False) -> Callable:
    """Decorator factory that follows the filament while the method runs.

    See :meth:`FilamentTracker.track`.

    Args:
        is_bowden_move (bool): The method moves the filament between FINDA
            and the extruder.

    Returns:
        Callable: The actual decorator.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped_f(self: MMU, *args, **kwargs) -> bool:
            with self.filament_tracker.track(is_bowden_move):
                return f(self, *args, **kwargs)

        return wrapped_f

    return decorator


class MMU:
    """MMU3 class to manage the MMU3 multi-material unit.

    Args:
        config (ConfigWrapper): The configuration wrapper.
    """

    def __init__(self, config: ConfigWrapper) -> None:
        # recovery state: what the operator is currently asking for
        # (current_operation) and what failed and is waiting to be retried
        # (pending_operation). Both are in-memory only and cleared on a
        # Klipper restart.
        self.current_operation: None | Operation = None
        self.pending_operation: None | Operation = None

        self.printer: Printer = config.get_printer()
        self.gcode: GCodeDispatch = self.printer.lookup_object("gcode")
        self.gcode_move: None | GCodeMove = None
        self.query_endstops: QueryEndstops = self.printer.load_object(
            config, "query_endstops"
        )
        self.reactor: Reactor = self.printer.get_reactor()

        self.mcu: None | MCU_endstop = None
        self.toolhead: None | ToolHead = None
        self.motion_queuing: None | PrinterMotionQueuing = None
        self.extruder: None | PrinterExtruder = None
        self.extruder_heater: None | Heater = None
        self.heaters: None | PrinterHeaters = None
        self.idler_stepper: None | ManualStepper = None
        self._idler_stepper_endstop = None
        self.pulley_stepper: None | ManualStepper = None
        self.pulley_stepper_endstop: None | MCU_endstop = None
        self.selector_stepper: None | ManualStepper = None
        self.selector_stepper_endstop: None | MCU_endstop = None
        self.display_status: None | DisplayStatus = None
        self.filament_switch_sensor: None | SwitchSensor = None
        self.filament_switch_sensor_position: None | FilamentSwitchSensorPosition = None
        self.filament_motion_sensor: None | EncoderSensor = None

        # state variables
        self.debug = False

        self.is_paused = False
        self.is_homed = False
        self.is_enabled = True
        self.extruder_temp = None
        # A gate is a physical lane of the MMU, a tool is what the slicer asks
        # for (Tn). ttg_map[n] is the gate tool n loads.
        # the tool last asked for, it names the loaded gate when more than
        # one tool maps to it (see gate_to_tool())
        self.selected_tool = None
        # the gate the selector / idler is at
        self.current_gate = None
        # the gate whose filament is in the path (FINDA or further)
        self.loaded_gate = None
        # how far the filament tip has moved from the MMU toward the nozzle
        self.filament_pos = FilamentPos.UNLOADED
        # FINDA's state, kept current by the MCU reporting every change of
        # its pin (reported to the MMU panel, get_status() must not query FINDA)
        self.finda_triggered = False
        # how far, in mm, the filament tip is from FINDA
        self.filament_tracker = FilamentTracker(self)
        # what the MMU is doing right now, in Happy Hare's vocabulary
        # (reported to the Mainsail / Fluidd MMU panel)
        self.action = ACTION_IDLE

        # statistics
        self.total_stats = OperationStats()
        self.job_stats = OperationStats()
        self.save_variables = None
        self.print_stats = None
        self._print_stats_state = "standby"
        # Happy Hare's print_state, set by MMU_PRINT_START / MMU_PRINT_END and
        # by following print_stats
        self.print_state = PRINT_STATE_READY

        # load config values
        # are we in debug mode
        self.debug = config.getboolean("debug", False)
        self.number_of_tools = config.getint("number_of_tools", 5)

        # per gate filament metadata, persisted via save_variables
        self.gate_map = GateMap(self.number_of_tools)
        # the tool-to-gate map, persisted via save_variables
        self.ttg_map = default_ttg_map(self.number_of_tools)
        # the tools the print uses, set by MMU_SLICER_TOOL_MAP in the start
        # G-code and cleared when the print ends
        self.slicer_tool_map = SlicerToolMap()
        # endless spool: on a runout, continue the tool with the next gate of
        # the same group. The config values are the defaults, MMU_ENDLESS_SPOOL
        # changes are persisted via save_variables and win over them.
        self.default_endless_spool_enabled = config.getboolean(
            "endless_spool_enabled", False
        )
        self.default_endless_spool_groups = config.getintlist(
            "endless_spool_groups",
            default_endless_spool_groups(self.number_of_tools),
            count=self.number_of_tools,
        )
        if not is_valid_endless_spool_groups(
            self.number_of_tools, self.default_endless_spool_groups
        ):
            raise config.error(
                "endless_spool_groups needs one non-negative group per gate"
            )
        self.endless_spool_enabled = self.default_endless_spool_enabled
        self.endless_spool_groups = list(self.default_endless_spool_groups)
        # A runout seen by a sensor before the filament switch sensor (e.g. a
        # motion sensor) with FINDA empty is handled once the end of the
        # filament reaches the switch sensor. The extruder may use this much
        # filament until then, the print pauses otherwise (the end of the
        # filament is stuck).
        self.runout_tail_length = config.getfloat(
            "runout_tail_length", 100.0, above=0.0
        )
        # the gate whose runout waits for the switch sensor, and the extruder
        # position when it was seen
        self.runout_tail_gate: None | int = None
        self.runout_tail_start = 0.0
        self._runout_tail_timer = None
        # True during an endless spool tool change, Happy Hare's ``runout``
        self.is_handling_runout = False
        # the _MMU_FORM_TIP_VARS values before the first MMU_TEST_FORM_TIP
        # override, restored by MMU_TEST_FORM_TIP RESET=1
        self.form_tip_defaults: None | dict = None
        # the tip of the loaded filament was formed / cut, the unload doesn't
        # do it again (MMU_FORM_TIP / MMU_CUT before a tool change)
        self.tip_formed = False
        # the Happy Hare shaped part of get_status(), read by the Mainsail /
        # Fluidd MMU panel
        self.hh_status = MmuStatus(self)
        # the Mainsail / Fluidd MMU panel is always enabled now, the option is
        # still read so configs that set it keep working
        if config.get("enable_mmu_panel", None) is not None:
            logger.warning(
                "mmu: enable_mmu_panel is no longer used, the MMU panel is "
                "always enabled. Remove it from [mmu]."
            )
        # Spoolman
        self.spoolman_support = config.getchoice(
            "spoolman_support",
            list(SPOOLMAN_SUPPORT_VALUES),
            SPOOLMAN_READONLY,
        )
        # start / end the print job when print_stats starts / ends a print,
        # turn off if the start / end G-code calls MMU_PRINT_START / _END
        self.print_start_detection = config.getboolean("print_start_detection", True)
        # the spool last sent to Moonraker, NO_SPOOL forces the first sync
        self._active_spool_id: None | int = NO_SPOOL
        self._spoolman_error_reported = False

        # timeouts
        self.timeout_pause = config.getint("timeout_pause", 36000)
        self.disable_heater = config.getint("disable_heater", 600)
        self.pulley_calibrate_pause_duration = config.getint(
            "pulley_calibrate_pause_duration", 10
        )
        self.pulley_calibrate_filament_length = config.getfloat(
            "pulley_calibrate_filament_length", 100
        )
        # bowden load
        self.bowden_load_length1 = config.getint("bowden_load_length1", 450)
        self.bowden_load_length2 = config.getint("bowden_load_length2", 20)
        self.bowden_load_length3 = config.getint("bowden_load_length3", 20)
        self.bowden_load_speed1 = config.getint("bowden_load_speed1", 120)
        self.bowden_load_speed2 = config.getint("bowden_load_speed2", 60)
        self.bowden_load_accel1 = config.getint("bowden_load_accel1", 80)
        self.bowden_load_accel2 = config.getint("bowden_load_accel2", 80)
        # bowden unload
        self.bowden_unload_length = config.getfloat("bowden_unload_length", 830)
        self.bowden_unload_speed = config.getint("bowden_unload_speed", 120)
        self.bowden_unload_accel = config.getint("bowden_unload_accel", 120)
        # hotend unload
        self.hotend_unload_length = config.getfloat("hotend_unload_length", 50)
        self.hotend_unload_speed = config.getint("hotend_unload_speed", 100)
        # FINDA load/unload
        self.finda_load_retry = config.getint("finda_load_retry", 20)
        self.finda_load_length = config.getfloat("finda_load_length", 120)
        self.finda_unload_retry = config.getint("finda_unload_retry", 10)
        self.finda_unload_length = config.getfloat("finda_unload_length", 30)
        self.finda_load_speed = config.getint("finda_load_speed", 20)
        self.finda_unload_speed = config.getint("finda_unload_speed", 20)
        self.finda_load_accel = config.getint("finda_load_accel", 50)
        self.finda_unload_accel = config.getint("finda_unload_accel", 50)
        # cut in the MMU
        self.cut_filament_length = config.getfloat("cut_filament_length", 20)
        self.cutting_edge_retract = config.getfloat("cutting_edge_retract", 5)
        self.cut_stepper_current = config.getfloat("cut_stepper_current", 1.0)
        # cut in extruder
        self.enable_filament_cutter = config.getboolean("enable_filament_cutter", False)
        # tip forming on unload, Happy Hare's name for it
        self.force_form_tip_standalone = config.getboolean(
            "force_form_tip_standalone", False
        )
        self.extra_load_length = config.getfloat("extra_load_length", 0)
        self.extra_load_speed = config.getfloat("extra_load_speed", 10)
        self.travel_speed = config.getfloat("travel_speed", 100)
        # selector
        self.selector_speed = config.getfloat("selector_speed", 35)
        self.selector_homing_speed = config.getfloat("selector_homing_speed", 20)
        self.selector_homing_speed_slow = config.getfloat(
            "selector_homing_speed_slow", 5
        )
        self.selector_homing_move_length = config.getfloat(
            "selector_homing_move_length", -76
        )
        self.selector_accel = config.getfloat("selector_accel", 200)
        self.selector_positions = config.getfloatlist(
            "selector_positions",
            [73.5, 59.375, 45.25, 31.125, 17, 0],
        )
        # idler
        self.idler_positions = config.getfloatlist(
            "idler_positions",
            [5, 20, 35, 50, 65, 85],
        )
        self.idler_homing_move_lengths = config.getfloatlist(
            "idler_homing_move_lengths",
            [7, -95],
        )
        self.idler_homing_speed = config.getfloat("idler_homing_speed", 100)
        self.idler_homing_accel = config.getfloat("idler_homing_accel", 80)
        self.idler_speed = config.getfloat("idler_speed", 100)
        self.idler_accel = config.getfloat("idler_accel", 80)

        self.pulley_load_to_extruder_speed = config.getint(
            "pulley_load_to_extruder_speed", 10
        )
        # pause values
        self.pause_before_disabling_steppers = (
            config.getint("pause_before_disabling_steppers", 100) / 1000.0
        )
        self.pause_after_disabling_steppers = (
            config.getint("pause_after_disabling_steppers", 250) / 1000.0
        )
        self.pause_position = config.getfloatlist("pause_position", [0, 200, 10])
        # temperature
        self.min_temp_extruder = config.getint("min_temp_extruder", 180)
        self.extruder_eject_temp = config.getint("extruder_eject_temp", 200)
        # other options
        self.enable_no_selector_mode: bool = config.getboolean(
            "enable_no_selector_mode",
            False,
        )
        self.load_retry = config.getint("load_retry", 5)
        self.unload_retry = config.getint("unload_retry", 5)
        self.tool_change_retry = config.getint("tool_change_retry", 5)
        self.filament_switch_sensor_position = (
            FilamentSwitchSensorPosition.to_switch_sensor_position(
                config.get(
                    "filament_switch_sensor_position",
                    FilamentSwitchSensorPosition.OnGears,
                )
            )
        )
        self.filament_switch_sensor_name = config.get(
            "filament_switch_sensor_name",
            "filament_switch_sensor my_filament_sensor",
        )

        self.filament_motion_sensor_name = config.get(
            "filament_motion_sensor_name",
            "filament_motion_sensor encoder_sensor",
        )

        self.setup_finda_sensor(config)

        # register commands
        self.register_commands()
        self.register_mmu_panel()
        self.printer.register_event_handler("klippy:connect", self._connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _connect(self) -> None:
        """Handle klippy:connect event."""
        self.toolhead: ToolHead = self.printer.lookup_object("toolhead")
        self.motion_queuing = self.printer.lookup_object("motion_queuing")
        self.gcode_move: GCodeMove = self.printer.lookup_object("gcode_move")
        self.extruder: PrinterExtruder = self.toolhead.get_extruder()
        self.heaters: PrinterHeaters = self.printer.lookup_object("heaters")
        self.extruder_heater: Heater = self.heaters.lookup_heater("extruder")
        self.idler_stepper: ManualStepper = self.printer.lookup_object(
            IDLER_STEPPER_NAME
        )
        self.pulley_stepper: ManualStepper = self.printer.lookup_object(
            PULLEY_STEPPER_NAME
        )
        self.pulley_stepper_endstop: MCU_endstop = self.get_endstop(PULLEY_STEPPER_NAME)
        self.selector_stepper: ManualStepper = self.printer.lookup_object(
            SELECTOR_STEPPER_NAME
        )
        self.selector_stepper_endstop: MCU_endstop = self.get_endstop(
            SELECTOR_STEPPER_NAME
        )
        self.mcu: MCU_endstop = self.pulley_stepper_endstop.get_mcu()
        self.filament_switch_sensor: SwitchSensor = self.printer.lookup_object(
            self.filament_switch_sensor_name
        )

        with contextlib.suppress(configparser.Error):
            self.filament_motion_sensor: EncoderSensor = self.printer.lookup_object(
                self.filament_motion_sensor_name
            )
        self.display_status: DisplayStatus = self.printer.lookup_object(
            "display_status"
        )

        self.check_tip_macros()

        self.save_variables = self.printer.lookup_object("save_variables", None)
        if self.save_variables is not None:
            self.total_stats = OperationStats.from_dict(
                self.load_variable(TOTAL_STATS_VARIABLE)
            )
            self.gate_map = GateMap.from_dict(
                self.number_of_tools,
                self.load_variable(GATE_MAP_VARIABLE),
            )
            self.ttg_map = ttg_map_from_saved(
                self.number_of_tools,
                self.save_variables.allVariables.get(TTG_MAP_VARIABLE),
            )
            self.load_endless_spool(self.save_variables.allVariables)
        else:
            self.respond_info(
                "[save_variables] is not configured - MMU3 lifetime "
                "statistics, the gate map, the tool-to-gate map and the "
                "endless spool settings will not persist across restarts."
            )

        self._runout_tail_timer = self.reactor.register_timer(self._check_runout_tail)

        self.print_stats = self.printer.lookup_object("print_stats", None)
        if self.print_stats is not None:
            self.reactor.register_timer(
                self._poll_print_stats,
                self.reactor.NOW,
            )

    def _poll_print_stats(self, eventtime: float) -> float:
        """Follow ``print_stats`` even when nothing reads the MMU status.

        Args:
            eventtime (float): The reactor event time.

        Returns:
            float: The next time this timer should fire.
        """
        self.follow_print_stats(eventtime)
        return eventtime + PRINT_STATS_POLL_INTERVAL

    @property
    def is_in_print(self) -> bool:
        """Return True while a print job is printing or paused.

        Returns:
            bool: True if a print is in progress.
        """
        return self.print_state in IN_PRINT_STATES

    def on_print_start(self) -> None:
        """Start a print job, reset the job stats and switch to ``printing``.

        Does nothing while a print is in progress, so ``MMU_PRINT_START`` and
        the ``print_stats`` detection of the same print reset the job stats
        only once.
        """
        if self.is_in_print:
            return
        self.job_stats.reset()
        self.print_state = PRINT_STATE_PRINTING

    def on_print_end(self, state: str) -> None:
        """End the print job with the given ``print_state``.

        Does nothing if no print is in progress, so ``MMU_PRINT_END`` and the
        ``print_stats`` detection of the same print end it only once.

        Args:
            state (str): The end state, e.g. ``complete`` or ``cancelled``.
        """
        if not self.is_in_print:
            return
        self.print_state = state
        self.slicer_tool_map.reset()

    def follow_print_stats(self, eventtime: float) -> None:
        """Update ``print_state`` when the ``print_stats`` state changes.

        Pause and resume are always followed. A print start / end only with
        ``print_start_detection`` on, otherwise ``MMU_PRINT_START`` /
        ``MMU_PRINT_END`` start and end the print job.

        Args:
            eventtime (float): The current event time.
        """
        if self.print_stats is None:
            return
        state = self.print_stats.get_status(eventtime).get("state", "standby")
        if state == self._print_stats_state:
            return
        self._print_stats_state = state
        if state == "printing":
            if self.print_state == PRINT_STATE_PAUSED:
                self.print_state = PRINT_STATE_PRINTING
            elif self.print_start_detection:
                self.on_print_start()
        elif state == "paused":
            if self.print_state == PRINT_STATE_PRINTING:
                self.print_state = PRINT_STATE_PAUSED
        else:
            end_state = PRINT_STATE_MAP.get(state, PRINT_STATE_READY)
            if self.is_in_print:
                if self.print_start_detection:
                    self.on_print_end(end_state)
            elif end_state == PRINT_STATE_READY:
                # print_stats was reset after the print ended
                self.print_state = PRINT_STATE_READY

    def save_gate_map(self) -> None:
        """Persist ``gate_map`` via ``save_variables``, if configured."""
        if self.save_variables is None:
            return
        # see save_total_stats(), gate names are sanitized from single quotes
        # in MMU_GATE_MAP so the JSON nests inside the single-quoted VALUE
        value = json.dumps(self.gate_map.to_dict())
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={GATE_MAP_VARIABLE} VALUE='{value}'"
        )

    def save_ttg_map(self) -> None:
        """Persist ``ttg_map`` via ``save_variables``, if configured."""
        if self.save_variables is None:
            return
        value = json.dumps(self.ttg_map)
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={TTG_MAP_VARIABLE} VALUE='{value}'"
        )

    def set_ttg_map(self, ttg_map: list[int]) -> None:
        """Replace the tool-to-gate map, saving it if it changed.

        Args:
            ttg_map (list[int]): The new map, ``ttg_map[tool]`` is the gate.
        """
        if ttg_map == self.ttg_map:
            return
        # a new list, so Klipper's change detection pushes it to Moonraker
        self.ttg_map = list(ttg_map)
        self.save_ttg_map()

    def tool_to_gate(self, tool: None | int) -> None | int:
        """Return the gate a tool loads.

        Args:
            tool (None | int): The tool.

        Returns:
            None | int: The mapped gate, None if the tool is None. An invalid
                tool is returned as is, for the caller to reject.
        """
        if tool is None:
            return None
        return map_tool_to_gate(tool, self.ttg_map)

    def gate_to_tool(self, gate: None | int) -> None | int:
        """Return the tool a gate stands for.

        The selected tool (the one last asked for) if it maps to the gate,
        else the first tool that does.

        Args:
            gate (None | int): The gate.

        Returns:
            None | int: The tool, None if no tool maps to the gate.
        """
        if gate is None:
            return None
        if (
            self.selected_tool is not None
            and self.tool_to_gate(self.selected_tool) == gate
        ):
            return self.selected_tool
        for tool, mapped_gate in enumerate(self.ttg_map):
            if mapped_gate == gate:
                return tool
        return None

    @property
    def loaded_tool(self) -> None | int:
        """Return the tool whose filament is in the path.

        Returns:
            None | int: The tool, None if nothing is loaded or no tool maps to
                the loaded gate.
        """
        return self.gate_to_tool(self.loaded_gate)

    def load_endless_spool(self, variables: dict) -> None:
        """Read the endless spool settings saved with ``save_variables``.

        Invalid or missing values keep the config defaults.

        Args:
            variables (dict): The saved variables.
        """
        enabled = variables.get(ENDLESS_SPOOL_ENABLED_VARIABLE)
        if enabled in (0, 1):
            self.endless_spool_enabled = bool(enabled)
        self.endless_spool_groups = endless_spool_groups_from_saved(
            self.number_of_tools,
            variables.get(ENDLESS_SPOOL_GROUPS_VARIABLE),
            self.default_endless_spool_groups,
        )

    def save_endless_spool(self) -> None:
        """Persist the endless spool settings via ``save_variables``."""
        if self.save_variables is None:
            return
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={ENDLESS_SPOOL_ENABLED_VARIABLE} "
            f"VALUE={int(self.endless_spool_enabled)}"
        )
        value = json.dumps(self.endless_spool_groups)
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={ENDLESS_SPOOL_GROUPS_VARIABLE} VALUE='{value}'"
        )

    def set_endless_spool(
        self, enabled: None | bool = None, groups: None | list[int] = None
    ) -> None:
        """Change the endless spool settings, saving them if they changed.

        Args:
            enabled (None | bool): Enable / disable endless spool, None keeps
                the current state.
            groups (None | list[int]): The group of each gate, None keeps the
                current groups.
        """
        changed = False
        if enabled is not None and bool(enabled) != self.endless_spool_enabled:
            self.endless_spool_enabled = bool(enabled)
            changed = True
        if groups is not None and list(groups) != self.endless_spool_groups:
            # a new list, so Klipper's change detection pushes it to Moonraker
            self.endless_spool_groups = list(groups)
            changed = True
        if changed:
            self.save_endless_spool()

    def next_endless_spool_gate(self, gate: int) -> tuple[None | int, list[int]]:
        """Return the gate endless spool continues with after ``gate``.

        See :func:`next_endless_spool_gate`.

        Args:
            gate (int): The gate that ran out.

        Returns:
            tuple[None | int, list[int]]: The next gate (None if no gate is
                left) and the gates of the group that were checked.
        """
        return next_endless_spool_gate(
            gate, self.endless_spool_groups, self.gate_map.statuses()
        )

    def set_gate_status(self, gate: None | int, status: int) -> None:
        """Record an observed gate availability, saving it if it changed.

        Args:
            gate (None | int): The gate, ignored if None or invalid.
            status (int): GATE_UNKNOWN, GATE_EMPTY or GATE_AVAILABLE.
        """
        if not self.gate_map.is_valid_gate(gate):
            return
        if self.gate_map.update(gate, status=status):
            with contextlib.suppress(Exception):
                self.save_gate_map()

    def sync_active_spool(self, quiet: bool = False) -> None:
        """Point Moonraker's active Spoolman spool at the loaded gate's spool.

        Moonraker attributes the extruded filament to the active spool, so
        switching it on every load / unload tracks the usage per spool. The
        spool is only considered active once the filament reached the
        extruder, it is cleared (``None``) otherwise. Moonraker is only called
        when the wanted spool changes.

        Args:
            quiet (bool): Do not report a missing Moonraker ``[spoolman]``
                section to the console (used on startup, when Moonraker may
                not be connected yet).
        """
        if self.spoolman_support == SPOOLMAN_OFF:
            return
        spool_id = None
        gate = self.loaded_gate
        if self.filament_pos >= FilamentPos.AT_EXTRUDER and self.gate_map.is_valid_gate(
            gate
        ):
            gate_spool_id = self.gate_map[gate].spool_id
            if gate_spool_id != NO_SPOOL:
                spool_id = gate_spool_id
        if spool_id == self._active_spool_id:
            return

        webhooks = self.printer.lookup_object("webhooks")
        try:
            webhooks.call_remote_method("spoolman_set_active_spool", spool_id=spool_id)
        except self.printer.command_error as e:
            # Moonraker is not connected or has no [spoolman] section, keep
            # _active_spool_id as is so the next sync retries
            self.respond_debug(f"Setting the active spool failed: {e}")
            if not quiet and not self._spoolman_error_reported:
                self._spoolman_error_reported = True
                self.respond_info(
                    "Could not set the active Spoolman spool, is [spoolman] "
                    "configured in moonraker.conf? (Set `spoolman_support: off` "
                    "to silence this.)"
                )
            return
        self._active_spool_id = spool_id
        self.respond_debug(f"Active spool: {spool_id}")

    @contextlib.contextmanager
    def running_action(self, action: str) -> Iterator[None]:
        """Report ``action`` as what the MMU is doing for the duration.

        The previous action is restored on exit, so nested actions (e.g.
        "Selecting" inside "Loading") report the innermost one.

        Args:
            action (str): One of the Happy Hare ``ACTION_*`` strings.

        Yields:
            None: Nothing.
        """
        previous = getattr(self, "action", ACTION_IDLE)
        self.set_action(action)
        try:
            yield
        finally:
            self.set_action(previous)

    def set_action(self, action: str) -> None:
        """Set ``action`` and call ``_MMU_ACTION_CHANGED`` if it changed.

        As in Happy Hare the macro gets ``ACTION`` and ``OLD_ACTION``, and an
        error in it is only reported, it doesn't fail the running operation.

        Args:
            action (str): One of the Happy Hare ``ACTION_*`` strings.
        """
        old_action = getattr(self, "action", ACTION_IDLE)
        self.action = action
        if action == old_action or not self.is_macro_defined(ACTION_CHANGED_MACRO):
            return
        try:
            self.gcode.run_script_from_command(
                f"{ACTION_CHANGED_MACRO} ACTION='{action}' OLD_ACTION='{old_action}'"
            )
        except self.printer.command_error as e:
            self.respond_info(f"{ACTION_CHANGED_MACRO} failed: {e}")

    def check_tip_macros(self) -> None:
        """Stop Klipper if a tip forming / cutting macro is missing.

        ``_MMU_FORM_TIP`` is always needed, ``_MMU_CUT_TIP`` only with
        ``enable_filament_cutter``. They were called ``RAMMING_SLICER`` and
        ``CUT_FILAMENT_IN_EXTRUDER`` before, a config copied from an older
        ``mmu.cfg`` still has the old names.

        Raises:
            configfile.error: If a needed macro is not defined.
        """
        required = [(FORM_TIP_MACRO, "RAMMING_SLICER")]
        if self.enable_filament_cutter:
            required.append((CUT_TIP_MACRO, "CUT_FILAMENT_IN_EXTRUDER"))
        for name, old_name in required:
            if not self.is_macro_defined(name):
                raise self.printer.config_error(
                    f"[gcode_macro {name}] is not defined. [gcode_macro "
                    f"{old_name}] was renamed to [gcode_macro {name}], rename "
                    "it in your mmu.cfg."
                )

    def is_macro_defined(self, name: str) -> bool:
        """Whether a ``[gcode_macro <name>]`` is defined.

        Args:
            name (str): The macro name.

        Returns:
            bool: True if the macro is defined.
        """
        return self.printer.lookup_object(f"gcode_macro {name}", None) is not None

    def run_user_macro(self, name: str) -> bool:
        """Run the optional user macro ``name`` and wait for its moves.

        Does nothing if the macro isn't defined. A failing macro fails the
        calling load / unload like a failing MMU step, so ``auto_pause`` and
        the recovery work the same.

        Args:
            name (str): The macro name, one of the ``*_MACRO`` constants.

        Returns:
            bool: False if the macro raised an error, True otherwise.
        """
        if not self.is_macro_defined(name):
            return True
        self.respond_debug(f"Running {name}")
        try:
            self.gcode.run_script_from_command(name)
            self.toolhead.wait_moves()
        except self.printer.command_error as e:
            error = f"{name} failed: {e}"
            self.display_status_msg(error)
            if self.current_operation is not None:
                self.current_operation.error = error
            return False
        return True

    def save_total_stats(self) -> None:
        """Persist ``total_stats`` via ``save_variables``, if configured."""
        if self.save_variables is None:
            return
        # SAVE_VARIABLE's VALUE is re-parsed with ast.literal_eval(), which
        # accepts JSON's double-quoted syntax (json.dumps() never emits a
        # single quote, so it nests cleanly inside the single-quoted VALUE).
        value = json.dumps(self.total_stats.to_dict())
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={TOTAL_STATS_VARIABLE} VALUE='{value}'"
        )

    def _handle_ready(self) -> None:
        """Handle klippy:ready - reconcile the tracked state with the sensors.

        In-memory state is lost on a restart, so on the way up derive the real
        filament position from the sensors (e.g. after a firmware restart in the
        middle of a print the filament is still physically loaded).

        The ``klippy:ready`` handlers run under ``reactor.assert_no_pause()``,
        but :meth:`assess_filament_pos` queries the FINDA endstop and flushes the
        lookahead, both of which pause the reactor. Defer the assessment to a
        reactor callback that fires once the main event loop is running, where
        pausing is allowed. A failure there must never disturb startup.
        """
        self.reactor.register_callback(self._assess_filament_pos_on_ready)

    def _assess_filament_pos_on_ready(self, eventtime: float) -> None:
        """Run the deferred startup filament-position assessment.

        Args:
            eventtime (float): The reactor event time (unused).
        """
        with contextlib.suppress(Exception):
            self.assess_filament_pos()
        with contextlib.suppress(Exception):
            self.sync_active_spool(quiet=True)

    def load_variable(self, name: str) -> dict:
        """Return a saved variable, falling back to its pre-rename name.

        Args:
            name (str): The variable name.

        Returns:
            dict: The saved value, or an empty dict if neither name is saved.
        """
        variables = self.save_variables.allVariables
        if name in variables:
            return variables[name]
        return variables.get(LEGACY_VARIABLES.get(name), {})

    def get_status(self, event_time: float) -> dict:
        """Return the ``printer.mmu`` status.

        The Happy Hare fields the Mainsail / Fluidd MMU panel reads (see
        :class:`MmuStatus`), plus the MMU3 specific ones. ``filament_pos`` is
        Happy Hare's integer, the MMU3 position name is ``filament_pos_name``.

        Args:
            event_time (float): The current event time.

        Returns:
            dict: The status of the MMU3.
        """
        status = self.hh_status.get_status(event_time)
        status.update(
            {
                "is_enabled": self.is_enabled,
                "current_gate": self.current_gate,
                "loaded_gate": self.loaded_gate,
                # the pre tool / gate split names of current_gate and
                # loaded_gate, kept for user macros that read them
                "current_tool": self.current_gate,
                "current_filament": self.loaded_gate,
                "filament_pos_name": self.filament_pos.name,
                "pending_operation": (
                    self.pending_operation.describe()
                    if self.pending_operation is not None
                    else None
                ),
                "total_stats": self.total_stats.to_dict(),
                "job_stats": self.job_stats.to_dict(),
                "gate_map": self.gate_map.to_dict(),
            }
        )
        return status

    def respond_info(self, msg: str) -> None:
        """Respond info through the current GCodeCommand instance.

        Args:
            msg (str): The info message.
        """
        self.gcode.respond_info(f"MMU3: {msg}")

    def respond_debug(self, msg: str) -> None:
        """Respond debug through the current GCodeCommand instance.

        Args:
            msg (str): The debug message.
        """
        if not self.debug:
            return
        self.gcode.respond_info(f"MMU3: {msg}")

    def display_status_msg(self, msg: str) -> None:
        """Display the given status message in the LCD display."""
        # also send the message to the console
        self.respond_info(msg)
        self.gcode.run_script_from_command(f"M117 {msg}")

    def command_table(self) -> list[tuple[str, Callable, str]]:
        """Return the MMU3 commands with a one-line description each.

        The descriptions show up in Klipper's ``HELP`` and in ``MMU_HELP``.
        The per tool ``Tn`` / ``Kn`` commands and the unsupported Happy Hare
        commands are not in the table.

        Returns:
            list[tuple[str, Callable, str]]: (name, handler, description).
        """
        return [
            ("MMU", self.cmd_mmu, "Enable / disable the MMU (ENABLE=0|1)"),
            ("MMU_HELP", self.cmd_mmu_help, "List the MMU commands"),
            ("MMU_STATUS", self.cmd_mmu_status, "Print a summary of the MMU state"),
            ("MMU_HOME", self.cmd_home_mmu, "Home the idler and the selector"),
            ("HOME_IDLER", self.cmd_home_idler, "Home the idler only"),
            ("MMU_SELECT", self.cmd_mmu_select, "Select a gate (GATE= / TOOL=)"),
            ("MMU_UNSELECT", self.cmd_unselect_gate, "Park the idler"),
            (
                "MMU_CHANGE_TOOL",
                self.cmd_mmu_change_tool,
                "Change to a tool (TOOL= / GATE=), same as Tn",
            ),
            (
                "MMU_LOAD",
                self.cmd_mmu_load,
                "Load the filament of a gate (GATE= / TOOL=) to the nozzle",
            ),
            (
                "MMU_UNLOAD",
                self.cmd_mmu_unload,
                "Unload the filament from the nozzle to the MMU",
            ),
            (
                "MMU_EJECT",
                self.cmd_mmu_eject,
                "Unload the filament and park the idler",
            ),
            (
                "MMU_PRELOAD",
                self.cmd_mmu_preload,
                "Feed the filament of a gate to FINDA and back",
            ),
            (
                "MMU_CHECK_GATE",
                self.cmd_mmu_check_gate,
                "Check the selected (or given) gates for filament",
            ),
            (
                "MMU_CHECK_GATES",
                self.cmd_mmu_check_gates,
                "Check all (or the given) gates for filament",
            ),
            (
                "MMU_GATE_MAP",
                self.cmd_mmu_gate_map,
                "Show or edit the filament metadata of the gates",
            ),
            (
                "MMU_TTG_MAP",
                self.cmd_mmu_ttg_map,
                "Show or edit the tool-to-gate map (TOOL= GATE= / MAP= / RESET=1)",
            ),
            (
                "MMU_REMAP_TTG",
                self.cmd_mmu_ttg_map,
                "Same as MMU_TTG_MAP",
            ),
            (
                "MMU_ENDLESS_SPOOL",
                self.cmd_mmu_endless_spool,
                "Show or edit endless spool (ENABLE= / GROUPS= / RESET=1)",
            ),
            (
                "MMU_SLICER_TOOL_MAP",
                self.cmd_mmu_slicer_tool_map,
                "Show or set the tools the print uses (from the start G-code)",
            ),
            (
                "MMU_RUNOUT",
                self.cmd_mmu_runout,
                "Handle a runout: endless spool or pause (add to runout_gcode)",
            ),
            (
                "MMU_UNLOCK",
                self.cmd_unlock,
                "Park the idler so the filament can be moved by hand",
            ),
            (
                "MMU_RETRY",
                self.cmd_mmu_retry,
                "Retry the operation that failed and paused the MMU",
            ),
            ("MMU_RECOVER", self.cmd_mmu_recover, "Recover the MMU state"),
            ("PAUSE_MMU", self.cmd_pause, "Pause the MMU and the print"),
            (
                "RESUME_MMU",
                self.cmd_resume,
                "Retry the failed operation and resume the print (FORCE=1)",
            ),
            ("MMU_MOTORS_OFF", self.cmd_motors_off, "Turn off the MMU motors"),
            (
                "MMU_FORM_TIP",
                self.cmd_mmu_form_tip,
                "Form the tip of the loaded filament (ramming)",
            ),
            (
                "MMU_TEST_FORM_TIP",
                self.cmd_mmu_form_tip,
                "Tune and run the tip forming (SHOW=1, RESET=1, RUN=0, VAR=value)",
            ),
            (
                "MMU_CUT",
                self.cmd_mmu_cut,
                "Cut the loaded filament in the extruder",
            ),
            (
                "MMU_PRINT_START",
                self.cmd_mmu_print_start,
                "Start the print job (add to the print start G-code)",
            ),
            (
                "MMU_PRINT_END",
                self.cmd_mmu_print_end,
                "End the print job (STATE=complete|cancelled|error|ready|standby)",
            ),
            (
                "MMU_STATS",
                self.cmd_mmu_stats,
                "Print the lifetime and current job statistics",
            ),
            (
                "MMU_STATS_RESET_JOB",
                self.cmd_mmu_stats_reset_job,
                "Reset the current job statistics",
            ),
            (
                "MMU_CALIBRATE_PULLEY_ROTATION_DISTANCE",
                self.cmd_calibrate_pulley_rotation_distance,
                "Calibrate the pulley rotation_distance",
            ),
            (
                "MMU_CALIBRATE_BOWDEN_LENGTH",
                self.cmd_calibrate_bowden_load_length,
                "Detect bowden_load_length1 using the filament switch sensor",
            ),
            (
                "ENDSTOPS_STATUS",
                self.cmd_endstops_status,
                "Print the state of the MMU endstops",
            ),
            ("MMU_GET_PARAM", self.cmd_mmu_get_param, "Print an MMU parameter"),
            ("MMU_SET_PARAM", self.cmd_mmu_set_param, "Set an MMU parameter"),
            ("M702", self.cmd_m702, "Unload the filament"),
        ]

    def register_commands(self) -> None:
        """Register new GCode commands."""
        for name, handler, desc in self.command_table():
            self.gcode.register_command(name, handler, desc=desc)

        for i in range(self.number_of_tools):
            self.gcode.register_command(f"T{i}", partial(self.cmd_tx, tool_id=i))
            self.gcode.register_command(f"K{i}", partial(self.cmd_kx, tool_id=i))

        # Happy Hare commands without an MMU3 equivalent
        for name in (
            "MMU_SPOOLMAN",
            "MMU_SYNC_GEAR_MOTOR",
            "MMU_MOTORS_ON",
            # the panels' "T macro color" setting, the MMU3's T commands are
            # not macros
            "MMU_TEST_CONFIG",
        ):
            self.gcode.register_command(
                name, partial(self.cmd_not_supported, name=name)
            )

    def register_mmu_panel(self) -> None:
        """Expose the MMU3 to the Mainsail / Fluidd MMU panel.

        The panels look for Happy Hare's ``mmu`` / ``mmu_machine`` objects.
        ``[mmu]`` makes this instance the ``mmu`` object (see
        :meth:`get_status`), the Happy Hare commands the panels send are
        registered in :meth:`register_commands`.
        """
        self.printer.add_object("mmu_machine", MmuMachine(self))

    def get_endstop(self, endstop_name: str) -> None | MCU_endstop:
        """Return the endstop with the given name.

        Args:
            endstop_name (str): The name of the endstop.

        Returns:
            None | MCU_endstop: The requested endstop if found, else None.
        """
        for endstop in self.query_endstops.endstops:
            if endstop[1] == endstop_name:
                return endstop[0]
        return None

    def get_extruder_temperature(self) -> float:
        """Return the current extruder temperature.

        Returns:
            float: The current extruder temperature.
        """
        print_time = self.toolhead.get_last_move_time()
        smooth_temp, target_temp = self.extruder_heater.get_temp(print_time)
        return smooth_temp if smooth_temp != 0 else target_temp

    def is_filament_in_switch_sensor(self) -> bool:
        """Check if the filament present in the filament switch sensor.

        Returns:
            bool: True if filament sensor is triggered, False otherwise.
        """
        return self.filament_switch_sensor.get_status(None)["filament_detected"]

    def is_filament_moving(self) -> bool:
        """Check if the filament is moving according to the filament motion sensor.

        Returns:
            bool: True if the filament is moving, False otherwise.
        """
        if not self.filament_motion_sensor:
            # no filament motion sensor,
            # assume it is always moving to avoid false runout triggers
            return True
        return self.filament_motion_sensor.get_status(None)["filament_detected"]

    def setup_finda_sensor(self, config: ConfigWrapper) -> None:
        """Have the MCU report every change of the FINDA pin.

        FINDA's pin is the pulley stepper's endstop pin. It is shared with the
        endstop, so ``finda_triggered`` follows FINDA even when nothing
        queries it, e.g. when the filament is removed by hand.

        Args:
            config (ConfigWrapper): The MMU3 config.
        """
        pin = config.getsection(PULLEY_STEPPER_NAME).get("endstop_pin")
        # allow_multi_use_pin() takes the bare pin, without ^ ~ ! modifiers
        self.printer.lookup_object("pins").allow_multi_use_pin(
            re.sub(r"^[\s^~!]+", "", pin)
        )
        buttons = self.printer.load_object(config, "buttons")
        buttons.register_buttons([pin], self._handle_finda_state)

    def _handle_finda_state(self, eventtime: float, state: int) -> None:
        """Store a FINDA state reported by the MCU.

        Args:
            eventtime (float): When the state was received.
            state (int): 1 if FINDA is triggered, 0 otherwise.
        """
        self.finda_triggered = bool(state)

    def is_filament_in_finda(self) -> bool:
        """Return if the filament is in FINDA or not.

        FINDA is queried, rather than ``finda_triggered`` returned, so the
        reading is taken after the queued moves are done.

        Returns:
            bool: True if the filament is present in FINDA, False otherwise.
        """
        print_time = self.toolhead.get_last_move_time()
        self.finda_triggered = bool(
            self.pulley_stepper_endstop.query_endstop(print_time)
        )
        return self.finda_triggered

    def enable_steppers(self) -> None:
        """Enable the idler and selector so they hold their positions.

        The pulley is left alone, it is enabled by its first move.
        """
        for stepper in (self.idler_stepper, self.selector_stepper):
            stepper.do_enable(True)

    def disable_steppers(
        self, steppers: None | ManualStepper | list[ManualStepper] = None
    ) -> bool:
        """Disable all stepper motors.

        Args:
            steppers (None | list[ManualStepper]): If None all the steppers are
                disabled, if it is a list, only the given steppers are
                disabled.

        Returns:
            bool: True, if all are successfully disabled, False otherwise.
        """
        start_time = time.time()
        if steppers is None:
            steppers = [self.pulley_stepper, self.selector_stepper, self.idler_stepper]
        elif isinstance(steppers, ManualStepper):
            steppers = [steppers]

        if not isinstance(steppers, list):
            return False

        self.respond_debug("Disabling steppers ...")
        self.toolhead.wait_moves()
        for stepper in steppers:
            # stepper.dwell(self.pause_before_disabling_steppers)
            stepper.do_enable(False)
            # stepper.dwell(self.pause_after_disabling_steppers)
        self.respond_debug("Steppers disabled!")

        duration = time.time() - start_time
        self.respond_debug(f"disable_steppers took {duration:0.1f} seconds")
        return True

    def sync_stepper_to_extruder(self, manual_stepper: ManualStepper) -> None:
        """Synchronize the given stepper to the extruder so that they move together.

        Args:
            manual_stepper (ManualStepper): The stepper to synchronize.

        Returns:
            trapq: The original trapq of the stepper before synchronization,
                which can be used to restore the original behavior later.
        """
        self.toolhead.wait_moves()
        self.toolhead.flush_step_generation()
        stepper = manual_stepper.rail.get_steppers()[0]
        orig_trapq = stepper.get_trapq()
        stepper.set_position([self.extruder.last_position, 0.0, 0.0])
        stepper.set_trapq(self.extruder.get_trapq())
        self.motion_queuing.check_step_generation_scan_windows()

        return orig_trapq

    def unsync_stepper_from_extruder(
        self,
        manual_stepper: ManualStepper,
        trapq,  # noqa: ANN001
    ) -> None:
        """Unsynchronize the given stepper from the extruder.

        Args:
            manual_stepper (ManualStepper): The stepper to unsynchronize.
            trapq: The original trapq of the stepper before synchronization.
        """
        self.toolhead.wait_moves()
        self.toolhead.flush_step_generation()
        stepper = manual_stepper.rail.get_steppers()[0]
        stepper.set_position([0.0, 0.0, 0.0])
        stepper.set_trapq(trapq)
        self.motion_queuing.check_step_generation_scan_windows()

    def validate_extruder_is_hot_enough(self) -> bool:
        """Validate if the extruder is hot enough.

        Pauses if extruder is not hot enough.

        Returns:
            bool: True if hotend is hot enough, False otherwise.
        """
        self.respond_debug("Checking hotend temperature")
        current_temp = self.get_extruder_temperature()
        self.respond_debug(f"Current hotend temperature: {current_temp:.1f}ºC")
        self.respond_debug(
            f"Minimum hotend temperature: {self.min_temp_extruder:.1f}ºC"
        )
        if current_temp < self.min_temp_extruder:
            self.display_status_msg("Extruder is not hot enough!")
            return False
        return True

    @reports_action(ACTION_HOMING)
    def home_idler(self) -> bool:
        """Home the idler.

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        # Home the idler
        self.respond_debug("Homing idler")
        self.idler_stepper.do_set_position(0)
        self.toolhead.wait_moves()
        self.respond_debug("Doing the fast move")
        # do a big rotation to ensure we hit the end stop
        # let's try using a homing move...
        self.idler_stepper.do_homing_move(
            movepos=self.idler_homing_move_lengths[0],
            speed=self.idler_homing_speed,
            accel=self.idler_homing_accel,
            probe_pos=False,
            triggered=True,
            check_trigger=True,
        )
        self.toolhead.wait_moves()
        self.idler_stepper.do_set_position(0)

        # rotate it a little back
        self.respond_debug("Doing the slow move")
        self.idler_stepper.do_move(
            self.idler_homing_move_lengths[1],
            self.idler_homing_speed,
            self.idler_homing_accel,
        )
        self.toolhead.wait_moves()
        self.idler_stepper.do_set_position(0)

        # do a second homing move, but slower
        self.idler_stepper.do_homing_move(
            movepos=self.idler_homing_move_lengths[2],
            speed=self.idler_homing_speed / 3,
            accel=self.idler_homing_accel / 3,
            probe_pos=False,
            triggered=True,
            check_trigger=True,
        )
        self.toolhead.wait_moves()
        self.idler_stepper.do_set_position(0)

        # park
        self.unselect_gate()

        self.respond_debug("Finished homing")

        return True

    @reports_action(ACTION_HOMING)
    def home_mmu(self) -> bool:
        """Home the MMU.

        Eject filament if loaded with eject_before_home()
        next home the mmu with home_mmu_only()

        Returns:
            bool: True, if homed, False otherwise.
        """
        with FilamentSwitchSensorManager(
            self.filament_switch_sensor,
            False,
            self.respond_debug,
            self.reactor,
            self.toolhead,
        ):
            self.is_homed = True
            self.respond_debug("Homing MMU ...")
            if not self.eject_before_home():
                return False
            return self.home_mmu_only()

    def home_mmu_only(self) -> bool:
        """Home the MMU.

        Follow the steps:

        1) home the idler
        2) home the selector (if needed)
        3) try to load filament 0 to FINDA and then unload it. Used to verify
           the MMU3 gear

        if all is ok, the MMU3 is ready to be used

        Returns:
            bool: True, if mmu homed, False otherwise.
        """
        if self.is_paused:
            self.display_status_msg("Homing MMU failed, MMU is paused, unlock it ...")
            return False

        self.home_idler()
        if not self.enable_no_selector_mode:
            self.respond_debug("Homing selector")
            self.selector_stepper.do_set_position(0)
            # do a fast homing first
            self.selector_stepper.do_homing_move(
                movepos=-abs(self.selector_homing_move_length),
                speed=self.selector_homing_speed,
                accel=self.selector_accel,
                probe_pos=False,
                triggered=True,
                check_trigger=True,
            )
            # and then a slow homing
            self.toolhead.wait_moves()
            self.selector_stepper.do_set_position(0)
            self.selector_stepper.do_move(
                3,
                self.selector_speed,
                self.selector_accel,
            )
            self.selector_stepper.do_set_position(0)
            self.toolhead.wait_moves()
            self.selector_stepper.do_homing_move(
                movepos=-abs(self.selector_homing_move_length),
                speed=self.selector_homing_speed_slow,
                accel=self.selector_accel,
                probe_pos=False,
                triggered=True,
                check_trigger=True,
            )
            self.toolhead.wait_moves()
            self.selector_stepper.do_set_position(0)

        self.current_gate = None
        self.loaded_gate = None
        self.filament_pos = FilamentPos.UNLOADED
        self.unselect_gate()
        self.is_homed = True
        self.respond_debug("Homing MMU ended ...")

        return True

    def load_filament_to_finda_in_loop(self) -> bool:
        """Load the filament to FINDA in a infinite loop.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True, if filament loaded to FINDA, False otherwise.
        """
        self.toolhead.wait_moves()
        for i in range(int(self.finda_load_retry)):
            self.pulley_stepper.do_set_position(0)
            self.pulley_stepper.do_homing_move(
                movepos=self.finda_load_length,
                speed=self.finda_load_speed,
                accel=self.finda_load_accel,
                probe_pos=False,
                triggered=True,
                check_trigger=False,
            )
            self.toolhead.wait_moves()

            # check endstop status and exit from the loop
            if self.is_filament_in_finda():
                self.respond_debug("FINDA endstop triggered. Exiting filament load.")
                return True
            self.respond_debug(f"FINDA endstop not triggered. Retrying... {i + 1}")
        self.display_status_msg(
            f"Couldn't load filament to FINDA after {self.finda_load_retry} tries!"
        )
        return False

    def pause(self) -> bool:
        """Pause the MMU.

        Park the extruder at the parking position
        Save the current state and start the delayed stop of the heated modify
        the timeout of the printer accordingly to timeout_pause.

        PAUSE MACROS
        PAUSE_MMU is called when an human intervention is needed
        use MMU_UNLOCK to park the idler and start the manual intervention
        and use RESUME when the invention is ended to resume the current print

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.extruder_temp = self.get_extruder_temperature()
        self.is_paused = True
        self.gcode.run_script_from_command(f"""
            SAVE_GCODE_STATE NAME=PAUSE_MMU_state
            SET_IDLE_TIMEOUT TIMEOUT={self.timeout_pause}
            M118 Start PAUSE
            PAUSE
            G90
            ;G1 X{self.pause_position[0]} Y{self.pause_position[1]} F3000
            M300
            M300
            M300
        """)
        self.toolhead.wait_moves()
        self.disable_steppers()
        return True

    def resume(self, force: bool = False) -> bool:
        """Resume the MMU and the print.

        If there is a pending operation left over from a failed load/unload,
        retry it first and only resume the print if that succeeds. ``force``
        skips the retry, clears the pending operation and resumes anyway
        ("I fixed it by hand").

        Args:
            force (bool): Skip the pending-operation retry.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if self.pending_operation is not None and not force:
            self.respond_info(
                f"Pending MMU operation: {self.pending_operation.describe()}"
            )
            if not self.retry_pending_operation():
                self.display_status_msg(
                    "MMU recovery failed - fix the issue then RESUME_MMU again "
                    "(or RESUME_MMU FORCE=1 to override)."
                )
                return False

        if force:
            self.pending_operation = None
            self.current_operation = None

        self.is_paused = False
        self.gcode.run_script_from_command(
            """
            M118 End PAUSE
            RESTORE_GCODE_STATE NAME=PAUSE_MMU_state
            RESUME
            """
        )
        self.toolhead.wait_moves()
        return True

    def _switch_sensor_bounds(
        self, in_finda: bool, in_switch: bool
    ) -> tuple[FilamentPos, FilamentPos]:
        """Return the (floor, ceiling) the sensors allow for ``filament_pos``.

        What the filament switch sensor proves depends on where it sits
        (``filament_switch_sensor_position``):

        * ``PreGears`` / ``OnGears`` - the sensor is at or before the extruder
          gears. Triggered proves the filament has reached the gears but cannot
          tell IN_HOTEND / LOADED apart (the strand keeps the sensor triggered
          once loaded). Not triggered proves the filament is *not* past the
          gears, so it is at most AT_FINDA.
        * ``PostGears`` - the sensor is after the gears. Triggered proves the
          filament has passed through them (>= IN_HOTEND). Not triggered still
          allows the tip to be sitting in the gears (AT_EXTRUDER).

        FINDA sits at the MMU output and stays triggered for every loaded
        state, so ``not in_finda and not in_switch`` is the only unambiguous
        "unloaded".
        """
        post_gears = (
            self.filament_switch_sensor_position
            == FilamentSwitchSensorPosition.PostGears
        )
        if in_switch:
            floor = FilamentPos.IN_HOTEND if post_gears else FilamentPos.AT_EXTRUDER
            return floor, FilamentPos.LOADED
        if in_finda:
            ceiling = FilamentPos.AT_EXTRUDER if post_gears else FilamentPos.AT_FINDA
            return FilamentPos.AT_FINDA, ceiling
        return FilamentPos.UNLOADED, FilamentPos.UNLOADED

    def assess_filament_pos(self) -> FilamentPos:
        """Derive the filament position from the physical sensors.

        Clamps ``self.filament_pos`` into the ``[floor, ceiling]`` window the
        FINDA and filament switch sensor allow (see
        :meth:`_switch_sensor_bounds`), so it can neither claim more progress
        than the sensors support nor keep stale progress the sensors have
        ruled out, and reconciles ``self.loaded_gate``. This replaces the
        ad-hoc ``loaded_gate``-from-FINDA fixups scattered through the
        unload helpers.

        Returns:
            FilamentPos: The assessed (and now stored) position.
        """
        in_finda = self.is_filament_in_finda()
        in_switch = self.is_filament_in_switch_sensor()

        floor, ceiling = self._switch_sensor_bounds(in_finda, in_switch)
        self.filament_pos = min(max(self.filament_pos, floor), ceiling)

        if self.filament_pos == FilamentPos.UNLOADED:
            self.loaded_gate = None
        elif self.loaded_gate is None:
            # filament is somewhere in the path; best guess is the selected gate
            self.loaded_gate = self.current_gate

        self.respond_debug(
            f"assess_filament_pos: FINDA={in_finda} switch={in_switch} "
            f"pos={self.filament_switch_sensor_position} "
            f"-> {self.filament_pos} (filament in gate {self.loaded_gate})"
        )
        return self.filament_pos

    def _load_path(self) -> list:
        """Ordered forward transitions as ``(reached_pos, step_fn, action)``."""
        return [
            (FilamentPos.AT_FINDA, self.load_filament_to_finda, ACTION_LOADING),
            (
                FilamentPos.AT_EXTRUDER,
                self.load_filament_from_finda_to_extruder,
                ACTION_LOADING,
            ),
            (
                FilamentPos.LOADED,
                self.load_filament_to_hotend,
                ACTION_LOADING_EXTRUDER,
            ),
        ]

    def _unload_path(self) -> list:
        """Ordered reverse transitions as ``(reached_pos, step_fn, action)``."""
        return [
            (
                FilamentPos.AT_EXTRUDER,
                self.unload_filament_from_hotend,
                ACTION_UNLOADING_EXTRUDER,
            ),
            (
                FilamentPos.AT_FINDA,
                self.unload_filament_from_extruder_to_finda,
                ACTION_UNLOADING,
            ),
            (FilamentPos.UNLOADED, self.unload_filament_from_finda, ACTION_UNLOADING),
        ]

    def move_filament_to(
        self, target_pos: FilamentPos, gate: None | int = None
    ) -> bool:
        """Run only the sub-steps between the current position and ``target_pos``.

        Shared by the normal load/unload paths and by recovery, so a retry after
        a mid-load failure continues from where it stopped instead of repeating
        the whole chain (e.g. a 450 mm bowden move).

        Args:
            target_pos (FilamentPos): The position to reach.
            gate (None | int): Gate to select before any forward move.
                Defaults to ``current_gate``; unload sub-steps auto-select from
                ``loaded_gate``.

        Returns:
            bool: True if ``filament_pos`` reached ``target_pos``.
        """
        if self.is_paused:
            return False
        if self.filament_pos == target_pos:
            return True
        if target_pos > self.filament_pos:
            return self._load_toward(target_pos, gate)
        return self._unload_toward(target_pos)

    def _load_toward(self, target_pos: FilamentPos, gate: None | int) -> bool:
        """Run the forward sub-steps needed to reach ``target_pos``."""
        if gate is None:
            gate = self.current_gate
        if gate is None:
            self.display_status_msg("Cannot load, no gate selected!")
            return False
        if (
            self.filament_pos <= FilamentPos.AT_FINDA
            and self.current_gate != gate
            and not self.select_gate(gate)
        ):
            return False
        for reached_pos, step_fn, action in self._load_path():
            if self.filament_pos < reached_pos <= target_pos:
                with self.running_action(action):
                    if not step_fn():
                        return False
        return self.filament_pos >= target_pos

    def _unload_toward(self, target_pos: FilamentPos) -> bool:
        """Run the reverse sub-steps needed to reach ``target_pos``."""
        for reached_pos, step_fn, action in self._unload_path():
            if target_pos <= reached_pos < self.filament_pos:
                with self.running_action(action):
                    if not step_fn():
                        return False
        return self.filament_pos <= target_pos

    def _pending_operation_resolved(self) -> bool:
        """Whether the current filament state satisfies ``pending_operation``.

        Used by :func:`track_operation` so that an unrelated command
        succeeding - e.g. clicking "Unload" or "Home MMU" in the
        recovery dialog to manually work on a stuck filament - does not
        silently discard a still-unfinished ``pending_operation``. Only
        actually reaching the position (and, for loads, the tool) the
        pending operation was trying to reach counts as resolving it.

        Returns:
            bool: True if there is no pending operation, or the current
                filament state already satisfies it.
        """
        op = self.pending_operation
        if op is None:
            return True
        if op.target_pos == FilamentPos.UNLOADED:
            return self.filament_pos == FilamentPos.UNLOADED
        return self.filament_pos >= op.target_pos and self.loaded_gate == op.to_gate

    def retry_pending_operation(self) -> bool:
        """Re-drive ``self.pending_operation`` after checking the sensors.

        Clears ``is_paused`` for the duration so the sub-steps can run, then
        restores it to reflect whether recovery succeeded.

        Returns:
            bool: True if the pending operation completed (or there was none).
        """
        op = self.pending_operation
        if op is None:
            self.display_status_msg("No pending MMU operation to retry.")
            return True

        self.respond_info(f"Retrying: {op.describe()}")
        self.is_paused = False
        ok = False
        try:
            self.assess_filament_pos()
            if op.kind == OperationKind.HOME:
                ok = self.home_mmu()
            elif op.kind == OperationKind.CUT and op.to_gate is not None:
                ok = self.cut_filament_in_mmu(op.to_gate)
            elif op.kind == OperationKind.TOOL_CHANGE and op.to_gate is not None:
                if op.to_tool is not None:
                    self.selected_tool = op.to_tool
                ok = self.unload_gate() and self.load_gate(op.to_gate)
            elif op.kind == OperationKind.LOAD and op.to_gate is not None:
                ok = self.load_gate(op.to_gate)
            else:  # UNLOAD / EJECT / degenerate TOOL_CHANGE
                ok = self.unload_gate()
        finally:
            self.is_paused = not ok

        if ok:
            self.pending_operation = None
            self.current_operation = None
            self.display_status_msg(f"{op.describe()} => recovered")
        return ok

    def show_recovery_prompt(self) -> None:
        """Show the Mainsail recovery dialog for the pending operation.

        The retry button is generic (``MMU_RETRY``) so the same dialog serves
        every failure site - the operator never has to remember the original
        command.
        """
        message = (
            self.pending_operation.describe()
            if self.pending_operation is not None
            else "MMU operation failed."
        )
        prompt = Prompt(
            headline="MMU Error",
            widgets=[
                Text(text=message),
                ButtonGroup(
                    buttons=[
                        Button(label="Unlock MMU", gcode="MMU_UNLOCK"),
                        Button(label="Unload", gcode="MMU_UNLOAD"),
                    ],
                ),
                ButtonGroup(
                    buttons=[
                        Button(label="Home MMU", gcode="MMU_HOME"),
                        Button(
                            label="Retry",
                            gcode="PROMPT_CLOSE_AND_RUN_COMMAND COMMAND=MMU_RETRY",
                        ),
                    ],
                ),
                FooterButton(
                    label="Resume",
                    gcode="PROMPT_CLOSE_AND_RUN_COMMAND COMMAND=RESUME_MMU",
                ),
            ],
        )
        self.gcode.run_script_from_command(prompt.to_gcode())

    def unlock(self) -> bool:
        """Park the idler, stop the delayed stop of the heater.

        Args:
            gcmd GCodeCommand: The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.display_status_msg("Unlocking MMU...")
        self.is_paused = False
        return self.home_idler()

    @reports_action(ACTION_SELECTING)
    def select_gate(self, gate: int) -> bool:
        """Select a gate. move the idler and then move the selector (if needed).

        Args:
            gate (int): The gate.

        Returns:
            bool: True, if the gate is selected, False otherwise.
        """
        if self.is_paused:
            return False

        if not self.is_homed:
            self.display_status_msg("MMU is not homed, homing!")
            if not self.home_mmu():
                return False

        if gate is None or gate < 0:
            self.display_status_msg(f"Invalid gate: {gate}")
            return False

        if self.is_filament_in_finda() and self.loaded_gate is None:
            self.display_status_msg(
                "Filament detected in FINDA, "
                "please unload it manually before selecting a gate."
            )
            return False

        self.respond_debug(f"Select gate {gate} ...")
        self.idler_stepper.do_move(
            self.idler_positions[gate],
            self.idler_speed,
            self.idler_accel,
            sync=False,
        )

        if not self.enable_no_selector_mode:
            self.selector_stepper.do_move(
                self.selector_positions[gate],
                self.selector_speed,
                self.selector_accel,
            )
        self.current_gate = gate
        self.respond_debug(f"Gate {gate} selected")
        return True

    def unselect_gate(self) -> bool:
        """Unselect the gate, only park the idler.

        Returns:
            bool: True, if the gate is unselected, False otherwise.
        """
        if self.is_paused:
            return False

        if not self.is_homed:
            self.display_status_msg("MMU is not homed, homing!")
            if not self.home_mmu():
                return False

        if self.current_gate is not None:
            self.respond_debug(f"Unselecting gate {self.current_gate}")
        else:
            self.respond_debug("Unselecting while no gate is selected!")

        self.idler_stepper.do_move(
            self.idler_positions[-1],
            self.idler_speed,
            self.idler_accel,
            sync=False,
        )
        self.current_gate = None
        self.respond_debug("Unselect gate is complete!")
        return True

    def retry_load_filament_to_hotend(self) -> bool:
        """Try to load the filament to the hotend.

        Called when the IR sensor does not detect the filament the MMU3 push
        the filament of 10mm and the extruder gear try to insert it into the
        nozzle.

        Returns:
            bool: True, if filament loaded to hotend, False otherwise.
        """
        if self.is_filament_in_switch_sensor():
            return True

        self.respond_debug("Retry loading ...")
        if self.is_paused:
            self.display_status_msg("Printer is paused ...")
            return False

        if not self.validate_extruder_is_hot_enough():
            return False

        self.respond_debug(
            "Loading Filament To Hotend (Native Trapq Sync Mode Retry)..."
        )

        with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
            length = self.bowden_load_length3
            speed = self.pulley_load_to_extruder_speed
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E{length} F{speed * 60}
                G90
            """)
            self.toolhead.wait_moves()

        self.pulley_stepper.do_set_position(0)

        return True

    @tracks_filament()
    def load_filament_to_hotend(self) -> bool:
        """Load the filament to hotend with perfectly synchronized steppers.

        Returns:
            bool: True, if filament loaded to hotend.
        """
        if self.is_paused:
            return False

        if not self.validate_extruder_is_hot_enough():
            return False

        self.respond_debug("Loading Filament To Hotend (Native Trapq Sync Mode)...")

        with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
            length = self.bowden_load_length3
            speed = self.pulley_load_to_extruder_speed
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E{length} F{speed * 60}
                G90
            """)
            self.toolhead.wait_moves()

        if not self.is_filament_in_switch_sensor():
            for _ in range(self.load_retry):
                self.retry_load_filament_to_hotend()

        self.unselect_gate()

        if not self.is_filament_in_switch_sensor():
            self.respond_debug("Filament is not in switch sensor after load!")
            return False

        self.filament_pos = max(self.filament_pos, FilamentPos.IN_HOTEND)

        detection_length = 0
        if self.filament_motion_sensor:
            detection_length = self.filament_motion_sensor.detection_length * 2

        if self.extra_load_length > detection_length:
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E{self.extra_load_length} F{self.extra_load_speed * 60}
                G90
                G0 F{self.travel_speed * 60}
            """)
            # the idler is released, the pulley does not see this push
            self.filament_tracker.advance(self.extra_load_length)
        elif self.filament_motion_sensor:
            # wiggle the filament back and forth and check the encoder sensor
            # to make sure the filament is really grabbed by the extruder gear
            detection_length = self.filament_motion_sensor.detection_length * 2
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E{detection_length} F{self.pulley_load_to_extruder_speed * 60}
                G90
            """)
            self.filament_tracker.advance(detection_length)
        self.toolhead.wait_moves()

        if self.filament_motion_sensor and not self.is_filament_moving():
            self.respond_debug("Filament is not moving after load!")
            return False

        self.filament_pos = FilamentPos.LOADED
        self.tip_formed = False
        self.respond_debug("Load Complete")
        return True

    def retry_unload_filament_from_hotend(self) -> None:
        """Retry unload, try correct misalignment of bondtech gear."""
        if not self.is_filament_in_switch_sensor():
            return True

        self.respond_debug("Retry unloading ....")
        if self.is_paused:
            self.display_status_msg("MMU is paused")
            return False

        if not self.validate_extruder_is_hot_enough():
            return False

        if self.current_gate is None and self.loaded_gate is not None:
            # keep the pulley able to help: without this the idler stays
            # parked and the pulley un-synced, so the extruder retracts
            # alone against filament that may still be pinched at the
            # selector.
            self.select_gate(self.loaded_gate)

        self.respond_debug("Unloading Filament...")
        with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E-{self.hotend_unload_length} F{self.hotend_unload_speed * 60}
                G92 E0
                G90
            """)
            self.toolhead.wait_moves()
        return True

    @tracks_filament()
    def unload_filament_from_hotend(self) -> bool:
        """Unload the filament from the nozzle (without RAMMING !!!).

        Retract the filament from the nozzle to the out of the extruder gear.
        Call PAUSE_MMU if the IR sensor detects the filament after the ejection

        Returns:
            bool: True, if the filament unloaded from extruder.
        """
        if self.is_paused:
            return False

        if not self.is_filament_in_switch_sensor():
            self.respond_debug("No filament in extruder")
            self.filament_pos = min(self.filament_pos, FilamentPos.AT_EXTRUDER)
            return True

        if self.current_gate is None and self.loaded_gate is not None:
            # keep the pulley able to help: without this the idler stays
            # parked and the pulley un-synced, so the extruder retracts
            # alone against filament that may still be pinched at the
            # selector.
            self.select_gate(self.loaded_gate)

        if not self.validate_extruder_is_hot_enough():
            return False

        self.respond_debug("Unloading Filament...")
        with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E-{self.hotend_unload_length} F{self.hotend_unload_speed * 60}
                G90
                G92 E0
                ;G4 P1000
            """)
            self.toolhead.wait_moves()

        if (
            self.filament_switch_sensor_position
            != FilamentSwitchSensorPosition.PreGears
        ):
            if self.is_filament_in_switch_sensor():
                for _ in range(self.unload_retry):
                    self.retry_unload_filament_from_hotend()

            if self.is_filament_in_switch_sensor():
                return False

        self.filament_pos = min(self.filament_pos, FilamentPos.AT_EXTRUDER)
        self.respond_debug("Filament removed")
        return True

    def form_tip(self, final_eject: bool = False) -> None:
        """Form the filament tip by ramming, reporting ``Forming Tip``.

        Args:
            final_eject (bool): Also pull the filament out of the extruder
                gears (the macro's ``FINAL_EJECT=1``).
        """
        script = FORM_TIP_MACRO + (" FINAL_EJECT=1" if final_eject else "")
        with self.running_action(ACTION_FORMING_TIP):
            self.gcode.run_script_from_command(script)
            self.toolhead.wait_moves()

    def cut_tip(self) -> None:
        """Cut the filament in the extruder, reporting ``Cutting Tip``."""
        with self.running_action(ACTION_CUTTING_TIP):
            self.gcode.run_script_from_command(CUT_TIP_MACRO)
            self.toolhead.wait_moves()

    def form_tip_standalone(self, cut: bool = False) -> bool:
        """Form the tip (or cut) of the loaded filament outside an unload.

        Runs the same step as an unload (:meth:`form_tip` / :meth:`cut_tip`
        with the idler parked), so it can be tested and tuned on its own. The
        filament is left in the extruder, no longer ``LOADED``.

        A filament pushed into the extruder by hand (seen by the filament
        switch sensor but not by FINDA) gets its tip formed too, without
        moving the MMU, and is ejected from the extruder gears so it can be
        pulled out, like Happy Hare's ``MMU_TEST_FORM_TIP``.

        Args:
            cut (bool): Cut the filament instead of ramming it.

        Returns:
            bool: True if the tip was formed / cut, False otherwise.
        """
        name = "MMU_CUT" if cut else "MMU_FORM_TIP"
        if not self.is_enabled:
            self.display_status_msg("MMU is not enabled!")
            return False
        if self.is_paused:
            self.display_status_msg(f"MMU is paused, cannot run {name}!")
            return False
        if cut and not self.enable_filament_cutter:
            self.display_status_msg(
                "MMU_CUT needs `enable_filament_cutter: True` in [mmu]."
            )
            return False
        # a filament pushed into the extruder by hand bypasses the MMU
        by_hand = (
            not cut
            and not self.is_filament_in_finda()
            and self.is_filament_in_switch_sensor()
        )
        if not by_hand and self.assess_filament_pos() != FilamentPos.LOADED:
            self.display_status_msg(f"No filament loaded, cannot run {name}!")
            return False
        if not self.validate_extruder_is_hot_enough():
            return False
        if by_hand:
            self.respond_info(
                "No filament in the MMU, forming the tip of the filament in the "
                "extruder and ejecting it."
            )
        elif self.current_gate is not None and not self.unselect_gate():
            return False

        with (
            FilamentSwitchSensorManager(
                self.filament_switch_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
            FilamentMotionSensorManager(
                self.filament_motion_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
        ):
            if cut:
                self.cut_tip()
            else:
                self.form_tip(final_eject=by_hand)
        # the MMU's filament, an unload doesn't cut / ram it again
        self.tip_formed = not by_hand

        # the filament is still in the extruder but retracted from the nozzle,
        # the sensors may show it is even further back
        self.filament_pos = min(self.filament_pos, FilamentPos.IN_HOTEND)
        self.assess_filament_pos()
        return True

    def unload_filament_from_hotend_with_ramming(self) -> bool:
        """Unload from extruder with ramming.

        Returns:
            bool: True, if filament unloaded from extruder, False otherwise.
        """
        if self.is_paused:
            return False

        if not self.validate_extruder_is_hot_enough():
            return False

        if self.current_gate is not None:
            self.respond_debug(f"Gate {self.current_gate} selected!")
            self.respond_debug(f"Auto unselecting gate {self.current_gate}")
            self.unselect_gate()

        self.respond_debug("Ramming and Unloading Filament...")

        if self.enable_filament_cutter:
            self.cut_tip()
        else:
            self.form_tip()

        if not self.unload_filament_from_hotend():
            return False
        self.respond_debug("Filament rammed and removed")
        return True

    def calibrate_pulley_rotation_distance(self, length: float | None = None) -> bool:
        """Calibrate pulley rotation_distance value.

        This will first load the filament in to the FINDA, pause for
        `pulley_calibrate_pause_duration` seconds, and then pull exactly
        `length` mm of filament and then pause. So, that the pulled filament
        can be measured from behind the MMU.

        Args:
            length (float | None): Length of filament to pull in mm. Defaults
                to `pulley_calibrate_filament_length` from config.

        Returns:
            bool: True, if filament is pulled by `length` mm, False in any
                other errors.
        """
        if length is None:
            length = self.pulley_calibrate_filament_length

        # pull the filament to finda
        self.display_status_msg("Load to FINDA")
        if not self.load_filament_to_finda():
            return False

        # wait for `pulley_calibrate_pause_duration` seconds
        self.gcode.run_script_from_command("M300")
        self.display_status_msg("Mark the filament")
        self.reactor.pause(
            self.reactor.monotonic() + self.pulley_calibrate_pause_duration
        )

        # now pull exactly `length` mm of filament.
        self.display_status_msg(f"Loading {length:.0f} mm")
        self.pulley_stepper.do_set_position(0)
        self.pulley_stepper.do_move(
            length,
            self.bowden_load_speed1,
            self.bowden_load_accel1,
        )
        return True

    def calibrate_bowden_load_length(self, step: float = 10.0) -> bool:
        """Auto-detect bowden_load_length1 by pushing filament to the switch sensor.

        Loads filament to FINDA, then advances in `step` mm increments until
        the filament switch sensor triggers. The total distance moved becomes
        the new bowden_load_length1 and is staged in the configfile so
        SAVE_CONFIG persists it.

        PRE_GEARS: sensor is upstream of the extruder gears; pure pulley push.
        ON_GEARS / POST_GEARS: filament must reach or pass through the extruder
        gears to trigger the sensor; ExtruderSynchronizer is used and speed is
        capped at half the extruder's max velocity so the extruder stepper is
        not overdriven.

        Args:
            step (float): Push increment size in mm. Default: 10.0.

        Returns:
            bool: True if calibration succeeded, False otherwise.
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            self.display_status_msg("Select a gate before calibrating bowden length!")
            return False

        if self.filament_switch_sensor is None:
            self.display_status_msg(
                "No filament switch sensor configured - cannot calibrate!"
            )
            return False

        # ON_GEARS: sensor is at the gears; filament tip must enter the gears.
        # POST_GEARS: sensor is past the gears; filament must pass through them.
        # Both need ExtruderSynchronizer so the gears turn in sync with the push.
        needs_extruder_sync = self.filament_switch_sensor_position in (
            FilamentSwitchSensorPosition.OnGears,
            FilamentSwitchSensorPosition.PostGears,
        )

        self.display_status_msg("Loading to FINDA for bowden calibration...")
        if not self.load_filament_to_finda():
            return False

        if self.is_filament_in_switch_sensor():
            self.display_status_msg(
                "Switch sensor already triggered - unload filament before calibrating!"
            )
            return False

        self.display_status_msg("Measuring bowden length...")
        max_distance = 1500.0
        total_moved = 0.0
        triggered = False

        if needs_extruder_sync:
            sync_speed = min(self.bowden_load_speed1, self.extruder.max_e_velocity / 2)
            with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
                while total_moved < max_distance:
                    move_amount = min(step, max_distance - total_moved)
                    self.gcode.run_script_from_command(f"""
                        G91
                        G92 E0
                        G1 E{move_amount:.3f} F{sync_speed * 60:.0f}
                        G90
                    """)
                    self.toolhead.wait_moves()
                    total_moved += move_amount
                    if self.is_filament_in_switch_sensor():
                        triggered = True
                        break
        else:
            self.pulley_stepper.do_set_position(0)
            while total_moved < max_distance:
                next_pos = min(total_moved + step, max_distance)
                self.pulley_stepper.do_move(
                    next_pos,
                    self.bowden_load_speed1,
                    self.bowden_load_accel1,
                )
                self.toolhead.wait_moves()
                total_moved = next_pos
                if self.is_filament_in_switch_sensor():
                    triggered = True
                    break

        if not triggered:
            self.display_status_msg(
                f"Filament did not reach sensor after {total_moved:.0f}mm!"
            )
            return False

        new_length = int(total_moved)
        self.bowden_load_length1 = new_length

        self.display_status_msg(f"bowden_load_length1 = {new_length}mm")
        self.respond_info(
            f"Bowden calibration complete.\n"
            f"Measured bowden_load_length1 = {new_length}\n"
            f"Update your config file: bowden_load_length1: {new_length}"
        )
        return True

    @reports_action(ACTION_CHECKING)
    def pre_load_filament_to_finda(self, gate: int) -> bool:
        """Feed the filament of a gate to FINDA and back.

        Args:
            gate (int): The gate to preload.

        Returns:
            bool: True if the filament reached FINDA and was unloaded again.
        """
        if self.is_paused:
            return False

        if not self.gate_map.is_valid_gate(gate):
            self.display_status_msg(f"Invalid gate: {gate}")
            return False

        self.respond_debug(f"Pre-loading gate {gate}")
        self.select_gate(gate)
        if not self.load_filament_to_finda():
            return False
        return self.unload_filament_from_finda()

    @reports_action(ACTION_CHECKING)
    def check_gates(self, gates: list[int], quiet: bool = False) -> bool:
        """Check which gates have filament by feeding each to FINDA and back.

        ``load_filament_to_finda()`` records each gate as available or empty.
        An empty gate does not stop the check. The previously selected gate
        is re-selected at the end.

        Args:
            gates (list[int]): The gates to check.
            quiet (bool): Do not print the summary.

        Returns:
            bool: False if a gate could not be selected or its filament could
                not be unloaded from FINDA, True otherwise (empty gates
                included).
        """
        if self.is_paused:
            return False

        previous_gate = self.current_gate
        results = {}
        try:
            for gate in gates:
                self.respond_debug(f"Checking gate {gate}")
                if not self.select_gate(gate):
                    return False
                if not self.load_filament_to_finda():
                    results[gate] = GATE_EMPTY
                    continue
                results[gate] = GATE_AVAILABLE
                if not self.unload_filament_from_finda():
                    return False
            if previous_gate is not None and previous_gate != self.current_gate:
                return self.select_gate(previous_gate)
            return True
        finally:
            if results and not quiet:
                self.respond_info(
                    "Gate check: "
                    + ", ".join(
                        f"Gate {gate}: {GATE_STATUS_TEXT[status]}"
                        for gate, status in results.items()
                    )
                )

    def load_filament_to_finda(self) -> bool:
        """Load filament until the FINDA detect it.

        Then push it 10mm more to be sure is well detected.
        PAUSE_MMU is called if the FINDA does not detect the filament

        Returns:
            bool: True, if the filament is loaded to FINDA, False otherwise.
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            self.display_status_msg("Cannot load to FINDA, gate not selected !!")
            return False

        if self.enable_no_selector_mode:
            # no per-gate FINDA stage in 5in1 mode - the spool feeds straight
            # to the extruder, mirroring load_filament_to_extruder()
            self.loaded_gate = self.current_gate
            self.filament_pos = max(self.filament_pos, FilamentPos.AT_FINDA)
            return True

        self.respond_debug("Loading filament to FINDA ...")
        if not self.load_filament_to_finda_in_loop():
            self.pulley_stepper.do_set_position(0)
            # nothing reached FINDA, the gate has run out of filament
            self.set_gate_status(self.current_gate, GATE_EMPTY)
            return False

        self.pulley_stepper.do_set_position(0)
        self.set_gate_status(self.current_gate, GATE_AVAILABLE)

        # if not self.is_filament_in_finda():
        #     return False

        self.loaded_gate = self.current_gate
        self.filament_pos = max(self.filament_pos, FilamentPos.AT_FINDA)
        self.respond_debug("Loading done to FINDA")
        return True

    @tracks_filament(is_bowden_move=True)
    def load_filament_from_finda_to_extruder(self) -> bool:
        """Load from the FINDA to the extruder gear.

        Move bowden_load_length1 then check the filament switch sensor.
        If not triggered, push in bowden_load_length2 increments up to
        load_retry times. Pause if filament never reaches the sensor.

        Returns:
            bool: True, if filament is loaded from FINDA to extruder, False
                otherwise.
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            self.display_status_msg("Gate not selected!")
            return False

        self.respond_debug("Loading filament from FINDA to extruder ...")

        self.pulley_stepper.do_set_position(0)
        self.pulley_stepper.do_move(
            self.bowden_load_length1,  # initial bulk move by bowden_load_length1
            self.bowden_load_speed1,
            self.bowden_load_accel1,
        )

        # Check filament switch sensor after initial bowden move.
        if (
            self.filament_switch_sensor is not None
            and self.filament_switch_sensor_position
            != FilamentSwitchSensorPosition.PostGears
        ):
            if self.is_filament_in_switch_sensor():
                self.respond_debug("Filament detected at sensor")
                self.respond_debug("Loading done from FINDA to extruder")
                self.filament_pos = max(self.filament_pos, FilamentPos.AT_EXTRUDER)
                return True

            # Not triggered yet - push incrementally up to load_retry times.
            # Uses bowden_load_length2 as the increment size per retry.
            self.respond_debug(
                "Filament not yet at extruder sensor, pushing incrementally ..."
            )
            with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
                for attempt in range(self.load_retry):
                    self.respond_debug(
                        f"Extruder sensor retry {attempt + 1}/{self.load_retry}"
                    )
                    self.gcode.run_script_from_command(f"""
                        G91
                        G92 E0
                        G1 E{self.bowden_load_length2} F{self.bowden_load_speed2 * 60}
                        G90
                    """)
                    self.toolhead.wait_moves()
                    # check sensor after each increment
                    if self.is_filament_in_switch_sensor():
                        self.respond_debug("Filament detected at extruder sensor")
                        self.respond_debug("Loading done from FINDA to extruder")
                        self.filament_pos = max(
                            self.filament_pos, FilamentPos.AT_EXTRUDER
                        )
                        return True

            # All retries exhausted, filament never reached sensor.
            self.display_status_msg(
                "Filament did not reach extruder sensor"
                f" after {self.load_retry} retries!"
            )
            return False

        # No sensor defined - fall back to original fixed distance behavior
        # so existing setups without a sensor continue to work unchanged.
        self.pulley_stepper.do_set_position(0)
        with ExtruderSynchronizer(mmu=self, manual_stepper=self.pulley_stepper):
            self.gcode.run_script_from_command(f"""
                G91
                G92 E0
                G1 E{self.bowden_load_length2} F{self.bowden_load_speed2 * 60}
                G90
            """)

        self.respond_debug("Loading done from FINDA to extruder")
        self.filament_pos = max(self.filament_pos, FilamentPos.AT_EXTRUDER)
        return True

    def load_filament_to_extruder(self) -> bool:
        """Load from MMU3 to extruder gear by calling load_filament_to_finda().

        Then load_filament_from_finda_to_extruder().
        PAUSE_MMU is called if the FINDA does not detect the filament.

        Returns:
            bool: True, if filament is loaded to extruder
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            self.display_status_msg("Gate not selected, cannot load to extruder!")
            return False

        self.respond_debug("Loading filament from MMU to extruder ...")
        if self.enable_no_selector_mode is False and not self.load_filament_to_finda():
            return False

        if self.load_filament_from_finda_to_extruder():
            self.respond_debug("Loading done from MMU to extruder")
            return True
        # there should be an error about loading from FINDA to extruder
        return False

    def unload_filament_from_finda(self) -> None:
        """Unload filament until the FINDA detect it.

        Then push it -10mm more to be sure is well not detected.
        PAUSE_MMU is called if the FINDA does detect the filament.

        Returns:
            bool: True, if filament unloaded from FINDA, False otherwise.
        """
        if self.is_paused:
            return False

        if self.enable_no_selector_mode:
            # no per-gate FINDA stage in 5in1 mode
            self.loaded_gate = None
            self.filament_pos = FilamentPos.UNLOADED
            return True

        if self.current_gate is None:
            if self.loaded_gate is not None:
                # Auto select the loaded gate
                self.select_gate(self.loaded_gate)
            else:
                self.display_status_msg("Gate not selected, cannot unload from FINDA!")
                return False

        self.respond_debug("Unloading filament from FINDA ...")
        self.pulley_stepper.do_set_position(0)
        self.pulley_stepper.do_move(
            -self.finda_unload_length,
            self.finda_unload_speed,
            self.finda_unload_accel,
        )
        self.pulley_stepper.do_set_position(0)
        self.toolhead.wait_moves()

        # The filament can be sitting well past FINDA, up the bowden tube
        # (e.g. after a failed load-to-extruder attempt), so the short move
        # above is not enough to clear it. Keep pulling in bowden-length
        # steps until FINDA stops triggering instead of giving up.
        if self.is_filament_in_finda() and not self.unload_filament_to_finda_in_loop():
            return False

        if self.is_filament_in_finda():
            return False
        self.loaded_gate = None
        self.filament_pos = FilamentPos.UNLOADED
        self.respond_debug("Unloading done from FINDA")
        return True

    @tracks_filament(is_bowden_move=True)
    def unload_filament_from_extruder_to_finda(self) -> bool:
        """Unload from extruder gear to the FINDA.

        Returns:
            bool: True, if filament unloaded from extruder to FINDA.
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            if self.loaded_gate is not None:
                # Auto select the loaded gate
                self.select_gate(self.loaded_gate)
            else:
                self.display_status_msg(
                    "Gate not selected, cannot unload from extruder to FINDA!"
                )
                return False

        self.respond_debug("Unloading filament from extruder to FINDA ...")
        self.pulley_stepper.do_set_position(0)
        if not self.enable_no_selector_mode:
            self.pulley_stepper.do_homing_move(
                movepos=-self.bowden_unload_length,
                speed=self.bowden_unload_speed,
                accel=self.bowden_unload_accel,
                probe_pos=False,
                triggered=False,
                check_trigger=False,
            )

            # if the filament sensor is pre-gears, check if we were able to
            # pull the filament out.
            if (
                self.filament_switch_sensor_position
                == FilamentSwitchSensorPosition.PreGears
                and self.is_filament_in_switch_sensor()
            ):
                for _ in range(self.unload_retry):
                    self.retry_unload_filament_from_hotend()
                if self.is_filament_in_switch_sensor():
                    self.display_status_msg(
                        "Filament stuck in extruder, cannot retract to FINDA!"
                    )
                    return False

            # if filament is still in finda, get into an unload loop...
            if (
                self.is_filament_in_finda()
                and not self.unload_filament_to_finda_in_loop()
            ):
                return False

            if self.is_filament_in_finda():
                return False
        else:
            self.pulley_stepper.do_move(
                -self.bowden_unload_length,
                self.bowden_unload_speed,
                self.bowden_unload_accel,
            )
        self.filament_pos = min(self.filament_pos, FilamentPos.AT_FINDA)
        self.respond_debug("Done unloading from FINDA!")
        return True

    def unload_filament_to_finda_in_loop(self) -> bool:
        """Unload the filament to FINDA in a loop.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True, if filament unloaded to FINDA, False otherwise.
        """
        for i in range(int(self.finda_unload_retry)):
            self.pulley_stepper.do_set_position(0)
            self.pulley_stepper.do_homing_move(
                movepos=-self.bowden_unload_length,
                speed=self.bowden_unload_speed,
                accel=self.bowden_unload_accel,
                probe_pos=False,
                triggered=False,
                check_trigger=False,
            )
            self.toolhead.wait_moves()

            # check endstop status and exit from the loop
            if not self.is_filament_in_finda():
                self.respond_debug("FINDA endstop triggered. Exiting filament unload.")
                return True
            self.respond_debug(f"FINDA endstop not triggered. Retrying... {i + 1}")
        self.display_status_msg(
            f"Couldn't unload filament to FINDA after {self.finda_unload_retry} tries!"
        )
        return False

    def unload_filament_from_extruder(self) -> bool:
        """Unload from the extruder gear to the MMU3.

        Do it by calling unload_filament_from_extruder_to_finda() and
        then unload_filament_from_finda()

        Returns:
            bool: True, if filament unloaded from the extruder, False otherwise.
        """
        if self.is_paused:
            return False

        if self.current_gate is None:
            if self.loaded_gate is not None:
                # Auto select the loaded gate
                self.select_gate(self.loaded_gate)
            else:
                self.display_status_msg(
                    "Gate not selected, cannot unload from extruder to MMU!"
                )
                return False

        self.respond_debug("Unloading filament from extruder to MMU ...")
        if not self.unload_filament_from_extruder_to_finda():
            return False

        if self.enable_no_selector_mode:
            self.respond_debug("Unloading done from extruder to MMU")
            return True

        if not self.unload_filament_from_finda():
            return False

        self.respond_debug("Unloading done from extruder to MMU")
        return True

    @reports_action(ACTION_CUTTING_FILAMENT)
    def cut_filament_in_mmu(self, gate: int) -> bool:
        """Cut the filament in the MMU3.

        Perform the cut from right to left.

        Args:
            gate (int): The gate.

        Returns:
            bool: True, if filament is cut, False otherwise.
        """
        if self.number_of_tools > 5:
            self.display_status_msg("Not supported!")
            return False

        if self.is_paused:
            return False

        if self.enable_no_selector_mode:
            self.display_status_msg("Not supported in 5in1 mode!")
            return False

        self.respond_debug(f"Cutting filament of gate {gate} ...")

        # First unload filament
        if not self.unload_gate():
            self.display_status_msg("Apparently unload failed!")
            return False

        # Select gate
        if not self.select_gate(gate):
            return False

        # Feed to FINDA
        if not self.load_filament_to_finda():
            return False

        # Unload filament from FINDA
        if not self.unload_filament_from_finda():
            return False

        # Prepare blade
        # - move the idler to the current gate position,
        #   to keep the filament tight in place.
        # - move the selector to the 0 position
        self.idler_stepper.do_move(
            self.idler_positions[gate],
            self.idler_homing_speed,
            self.idler_homing_accel,
        )
        # move the selector to 0 position or close to 0
        self.selector_stepper.do_move(
            5,
            self.selector_speed,
            self.selector_accel,
        )

        # Push filament
        self.pulley_stepper.do_set_position(0)
        self.pulley_stepper.do_move(
            self.cut_filament_length + self.cutting_edge_retract,
            self.pulley_stepper.velocity,
            self.pulley_stepper.accel,
        )

        # Unlock the selector
        # Perform the cut by moving to the current slot
        # set stepper current
        # decrease driver_SGTHRS
        # SET_TMC_FIELD
        # SET_TMC_CURRENT
        # TODO: Use the Python API for this, maybe a context manager with `with`?
        stepper_name = SELECTOR_STEPPER_NAME.split(" ")[-1]
        self.gcode.run_script_from_command(f"""
            SET_TMC_FIELD STEPPER={stepper_name} FIELD=SGTHRS VALUE=0
            SET_TMC_CURRENT STEPPER={stepper_name} CURRENT={self.cut_stepper_current}
        """)
        self.toolhead.wait_moves()

        # do cut
        self.selector_stepper.do_move(
            self.selector_positions[gate],
            self.selector_homing_speed,
            0,
        )

        # return the stepper current and threshold to normal
        # TODO: This is manual for now
        self.gcode.run_script_from_command(f"""
            SET_TMC_FIELD STEPPER={stepper_name} FIELD=SGTHRS VALUE=96
            SET_TMC_CURRENT STEPPER={stepper_name} CURRENT=0.580
        """)

        # Pull filament back from the cutting edge
        self.pulley_stepper.do_set_position(0)
        self.pulley_stepper.do_move(
            -self.cutting_edge_retract,
            self.pulley_stepper.velocity,
            self.pulley_stepper.accel,
        )

        # Home the mmu
        self.home_mmu()

        self.respond_debug(f"Done cutting gate {gate}!")
        return True

    def load_gate(self, gate: int) -> bool:
        """Load filament from MMU3 to nozzle.

        Args:
            gate (int): The gate.

        Returns:
            bool: True, if filament is loaded, False otherwise.
        """
        if self.is_paused:
            return False

        if not self.validate_extruder_is_hot_enough():
            return False

        self.respond_debug(f"MMU_LOAD {gate}")
        if self.filament_pos == FilamentPos.LOADED:
            # nothing to load, so no load hooks either
            return self.move_filament_to(FilamentPos.LOADED, gate)
        if not self.run_user_macro(PRE_LOAD_MACRO):
            return False
        if self.filament_pos < FilamentPos.AT_EXTRUDER and not self.select_gate(gate):
            return False
        # planner runs only the steps still needed to reach LOADED, so a retry
        # after a mid-load failure does not repeat the whole bowden move
        if not self.move_filament_to(FilamentPos.LOADED, gate):
            return False
        return self.run_user_macro(POST_LOAD_MACRO)

    def unload_gate(self) -> bool:
        """Unload filament from nozzle to MMU3.

        Returns:
            bool: True, if the filament is unloaded, False otherwise.
        """
        if self.is_paused:
            self.respond_debug("MMU is paused, cannot unload!")
            return False

        if self.loaded_gate is None:
            self.respond_debug("Current filament is None!")
            if self.is_filament_in_finda():
                self.respond_debug("But there is a filament in FINDA!")
                if self.current_gate is None:
                    self.respond_debug("Current gate is also None!")
                    self.respond_debug("Cancelling unload!!!")
                    return False
                self.respond_debug(f"Current gate is {self.current_gate}")
                self.loaded_gate = self.current_gate
                self.respond_debug(f"Also setting loaded gate to {self.loaded_gate}")
                return True
            # filament is not in FINDA
            self.respond_debug("And no filament in FINDA")
            self.respond_debug("No need to unload!")
            return True
        self.respond_debug(f"Loaded gate is {self.loaded_gate}")

        # nothing to unload, so no unload hooks either
        run_hooks = self.filament_pos > FilamentPos.UNLOADED
        if run_hooks and not self.run_user_macro(PRE_UNLOAD_MACRO):
            return False

        if self.tip_formed:
            self.respond_debug(f"The tip of gate {self.loaded_gate} is already formed")
        elif self.is_filament_in_switch_sensor():
            if self.enable_filament_cutter:
                self.respond_debug(f"Cut gate {self.loaded_gate}")
                self.cut_tip()
                self.tip_formed = True
            elif (
                self.force_form_tip_standalone
                and self.filament_pos == FilamentPos.LOADED
            ):
                # instead of the slicer's ramming, only from the nozzle
                self.respond_debug(f"Form the tip of gate {self.loaded_gate}")
                if self.current_gate is not None and not self.unselect_gate():
                    return False
                self.form_tip()
                # a retry of the unload doesn't ram it again
                self.filament_pos = FilamentPos.IN_HOTEND
                self.tip_formed = True

        self.respond_debug(f"MMU_UNLOAD {self.loaded_gate}")
        # planner runs only the steps still needed to reach UNLOADED
        if not self.move_filament_to(FilamentPos.UNLOADED, self.loaded_gate):
            return False
        self.tip_formed = False
        return not run_hooks or self.run_user_macro(POST_UNLOAD_MACRO)

    def eject_from_extruder(self) -> bool:
        """Preheat the heater if needed and unload the filament with ramming.

        Eject from nozzle to extruder gear out.

        Returns:
            bool: True, if the filament is ejected from extruder, False
                otherwise.
        """
        if self.is_paused:
            return False

        if not self.is_filament_in_switch_sensor():
            self.respond_debug("Filament not in extruder")
            return True

        self.respond_debug("Filament in hotend, trying to eject it ...")
        self.respond_debug("Preheat Nozzle")
        min_temp = max(self.get_extruder_temperature(), self.extruder_eject_temp)
        with self.running_action(ACTION_HEATING):
            self.gcode.run_script_from_command(f"M109 S{min_temp}")
        return self.unload_filament_from_hotend_with_ramming()

    def eject_before_home(self) -> None:
        """Eject from extruder gear to MMU3.

        Returns:
            bool: True, if filament ejected, False otherwise.
        """
        self.respond_debug("Eject Filament if loaded ...")
        if self.is_filament_in_switch_sensor():
            if not self.eject_from_extruder():
                return False
            if self.is_filament_in_switch_sensor():
                return False

        if not self.enable_no_selector_mode:
            if self.is_filament_in_finda():
                if not self.unload_filament_from_extruder():
                    return False
                if self.is_filament_in_finda():
                    return False
                self.respond_debug("Filament ejected !")
            else:
                self.respond_debug("Filament already ejected !")
        else:
            self.respond_debug("Filament already ejected !")

        return True

    def cmd_endstops_status(self, gcmd: GCodeCommand) -> bool:
        """Print the status of all endstops.

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        # Query the endstops
        print_time = self.toolhead.get_last_move_time()

        # Report results
        self.respond_info("Endstop status")
        self.respond_info("==============")
        self.respond_info(f"Extruder : {self.is_filament_in_switch_sensor()}")
        self.respond_info(
            f"{STEPPER_NAME_MAP[PULLEY_STEPPER_NAME]} : "
            f"{int(self.is_filament_in_finda())}"
        )
        self.respond_info(
            f"{STEPPER_NAME_MAP[SELECTOR_STEPPER_NAME]} : "
            f"{self.selector_stepper_endstop.query_endstop(print_time)}"
        )

        return True

    def cmd_mmu_stats(self, gcmd: GCodeCommand) -> bool:
        """Print a summary of the lifetime and current-job operation statistics.

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        for label, stats in (
            ("Total (lifetime)", self.total_stats),
            ("Current job", self.job_stats),
        ):
            self.respond_info(f"{label} statistics")
            self.respond_info("=" * (len(label) + 11))
            for kind in OperationKind:
                attempts = stats.attempts.get(kind, 0)
                failures = stats.failures.get(kind, 0)
                self.respond_info(f"{kind.value}: {attempts} ({failures} failed)")
            if stats.toolchanges:
                self.respond_info("toolchanges:")
                for (from_tool, to_tool), count in sorted(stats.toolchanges.items()):
                    self.respond_info(f"  T{from_tool} -> T{to_tool}: {count}")

        return True

    def cmd_mmu_stats_reset_job(self, gcmd: GCodeCommand) -> bool:
        """Reset the current-job operation statistics.

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.job_stats.reset()
        return True

    def cmd_mmu_print_start(self, gcmd: GCodeCommand) -> bool:
        """Start the print job, ``MMU_PRINT_START``.

        Resets the job stats and sets ``print_state`` to ``printing``, unless
        a print is already in progress (e.g. ``print_stats`` started it).
        Then warns about the tools of the slicer tool map that don't match the
        gates, see :meth:`check_slicer_tool_map`.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.on_print_start()
        self.check_slicer_tool_map()
        return True

    def check_slicer_tool_map(self) -> None:
        """Warn about the tools of the slicer tool map that don't match.

        A tool the print uses that maps to an empty gate, or to a gate with a
        different material, see :func:`slicer_tool_map_warnings`. Only warns,
        the print goes on. Nothing is checked while the MMU is disabled.
        """
        if not self.is_enabled:
            return
        warnings = slicer_tool_map_warnings(
            self.slicer_tool_map, self.ttg_map, self.gate_map
        )
        if not warnings:
            return
        self.respond_info(
            "\n".join(
                [
                    "Warning: the print doesn't match the gates:",
                    *warnings,
                    "Check the gates (MMU_GATE_MAP) or remap the tools (MMU_TTG_MAP).",
                ]
            )
        )

    def cmd_mmu_print_end(self, gcmd: GCodeCommand) -> bool:
        """End the print job, ``MMU_PRINT_END STATE=complete``.

        Sets ``print_state`` to ``STATE`` (``complete`` by default). Does
        nothing if no print is in progress.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        state = gcmd.get("STATE", "complete").lower()
        if state not in PRINT_END_STATES:
            raise gcmd.error(
                f"Unknown STATE '{state}', use one of {', '.join(PRINT_END_STATES)}"
            )
        self.on_print_end(state)
        return True

    @auto_pause
    @auto_disable_steppers
    def cmd_home_idler(self, gcmd: GCodeCommand) -> bool:
        """Home the idler.

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.home_idler()

    @auto_pause
    @track_operation(OperationKind.HOME)
    @measure_duration
    @auto_disable_steppers
    def cmd_home_mmu(self, gcmd: GCodeCommand) -> bool:
        """Home the MMU.

        Eject filament if loaded with eject_before_home()
        next home the mmu with home_mmu_only()

        Args:
            gcmd (GcodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.home_mmu()

    def cmd_pause(self, gcmd: GCodeCommand) -> bool:
        """Pause the MMU.

        Park the extruder at the parking position
        Save the current state and start the delayed stop of the heated modify
        the timeout of the printer accordingly to timeout_pause.

        Args:
            gcmd: (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if not self.is_enabled:
            self.display_status_msg("MMU is not enabled!")
            return True

        return self.pause()

    def cmd_resume(self, gcmd: GCodeCommand) -> bool:
        """Resume the MMU and the print.

        ``FORCE=1`` clears any pending operation and resumes without retrying it.

        Args:
            gcmd: (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if not self.is_enabled:
            self.display_status_msg("MMU is not enabled!")
            return True

        return self.resume(force=bool(gcmd.get_int("FORCE", 0)))

    @auto_pause
    @auto_disable_steppers
    def cmd_mmu_retry(self, gcmd: GCodeCommand) -> bool:
        """Retry the operation that failed and left the MMU paused.

        Reads ``pending_operation`` so the operator does not have to remember
        the original command (e.g. which ``Tx``).

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if the pending operation completed, False otherwise.
        """
        return self.retry_pending_operation()

    @auto_pause
    @track_operation(OperationKind.TOOL_CHANGE)
    @measure_duration
    @auto_disable_steppers
    def cmd_tx(
        self, gcmd: GCodeCommand, tool_id: int = 0, gate: None | int = None
    ) -> bool:
        """The generic Tx command.

        Args:
            gcmd (GCodeCommand): The G-code command.
            tool_id (int, optional): The tool id to load. Defaults to 0.
            gate (None | int, optional): The gate to load, bypassing the
                tool-to-gate map. Defaults to the gate ``tool_id`` maps to.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if gate is None:
            gate = self.tool_to_gate(tool_id)
        previous_tool = self.loaded_tool
        previous_gate = self.loaded_gate
        self.selected_tool = tool_id

        to_text = f"T{tool_id}"
        if gate != tool_id:
            to_text = f"{to_text} (gate {gate})"
        if previous_gate is not None:
            status_message = f"{tool_text(previous_tool, previous_gate)} => {to_text}"
        else:
            status_message = to_text
        self.display_status_msg(status_message)

        # sync the tracked position with the sensors before planning so the
        # planner cannot skip steps because of stale state
        self.assess_filament_pos()

        if self.loaded_gate == gate and self.filament_pos == FilamentPos.LOADED:
            return True

        with (
            FilamentSwitchSensorManager(
                self.filament_switch_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
            FilamentMotionSensorManager(
                self.filament_motion_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
        ):
            for i in range(self.tool_change_retry):
                if i > 0:
                    self.display_status_msg(f"Retry ({i + 1}): {to_text}...")
                    # re-sync the tracked position with the sensors so the
                    # planner resumes from where the filament actually is
                    self.assess_filament_pos()

                if i in range(1, self.tool_change_retry - 1):
                    # on last try we'll home the mmu
                    self.home_idler()

                if not self.unload_gate():
                    self.respond_debug(f"Unload gate {self.loaded_gate} failed!")
                    continue

                # if this is the last try, do a homing move as a last resort
                if i == self.tool_change_retry - 1:
                    self.home_mmu()

                if not self.load_gate(gate):
                    self.respond_debug(f"Load {to_text} failed!")
                    continue
                break
            else:
                # all retries exhausted - auto_pause promotes the tracked
                # operation to pending_operation and shows the recovery prompt
                self.respond_debug(f"{status_message} failed!")
                return False

        self.respond_debug(f"Done {status_message}")
        return True

    @auto_pause
    @track_operation(OperationKind.CUT)
    @auto_disable_steppers
    def cmd_kx(self, gcmd: GCodeCommand, tool_id: int = 0) -> bool:
        """The generic Kx command.

        Args:
            gcmd (GCodeCommand): The G-code command.
            tool_id (int, optional): The tool id to cut. Defaults to 0.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.cut_filament_in_mmu(self.tool_to_gate(tool_id))

    @auto_disable_steppers
    def cmd_unlock(self, gcmd: GCodeCommand) -> bool:
        """Park the idler, stop the delayed stop of the heater.

        Unlocking does not resolve whatever operation originally failed, so
        ``pending_operation`` is left untouched. It is not re-shown here
        though - "Unlock MMU" does not close the recovery dialog server-side
        (see :func:`auto_pause`), so there is nothing to bring back.

        Args:
            gcmd (GCodeCommand): The G-code command.
        """
        return self.unlock()

    @auto_pause
    @track_operation(OperationKind.LOAD)
    @measure_duration
    @auto_disable_steppers
    def cmd_load_gate(self, gcmd: GCodeCommand) -> bool:
        """Load filament from MMU3 to nozzle.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        tool, gate = get_tool_and_gate_params(gcmd, self.ttg_map)
        if tool is not None:
            self.selected_tool = tool
        if gate is None:
            gate = self.default_gate()
        self.assess_filament_pos()
        return self.load_gate(gate)

    @auto_pause
    @track_operation(OperationKind.UNLOAD)
    @measure_duration
    @auto_disable_steppers
    def cmd_unload_gate(self, gcmd: GCodeCommand) -> bool:
        """Unload filament from nozzle to MMU3.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        with (
            FilamentSwitchSensorManager(
                self.filament_switch_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
            FilamentMotionSensorManager(
                self.filament_motion_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
        ):
            self.assess_filament_pos()
            return self.unload_gate()

    @auto_pause
    @measure_duration
    @auto_disable_steppers
    def cmd_select_gate(self, gcmd: GCodeCommand) -> bool:
        """Select a gate. move the idler and then move the selector (if needed).

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        tool, gate = get_tool_and_gate_params(gcmd, self.ttg_map)
        if tool is not None:
            self.selected_tool = tool
        return self.select_gate(gate)

    @auto_pause
    @measure_duration
    @auto_disable_steppers
    def cmd_unselect_gate(self, gcmd: GCodeCommand) -> bool:
        """Unselect the gate, only park the idler.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.unselect_gate()

    def default_gate(self) -> None | int:
        """Return the gate a command without ``GATE=`` acts on.

        Returns:
            None | int: The selected gate, else the gate whose filament is in
                the path, else None.
        """
        if self.current_gate is not None:
            return self.current_gate
        return self.loaded_gate

    def cmd_not_supported(self, gcmd: GCodeCommand, name: str = "") -> bool:
        """Answer a Happy Hare command that has no MMU3 equivalent.

        Registered so the Mainsail / Fluidd MMU panel buttons do not error.

        Args:
            gcmd (GCodeCommand): The G-code command.
            name (str): The command name.

        Returns:
            bool: Always True.
        """
        self.respond_info(f"{name} is not supported on MMU3.")
        return True

    def cmd_motors_off(self, gcmd: GCodeCommand) -> bool:
        """Turn off the MMU3 stepper motors.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if the steppers are disabled.
        """
        return self.disable_steppers()

    @auto_disable_steppers
    def cmd_mmu_form_tip(self, gcmd: GCodeCommand) -> bool:
        """Tune and form the tip, ``MMU_FORM_TIP`` / ``MMU_TEST_FORM_TIP``.

        Same as Happy Hare, where ``MMU_FORM_TIP`` is an alias of
        ``MMU_TEST_FORM_TIP``. Any other parameter sets the
        ``_MMU_FORM_TIP_VARS`` variable of that name (with or without the
        ``variable_`` prefix) until Klipper restarts, the values before the
        first change are kept for ``RESET=1``. ``SHOW=1`` lists the variables
        without forming a tip, ``RUN=0`` only sets them. The tip is formed
        like :meth:`form_tip_standalone`.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if the tip was formed (or the variables were shown /
                set), False otherwise.
        """
        reset = gcmd.get_int("RESET", 0, minval=0, maxval=1)
        show = gcmd.get_int("SHOW", 0, minval=0, maxval=1)
        run = gcmd.get_int("RUN", 1, minval=0, maxval=1)
        # without _MMU_FORM_TIP_VARS the variables are the macro's own
        vars_macro = self.printer.lookup_object(
            f"gcode_macro {FORM_TIP_VARS_MACRO}", None
        ) or self.printer.lookup_object(f"gcode_macro {FORM_TIP_MACRO}", None)
        overrides = {}
        for name, value in gcmd.get_command_parameters().items():
            if name.upper() in TEST_FORM_TIP_PARAMS:
                continue
            name = name.lower().removeprefix("variable_")
            if vars_macro is None or name not in vars_macro.variables:
                raise gcmd.error(
                    f"Unknown tip forming variable '{name}', "
                    "MMU_TEST_FORM_TIP SHOW=1 lists them."
                )
            overrides[name] = parse_macro_variable(value)

        if reset:
            if self.form_tip_defaults is not None:
                vars_macro.variables = dict(self.form_tip_defaults)
                self.form_tip_defaults = None
                self.respond_info("Tip forming variables reset to the config values.")
            show = 1
        elif overrides:
            if self.form_tip_defaults is None:
                self.form_tip_defaults = dict(vars_macro.variables)
            vars_macro.variables = {**vars_macro.variables, **overrides}

        if show or overrides or gcmd.get_command() == "MMU_TEST_FORM_TIP":
            self.print_form_tip_vars(vars_macro)
        if show or not run:
            return True
        return self.form_tip_standalone()

    def print_form_tip_vars(self, vars_macro: object | None) -> None:
        """Print the tip forming variables, as lines to copy into ``mmu.cfg``.

        Args:
            vars_macro (object | None): The ``gcode_macro`` holding them.
        """
        variables = {} if vars_macro is None else vars_macro.variables
        if not variables:
            self.respond_info(f"{FORM_TIP_MACRO} has no variables to tune.")
            return
        changed = ""
        if self.form_tip_defaults is not None:
            changed = " (changed, RESET=1 restores)"
        lines = [f"Tip forming variables{changed}:"]
        lines += [
            f"variable_{name}: {value!r}" for name, value in sorted(variables.items())
        ]
        self.respond_info("\n".join(lines))

    @auto_disable_steppers
    def cmd_mmu_cut(self, gcmd: GCodeCommand) -> bool:
        """Cut the loaded filament in the extruder, ``MMU_CUT``.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if the filament was cut, False otherwise.
        """
        return self.form_tip_standalone(cut=True)

    def cmd_mmu_load(self, gcmd: GCodeCommand) -> bool:
        """Load the filament of a gate (``GATE=`` / ``TOOL=``) to the nozzle.

        Without a gate the selected gate is loaded.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if gcmd.get_int("EXTRUDER_ONLY", 0):
            self.respond_info("MMU_LOAD EXTRUDER_ONLY=1 is not supported on MMU3.")
            return False
        if (
            get_gate_param(gcmd, ttg_map=self.ttg_map) is None
            and self.default_gate() is None
        ):
            self.respond_info("No gate selected, use MMU_LOAD GATE=<gate>.")
            return False
        return self.cmd_load_gate(gcmd)

    def cmd_mmu_unload(self, gcmd: GCodeCommand) -> bool:
        """Unload the filament from the nozzle to the MMU3.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if gcmd.get_int("EXTRUDER_ONLY", 0):
            self.respond_info("MMU_UNLOAD EXTRUDER_ONLY=1 is not supported on MMU3.")
            return False
        return self.cmd_unload_gate(gcmd)

    def cmd_mmu_eject(self, gcmd: GCodeCommand) -> bool:
        """Unload the filament back into its gate and park the idler.

        ``GATE=`` is accepted for Happy Hare compatibility. Only the loaded
        gate can be ejected.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        gate = get_gate_param(gcmd, ttg_map=self.ttg_map)
        if gate is not None and gate != self.loaded_gate:
            self.respond_info(f"Gate {gate} is not loaded, nothing to eject.")
            return True
        return self.cmd_m702(gcmd)

    def cmd_mmu_select(self, gcmd: GCodeCommand) -> bool:
        """Select a gate (``GATE=`` / ``TOOL=``).

        Refuses to move the selector away from a gate whose filament is
        still in the path, as that would drag the selector across it.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if gcmd.get_int("BYPASS", 0):
            self.respond_info("MMU_SELECT BYPASS=1 is not supported on MMU3.")
            return False
        gate = get_gate_param(gcmd, ttg_map=self.ttg_map)
        if (
            self.filament_pos != FilamentPos.UNLOADED
            and self.loaded_gate is not None
            and gate != self.loaded_gate
        ):
            self.respond_info(
                f"Gate {self.loaded_gate} is loaded, unload it before "
                f"selecting another gate."
            )
            return False
        return self.cmd_select_gate(gcmd)

    def cmd_mmu_change_tool(self, gcmd: GCodeCommand) -> bool:
        """Change to a tool (``TOOL=``) or gate (``GATE=``), same as ``Tn``.

        ``TOOL=`` loads the gate the tool maps to, ``GATE=`` bypasses the
        tool-to-gate map and loads the gate as the tool that maps to it.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        tool, gate = get_tool_and_gate_params(gcmd, self.ttg_map)
        if not self.gate_map.is_valid_gate(gate):
            self.respond_info(f"MMU_CHANGE_TOOL needs a valid TOOL= or GATE= ({gate})")
            return False
        if gcmd.get_int("GATE", None) is not None:
            # GATE= names the gate, load it as the tool mapped to it
            tool = self.gate_to_tool(gate)
            if tool is None:
                self.respond_info(
                    f"No tool maps to gate {gate}, map one with MMU_TTG_MAP."
                )
                return False
        return self.cmd_tx(gcmd, tool_id=tool, gate=gate)

    def cmd_mmu_preload(self, gcmd: GCodeCommand) -> bool:
        """Check a gate by feeding its filament to FINDA and back.

        Without ``GATE=`` / ``TOOL=`` the selected gate is preloaded.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        gate = get_gate_param(gcmd, ttg_map=self.ttg_map)
        if gate is None:
            gate = self.current_gate
        if not self.gate_map.is_valid_gate(gate):
            self.respond_info("No gate selected, use MMU_PRELOAD GATE=<gate>.")
            return False
        if self.filament_pos != FilamentPos.UNLOADED:
            self.respond_info(
                f"Gate {self.loaded_gate} is loaded, unload it before preloading."
            )
            return False
        return self.cmd_preload_filament_to_finda(gcmd, gate=gate)

    def get_check_gates_param(
        self, gcmd: GCodeCommand, check_all: bool
    ) -> None | list[int]:
        """Return the gates ``MMU_CHECK_GATE`` / ``MMU_CHECK_GATES`` check.

        Reads Happy Hare's ``ALL=1``, ``GATES=``, ``TOOLS=``, ``GATE=`` and
        ``TOOL=`` in that order. Tools are resolved to their gates through the
        tool-to-gate map.

        Args:
            gcmd (GCodeCommand): The G-code command.
            check_all (bool): Check all gates when no parameter is given,
                otherwise check the selected gate.

        Raises:
            gcmd.error: If a gate is not an integer or does not exist.

        Returns:
            None | list[int]: The gates to check, None if no gate is given
                and none is selected.
        """
        all_gates = list(range(self.gate_map.num_gates))
        if gcmd.get_int("ALL", 0, minval=0, maxval=1):
            return all_gates

        gates = get_gate_list_param(gcmd, "GATES")
        if gates is None:
            tools = get_gate_list_param(gcmd, "TOOLS")
            if tools is not None:
                gates = [self.tool_to_gate(tool) for tool in tools]
        if gates is None:
            gate = gcmd.get_int("GATE", None)
            if gate is None:
                gate = self.tool_to_gate(gcmd.get_int("TOOL", None))
            if gate is not None:
                gates = [gate]
        if gates is None:
            if check_all:
                return all_gates
            if self.current_gate is None:
                return None
            gates = [self.current_gate]

        for gate in gates:
            if not self.gate_map.is_valid_gate(gate):
                raise gcmd.error(f"Invalid gate: {gate}")
        # keep the order but check each gate once
        return list(dict.fromkeys(gates))

    def cmd_mmu_check_gate(self, gcmd: GCodeCommand) -> bool:
        """Check the selected gate, or the ones given, for filament.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.check_gates_command(gcmd, check_all=False)

    def cmd_mmu_check_gates(self, gcmd: GCodeCommand) -> bool:
        """Check all gates, or the ones given, for filament.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.check_gates_command(gcmd, check_all=True)

    def check_gates_command(self, gcmd: GCodeCommand, check_all: bool) -> bool:
        """Validate a gate check request and run it.

        Refused without pausing the MMU while filament is loaded, as checking
        feeds each gate to FINDA.

        Args:
            gcmd (GCodeCommand): The G-code command.
            check_all (bool): Check all gates when no parameter is given.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        gates = self.get_check_gates_param(gcmd, check_all)
        if gates is None:
            self.respond_info("No gate selected, use MMU_CHECK_GATE GATE=<gate>.")
            return False
        if self.filament_pos != FilamentPos.UNLOADED:
            self.respond_info(
                f"Gate {self.loaded_gate} is loaded, unload it before checking gates."
            )
            return False
        if self.enable_no_selector_mode:
            self.respond_info("Checking gates is not supported in no selector mode.")
            return False
        quiet = bool(gcmd.get_int("QUIET", 0, minval=0, maxval=1))
        return self.cmd_check_gates(gcmd, gates=gates, quiet=quiet)

    @auto_pause
    @auto_disable_steppers
    def cmd_check_gates(
        self, gcmd: GCodeCommand, gates: list[int], quiet: bool = False
    ) -> bool:
        """Check the given gates for filament.

        Args:
            gcmd (GCodeCommand): The G-code command.
            gates (list[int]): The gates to check.
            quiet (bool): Do not print the summary.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.check_gates(gates, quiet=quiet)

    def cmd_mmu_recover(self, gcmd: GCodeCommand) -> bool:
        """Recover the MMU state.

        Without arguments, retries the pending operation (``MMU_RETRY``) or,
        if there is none, re-reads the filament position from the sensors.

        ``GATE=`` / ``TOOL=`` and ``LOADED=0|1`` tell the MMU3 where the
        filament actually is (e.g. after fixing it by hand); the sensors still
        have the final say. A ``TOOL=`` alone is the gate it maps to,
        ``TOOL=`` with a different ``GATE=`` remaps the tool to that gate
        (Happy Hare's ``MMU_RECOVER TOOL=2 GATE=3``).

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        tool, gate = get_tool_and_gate_params(gcmd, self.ttg_map, minval=-1)
        loaded = gcmd.get_int("LOADED", None, minval=0, maxval=1)

        if gate is None and loaded is None:
            if self.pending_operation is not None:
                return self.cmd_mmu_retry(gcmd)
        else:
            if (
                tool is not None
                and tool != -1
                and not self.gate_map.is_valid_gate(tool)
            ):
                raise gcmd.error(f"Invalid tool: {tool}")
            if gate is not None:
                if gate != -1 and not self.gate_map.is_valid_gate(gate):
                    raise gcmd.error(f"Invalid gate: {gate}")
                self.loaded_gate = gate if gate >= 0 else None
            if tool is not None and tool >= 0:
                self.selected_tool = tool
                if gate is not None and gate >= 0 and self.tool_to_gate(tool) != gate:
                    ttg_map = list(self.ttg_map)
                    ttg_map[tool] = gate
                    self.set_ttg_map(ttg_map)
                    self.respond_info(f"Remapped T{tool} to gate {gate}.")
            if loaded == 1:
                if self.loaded_gate is None:
                    raise gcmd.error("LOADED=1 needs a GATE=")
                self.filament_pos = FilamentPos.LOADED
                self.tip_formed = False
            elif loaded == 0:
                self.filament_pos = FilamentPos.UNLOADED
                self.loaded_gate = None

        self.assess_filament_pos()
        self.sync_active_spool()
        loaded_text = tool_text(self.loaded_tool, self.loaded_gate) or "none"
        if self.loaded_gate is not None and self.loaded_tool is not None:
            loaded_text = f"{loaded_text} (gate {self.loaded_gate})"
        self.respond_info(
            f"Filament position: {self.filament_pos}, filament: "
            f"{loaded_text}, selected gate: {self.current_gate}"
        )
        return True

    def cmd_mmu_runout(self, gcmd: GCodeCommand) -> bool:
        """Handle a filament runout: continue with endless spool, or pause.

        Klipper's ``filament_switch_sensor`` has no runout event to listen to,
        this is meant to be called from its ``runout_gcode``, which Klipper
        only runs while printing and while the sensor is enabled. The MMU
        disables the sensor during its own loads and unloads, and the command
        ignores the runout if the MMU is moving filament.

        The loaded gate is marked empty. With endless spool enabled and a
        print in progress, the loaded tool is remapped to the next gate of its
        group and loaded, and the print continues. Otherwise the print is
        paused (``PAUSE``).

        A runout with filament still in FINDA means the filament broke or got
        stuck between FINDA and the sensor, the spool is not empty so the gate
        is not marked empty and the print is paused.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if the print continues or was paused, False if the
                endless spool tool change failed (the MMU is then paused).
        """
        if self.action != ACTION_IDLE:
            self.respond_info(f"MMU is busy ({self.action}), runout ignored.")
            return True
        if not self.is_enabled:
            return self.pause_on_runout("Filament runout, the MMU is disabled.")
        gate = self.loaded_gate
        if gate is None or self.filament_pos != FilamentPos.LOADED:
            return self.pause_on_runout("Filament runout, no filament loaded.")
        if not self.enable_no_selector_mode and self.is_filament_in_finda():
            return self.pause_on_runout(
                f"Filament runout on gate {gate}, but FINDA still detects "
                "filament. The nozzle may be clogged, or the filament tangled, "
                "broken or stuck in the bowden, the gate is not marked empty."
            )
        if self.is_in_print and self.is_filament_in_switch_sensor():
            # a sensor before the switch sensor (e.g. a motion sensor)
            return self.wait_for_runout_tail(gate)
        self.clear_runout_tail()
        self.set_gate_status(gate, GATE_EMPTY)
        message = f"Gate {gate} ran out of filament, marked empty."
        if not self.endless_spool_enabled:
            return self.pause_on_runout(message)
        tool = self.loaded_tool
        if tool is None:
            return self.pause_on_runout(
                f"{message} No tool maps to gate {gate}, endless spool can't continue."
            )
        if not self.is_in_print:
            return self.pause_on_runout(
                f"{message} Not printing, endless spool only continues a print."
            )
        next_gate, checked = self.next_endless_spool_gate(gate)
        group = group_name(self.endless_spool_groups[gate])
        checked_text = ", ".join(str(g) for g in checked) or "none"
        if next_gate is None:
            return self.pause_on_runout(
                f"{message} Endless spool: no gate left for T{tool} in group "
                f"{group} (checked gates: {checked_text})."
            )
        self.respond_info(
            f"{message} Endless spool: T{tool} continues with gate {next_gate} "
            f"(group {group})."
        )
        return self.endless_spool_swap(gcmd, tool, next_gate)

    def wait_for_runout_tail(self, gate: int) -> bool:
        """Keep printing until the end of the filament reaches the switch sensor.

        Called when a sensor before the filament switch sensor (e.g. a motion
        sensor) sees a runout. With FINDA empty the spool ran out, but the end
        of the filament is past the MMU pulley and can't be unloaded. The
        print uses it up, and the switch sensor's ``MMU_RUNOUT`` then continues
        with endless spool or pauses. If the extruder uses more than
        ``runout_tail_length`` before that, the end of the filament is stuck
        and the print pauses (see :meth:`_check_runout_tail`).

        Without FINDA (``enable_no_selector_mode``) a clog can't be told from
        a runout, so the print pauses instead.

        Args:
            gate (int): The gate that ran out.

        Returns:
            bool: Always True.
        """
        if self.enable_no_selector_mode:
            return self.pause_on_runout(
                f"Filament runout on gate {gate} before the filament switch "
                "sensor. Without FINDA a clog can't be told from a runout, the "
                "gate is not marked empty."
            )
        self.set_gate_status(gate, GATE_EMPTY)
        if self.runout_tail_gate == gate:
            # already waiting, keep the first position
            return True
        self.respond_info(
            f"Gate {gate} ran out of filament, marked empty. Printing on until "
            "the end of the filament reaches the filament switch sensor."
        )
        self.runout_tail_gate = gate
        self.runout_tail_start = self.extruder_position(self.reactor.monotonic())
        self.reactor.update_timer(self._runout_tail_timer, self.reactor.NOW)
        return True

    def clear_runout_tail(self) -> None:
        """Stop waiting for the end of the filament to reach the switch sensor."""
        self.runout_tail_gate = None
        if self._runout_tail_timer is not None:
            self.reactor.update_timer(self._runout_tail_timer, self.reactor.NEVER)

    def extruder_position(self, eventtime: float) -> float:
        """Return the extruder position, the same way Klipper's motion sensor does.

        Args:
            eventtime (float): The reactor event time.

        Returns:
            float: The extruder position in mm.
        """
        mcu = self.printer.lookup_object("mcu")
        return self.extruder.find_past_position(mcu.estimated_print_time(eventtime))

    def _check_runout_tail(self, eventtime: float) -> float:
        """Pause the print if the end of the filament doesn't arrive in time.

        Stops watching if the print ended or the gate was unloaded / changed
        in the meantime (e.g. by the switch sensor's ``MMU_RUNOUT``).

        Args:
            eventtime (float): The reactor event time.

        Returns:
            float: The next time this timer should fire.
        """
        gate = self.runout_tail_gate
        if (
            gate is None
            or not self.is_in_print
            or self.loaded_gate != gate
            or self.filament_pos != FilamentPos.LOADED
        ):
            self.runout_tail_gate = None
            return self.reactor.NEVER
        used = self.extruder_position(eventtime) - self.runout_tail_start
        if used <= self.runout_tail_length:
            return eventtime + RUNOUT_TAIL_CHECK_INTERVAL
        self.runout_tail_gate = None
        # G-code can't run in a timer, pause from a callback like Klipper's
        # runout sensors do
        self.reactor.register_callback(partial(self._pause_stuck_runout_tail, gate))
        return self.reactor.NEVER

    def _pause_stuck_runout_tail(self, gate: int, eventtime: float) -> None:
        """Pause the print, the end of the filament didn't reach the switch sensor.

        Args:
            gate (int): The gate that ran out.
            eventtime (float): The reactor event time.
        """
        self.respond_info(
            f"Gate {gate} ran out of filament, but the filament switch sensor "
            f"still detects filament after {self.runout_tail_length:.0f} mm. The "
            "end of the filament may be stuck in the bowden. Pausing the print."
        )
        # pausing from an event must pause the print immediately, see
        # Klipper's filament_switch_sensor
        pause_resume = self.printer.lookup_object("pause_resume", None)
        if pause_resume is not None:
            pause_resume.send_pause_command()
        try:
            self.gcode.run_script("PAUSE")
        except Exception:
            logger.exception("mmu: pausing the print failed")

    def pause_on_runout(self, message: str) -> bool:
        """Report why a runout pauses the print and pause it.

        The print is paused with ``PAUSE`` (the print's pause macro), the MMU
        itself is not paused: after fixing the filament, ``RESUME`` continues.

        Args:
            message (str): Why the print can't continue.

        Returns:
            bool: Always True.
        """
        self.respond_info(f"{message} Pausing the print.")
        self.gcode.run_script_from_command("PAUSE")
        return True

    def endless_spool_swap(self, gcmd: GCodeCommand, tool: int, gate: int) -> bool:
        """Continue ``tool`` with ``gate`` after a runout and resume the print.

        The print is paused first, so its ``PAUSE`` / ``RESUME`` macros park
        and unpark the toolhead around the tool change, then the tool is
        remapped to ``gate`` and loaded like ``Tn``. If the tool change
        fails, the MMU pauses and shows the recovery prompt, ``RESUME_MMU``
        retries it and resumes the print.

        The optional ``_MMU_ENDLESS_SPOOL_PRE_UNLOAD`` macro runs before the
        tool change and ``_MMU_ENDLESS_SPOOL_POST_LOAD`` after it (e.g. to
        wipe the nozzle), both while the print is paused. If one fails, the
        print stays paused and ``RESUME`` continues it.

        Args:
            gcmd (GCodeCommand): The G-code command.
            tool (int): The tool that ran out.
            gate (int): The gate to continue with.

        Returns:
            bool: True if the print resumed, False otherwise.
        """
        self.gcode.run_script_from_command("PAUSE")
        self.is_handling_runout = True
        try:
            if not self.run_user_macro(ENDLESS_SPOOL_PRE_UNLOAD_MACRO):
                return False
            ttg_map = list(self.ttg_map)
            ttg_map[tool] = gate
            self.set_ttg_map(ttg_map)
            self.respond_info(f"Remapped T{tool} to gate {gate}.")
            if not self.cmd_tx(gcmd, tool_id=tool, gate=gate):
                return False
            if not self.run_user_macro(ENDLESS_SPOOL_POST_LOAD_MACRO):
                return False
        finally:
            self.is_handling_runout = False
        self.gcode.run_script_from_command("RESUME")
        return True

    def cmd_mmu_endless_spool(self, gcmd: GCodeCommand) -> bool:
        """Show or edit endless spool, Happy Hare's ``MMU_ENDLESS_SPOOL``.

        ``ENABLE=0|1`` disables / enables it, ``GROUPS=g,g,...`` sets the group
        of each gate (in gate order, gates with the same number form a group),
        ``RESET=1`` goes back to the ``[mmu]`` config values. ``QUIET=1`` does
        not print the settings, no arguments prints them.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        quiet = gcmd.get_int("QUIET", 0, minval=0, maxval=1)
        if gcmd.get_int("RESET", 0, minval=0, maxval=1):
            self.set_endless_spool(
                enabled=self.default_endless_spool_enabled,
                groups=self.default_endless_spool_groups,
            )
        else:
            enabled = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
            groups_param = gcmd.get("GROUPS", None)
            groups = None
            if groups_param is not None:
                groups = self.parse_endless_spool_groups(gcmd, groups_param)
            if enabled is None and groups is None:
                quiet = 0
            self.set_endless_spool(
                enabled=None if enabled is None else bool(enabled), groups=groups
            )
        if not quiet:
            self.print_endless_spool()
        return True

    def parse_endless_spool_groups(self, gcmd: GCodeCommand, value: str) -> list[int]:
        """Return the groups given with ``MMU_ENDLESS_SPOOL GROUPS=``.

        Args:
            gcmd (GCodeCommand): The G-code command.
            value (str): The comma separated groups, in gate order.

        Raises:
            gcmd.error: If a group is not a non-negative number or there is not
                one group per gate.

        Returns:
            list[int]: The group of each gate.
        """
        parts = [part.strip() for part in value.strip().strip("'\"").split(",")]
        if len(parts) != self.number_of_tools:
            raise gcmd.error(
                f"GROUPS= has {len(parts)} groups, it needs one for each of the "
                f"{self.number_of_tools} gates."
            )
        groups = []
        for part in parts:
            try:
                group = int(part)
            except ValueError:
                raise gcmd.error(f"Invalid group in GROUPS=: {part}") from None
            if group < 0:
                raise gcmd.error(f"Invalid group in GROUPS=: {group}")
            groups.append(group)
        return groups

    def endless_spool_group_gates(self, gate: int) -> list[int]:
        """Return the gates in the endless spool group of ``gate``.

        Args:
            gate (int): The gate.

        Returns:
            list[int]: The gates of the group, starting with ``gate`` and in
                the order endless spool tries them.
        """
        num_gates = self.number_of_tools
        group = self.endless_spool_groups[gate]
        return [
            (gate + offset) % num_gates
            for offset in range(num_gates)
            if self.endless_spool_groups[(gate + offset) % num_gates] == group
        ]

    def print_endless_spool(self) -> None:
        """Print whether endless spool is enabled and its groups."""
        state = "enabled" if self.endless_spool_enabled else "disabled"
        lines = [f"Endless spool is {state}.", "Endless spool groups:"]
        groups: dict[int, list[int]] = {}
        for gate, group in enumerate(self.endless_spool_groups):
            groups.setdefault(group, []).append(gate)
        for group, gates in groups.items():
            gates_text = ", ".join(str(g) for g in gates)
            lines.append(f"Group {group_name(group)}: gates {gates_text}")
        self.respond_info("\n".join(lines))

    def cmd_mmu_gate_map(self, gcmd: GCodeCommand) -> bool:
        """Show or edit the filament metadata of the gates.

        ``GATE=<gate>`` with any of ``NAME=``, ``MATERIAL=``, ``COLOR=``,
        ``TEMP=``, ``SPOOLID=``, ``AVAILABLE=`` and ``SPEED=`` edits a gate,
        ``RESET=1`` clears all gates and no arguments prints the map.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        if gcmd.get_int("RESET", 0):
            self.gate_map.reset()
            self.save_gate_map()
            self.sync_active_spool()
            self.respond_info("Gate map reset.")
            return True

        gate = gcmd.get_int("GATE", None)
        if gate is None:
            if gcmd.get("MAP", None) is not None:
                self.respond_info("MMU_GATE_MAP MAP= is not supported on MMU3.")
                return True
            self.print_gate_map()
            return True
        if not self.gate_map.is_valid_gate(gate):
            raise gcmd.error(f"Invalid gate: {gate}")

        fields = self.parse_gate_map_fields(gcmd)
        spool_id = fields.get("spool_id", NO_SPOOL)
        changed = False
        if spool_id != NO_SPOOL:
            # a spool can only be in one gate
            for other in self.gate_map.gates_with_spool(spool_id):
                if other != gate:
                    changed |= self.gate_map.update(other, spool_id=NO_SPOOL)
        changed |= self.gate_map.update(gate, **fields)

        if changed:
            self.save_gate_map()
            self.sync_active_spool()
        if not gcmd.get_int("QUIET", 0):
            self.print_gate_map()
        return True

    def cmd_mmu_ttg_map(self, gcmd: GCodeCommand) -> bool:
        """Show or edit the tool-to-gate map, Happy Hare's ``MMU_TTG_MAP``.

        ``TOOL= GATE=`` maps a tool to a gate, ``MAP=g,g,...`` sets the whole
        map (the gate of each tool, in tool order), ``RESET=1`` maps tool n to
        gate n again. ``GATE= AVAILABLE=`` also sets the gate's availability.
        ``QUIET=1`` does not print the map, no arguments prints it.
        ``MMU_REMAP_TTG`` is the same command.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        num_gates = self.number_of_tools
        quiet = gcmd.get_int("QUIET", 0, minval=0, maxval=1)
        ttg_map_param = gcmd.get("MAP", None)
        tool = gcmd.get_int("TOOL", None, minval=0, maxval=num_gates - 1)
        gate = gcmd.get_int("GATE", None, minval=0, maxval=num_gates - 1)
        available = gcmd.get_int(
            "AVAILABLE", None, minval=GATE_EMPTY, maxval=GATE_AVAILABLE
        )

        if gcmd.get_int("RESET", 0, minval=0, maxval=1):
            self.set_ttg_map(default_ttg_map(num_gates))
        elif ttg_map_param is not None:
            self.set_ttg_map(self.parse_ttg_map(gcmd, ttg_map_param))
        elif gate is not None:
            if tool is not None:
                ttg_map = list(self.ttg_map)
                ttg_map[tool] = gate
                self.set_ttg_map(ttg_map)
            if available is not None:
                self.set_gate_status(gate, available)
        elif tool is not None:
            raise gcmd.error("MMU_TTG_MAP TOOL= needs a GATE=")
        else:
            quiet = 0

        if not quiet:
            self.print_ttg_map()
        return True

    def parse_ttg_map(self, gcmd: GCodeCommand, value: str) -> list[int]:
        """Return the tool-to-gate map given with ``MMU_TTG_MAP MAP=``.

        Args:
            gcmd (GCodeCommand): The G-code command.
            value (str): The comma separated gates, in tool order.

        Raises:
            gcmd.error: If a gate is not a valid gate or there is not one
                gate per tool.

        Returns:
            list[int]: The map.
        """
        parts = [part.strip() for part in value.strip().strip("'\"").split(",")]
        if len(parts) != self.number_of_tools:
            raise gcmd.error(
                f"MAP= has {len(parts)} gates, it needs one for each of the "
                f"{self.number_of_tools} tools."
            )
        ttg_map = []
        for part in parts:
            try:
                gate = int(part)
            except ValueError:
                raise gcmd.error(f"Invalid gate in MAP=: {part}") from None
            if not self.gate_map.is_valid_gate(gate):
                raise gcmd.error(f"Invalid gate in MAP=: {gate}")
            ttg_map.append(gate)
        return ttg_map

    def print_ttg_map(self) -> None:
        """Print the tool-to-gate map to the console."""
        lines = ["TTG map:"]
        loaded_tool = self.loaded_tool
        for tool, gate in enumerate(self.ttg_map):
            parts = [f"T{tool} -> gate {gate}"]
            if self.gate_map.is_valid_gate(gate):
                info = self.gate_map[gate]
                parts.append(info.material or "-")
                if info.name:
                    parts.append(info.name)
                parts.append(f"[{GATE_STATUS_TEXT.get(info.status, info.status)}]")
                if self.endless_spool_enabled:
                    group = group_name(self.endless_spool_groups[gate])
                    gates = " > ".join(
                        str(g) for g in self.endless_spool_group_gates(gate)
                    )
                    parts.append(f"group {group}: {gates}")
            if tool == loaded_tool:
                parts.append("<- loaded")
            lines.append(" ".join(str(p) for p in parts))
        self.respond_info("\n".join(lines))

    def cmd_mmu_slicer_tool_map(self, gcmd: GCodeCommand) -> bool:
        """Show or set the tools the print uses, ``MMU_SLICER_TOOL_MAP``.

        Called from the slicer's start G-code, before ``MMU_PRINT_START``:
        ``RESET=1`` forgets the previous print, ``INITIAL_TOOL=`` and
        ``TOTAL_TOOLCHANGES=`` set what the print starts with and how many
        tool changes it has, ``TOOL=`` with ``COLOR=``, ``MATERIAL=``,
        ``TEMP=``, ``NAME=`` and ``USED=0|1`` sets the filament of a tool.
        These don't print the map, no arguments (or ``DETAIL=1``, which also
        lists the unused tools) prints it with the mismatches to the gates.
        Happy Hare's other parameters (``PURGE_VOLUMES=``, ``AUTOMAP=``, ...)
        are ignored.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        max_tool = self.number_of_tools - 1
        detail = gcmd.get_int("DETAIL", 0, minval=0, maxval=1)
        quiet = gcmd.get_int("QUIET", 0, minval=0, maxval=1)
        tool = gcmd.get_int("TOOL", None, minval=0, maxval=max_tool)
        initial_tool = gcmd.get_int("INITIAL_TOOL", None, minval=0, maxval=max_tool)
        total_toolchanges = gcmd.get_int("TOTAL_TOOLCHANGES", None, minval=0)

        changed = False
        if gcmd.get_int("RESET", 0, minval=0, maxval=1):
            self.slicer_tool_map.reset()
            changed = True
        if tool is not None:
            self.slicer_tool_map.set_tool(
                tool,
                color=gcmd.get("COLOR", ""),
                material=gcmd.get("MATERIAL", SLICER_MATERIAL_UNKNOWN).strip(),
                temp=gcmd.get_int("TEMP", 0, minval=0),
                name=gcmd.get("NAME", "").strip(),
                used=bool(gcmd.get_int("USED", 1, minval=0, maxval=1)),
            )
            changed = True
        if initial_tool is not None:
            self.slicer_tool_map.set_initial_tool(initial_tool)
            changed = True
        if total_toolchanges is not None:
            self.slicer_tool_map.total_toolchanges = total_toolchanges
            changed = True

        if (not changed and not quiet) or detail:
            self.print_slicer_tool_map(detail=bool(detail))
        return True

    def print_slicer_tool_map(self, detail: bool = False) -> None:
        """Print the tools the print uses and the gates they load.

        Args:
            detail (bool): Also list the tools the print doesn't use.
        """
        slicer_tool_map = self.slicer_tool_map
        if slicer_tool_map.is_empty:
            self.respond_info("No slicer tool map loaded.")
            return
        num_tools = len(slicer_tool_map.referenced_tools)
        summary = "Single color print" if num_tools <= 1 else f"{num_tools} color print"
        if slicer_tool_map.total_toolchanges is not None:
            summary += f", {slicer_tool_map.total_toolchanges} tool changes"
        lines = ["Slicer tool map:", summary]
        for tool, info in sorted(slicer_tool_map.tools.items()):
            if not info["in_use"] and not detail:
                continue
            parts = [f"T{tool} -> gate {self.tool_to_gate(tool)}:", info["material"]]
            if info["name"]:
                parts.append(info["name"])
            if info["color"]:
                parts.append(f"color={info['color']}")
            if info["temp"]:
                parts.append(f"{info['temp']}C")
            if not info["in_use"]:
                parts.append("(not used)")
            lines.append(" ".join(str(p) for p in parts))
        if slicer_tool_map.initial_tool is not None:
            lines.append(f"Initial tool: T{slicer_tool_map.initial_tool}")
        warnings = slicer_tool_map_warnings(
            slicer_tool_map, self.ttg_map, self.gate_map
        )
        if warnings:
            lines.append("Warnings:")
            lines.extend(warnings)
        self.respond_info("\n".join(lines))

    @staticmethod
    def parse_gate_map_fields(gcmd: GCodeCommand) -> dict:
        """Return the gate fields given to ``MMU_GATE_MAP``.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            dict: :class:`GateInfo` field name -> value, only the given ones.
        """
        fields = {}
        for param, field in (("NAME", "name"), ("MATERIAL", "material")):
            value = gcmd.get(param, None)
            if value is not None:
                # quotes would break the single-quoted SAVE_VARIABLE value
                fields[field] = value.replace("'", "").replace('"', "").strip()
        color = gcmd.get("COLOR", None)
        if color is not None:
            fields["color"] = color
        for param, field, minval, maxval in (
            ("TEMP", "temperature", -1, None),
            ("SPOOLID", "spool_id", -1, None),
            ("AVAILABLE", "status", GATE_UNKNOWN, GATE_AVAILABLE + 1),
            ("SPEED", "speed_override", 10, 150),
        ):
            value = gcmd.get_int(param, None, minval=minval, maxval=maxval)
            if value is not None:
                fields[field] = value
        if fields.get("status", GATE_UNKNOWN) > GATE_AVAILABLE:
            # "available from buffer" - the MMU3 has no buffer
            fields["status"] = GATE_AVAILABLE
        return fields

    def print_gate_map(self) -> None:
        """Print the gate map to the console."""
        lines = ["Gate map:"]
        for gate, info in enumerate(self.gate_map.gates):
            parts = [f"Gate {gate}:"]
            parts.append(info.material or "-")
            if info.name:
                parts.append(info.name)
            if info.color:
                parts.append(f"color={info.color}")
            if info.temperature >= 0:
                parts.append(f"{info.temperature}C")
            if info.spool_id != NO_SPOOL:
                parts.append(f"spool={info.spool_id}")
            parts.append(f"[{GATE_STATUS_TEXT.get(info.status, info.status)}]")
            if gate == self.loaded_gate:
                parts.append("<- loaded")
            lines.append(" ".join(str(p) for p in parts))
        self.respond_info("\n".join(lines))

    @auto_pause
    @track_operation(OperationKind.UNLOAD)
    @auto_disable_steppers
    def cmd_m702(self, gcmd: GCodeCommand) -> bool:
        """Unload filament if inserted into the IR sensor.

        Args:
            gcmd (GCodeCommand): The G-code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        with (
            FilamentSwitchSensorManager(
                self.filament_switch_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
            FilamentMotionSensorManager(
                self.filament_motion_sensor,
                False,
                self.respond_debug,
                self.reactor,
                self.toolhead,
            ),
        ):
            if not self.unload_gate():
                return False
            if not self.enable_no_selector_mode:
                if not self.is_filament_in_finda():
                    if not self.unselect_gate():
                        return False
                else:
                    self.display_status_msg("M702 Error !!!")
                    return False
            else:
                if not self.unselect_gate():
                    return False
                self.loaded_gate = None
            self.display_status_msg("M702 ok ...")
            return True

    @auto_pause
    @measure_duration
    @auto_disable_steppers
    def cmd_calibrate_pulley_rotation_distance(self, gcmd: GCodeCommand) -> bool:
        """Calibrate pulley rotation_distance.

        Optional parameter LENGTH=<mm> overrides the configured
        pulley_calibrate_filament_length for this run.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        length = gcmd.get_float(
            "LENGTH", self.pulley_calibrate_filament_length, above=0.0
        )
        return self.calibrate_pulley_rotation_distance(length=length)

    @auto_pause
    @measure_duration
    @auto_disable_steppers
    def cmd_calibrate_bowden_load_length(self, gcmd: GCodeCommand) -> bool:
        """Auto-detect bowden_load_length1 by pushing to the filament switch sensor.

        Optional parameter STEP=<mm> (default 10) sets the push increment.
        After calibration, update bowden_load_length1 in your config file.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if calibration succeeded, False otherwise.
        """
        step = gcmd.get_float("STEP", 10.0, above=0.0)
        return self.calibrate_bowden_load_length(step=step)

    @auto_pause
    @auto_disable_steppers
    def cmd_preload_filament_to_finda(self, gcmd: GCodeCommand, gate: int) -> bool:
        """Preload the filament of a gate to FINDA and back.

        Args:
            gcmd (GCodeCommand): The G-Code command.
            gate (int): The gate to preload.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        return self.pre_load_filament_to_finda(gate)

    def cmd_mmu(self, gcmd: GCodeCommand) -> bool:
        """Enable (``ENABLE=1``) or disable (``ENABLE=0``) the MMU3.

        Without ``ENABLE=`` print whether the MMU3 is enabled.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        if enable is None:
            self.respond_info(f"MMU is {'enabled' if self.is_enabled else 'disabled'}.")
            return True
        if enable:
            return self.cmd_mmu_enable(gcmd)
        return self.cmd_mmu_disable(gcmd)

    def cmd_mmu_enable(self, gcmd: GCodeCommand) -> bool:
        """Enable the MMU3, ``MMU ENABLE=1``.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.is_enabled = True
        self.display_status_msg("MMU Enabled")
        return True

    def cmd_mmu_disable(self, gcmd: GCodeCommand) -> bool:
        """Disable the MMU3, ``MMU ENABLE=0``.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        self.is_enabled = False
        # also disable steppers
        self.disable_steppers()
        self.display_status_msg("MMU Disabled")
        return True

    def cmd_mmu_help(self, gcmd: GCodeCommand) -> bool:
        """List the MMU3 commands with a one-line description each.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: Always True.
        """
        table = self.command_table()
        last_tool = self.number_of_tools - 1
        tool_commands = [
            (f"T0 - T{last_tool}", "Change to the tool"),
            (f"K0 - K{last_tool}", "Cut the filament of the tool in the MMU"),
        ]
        rows = [(name, desc) for name, _, desc in table] + tool_commands
        lines = ["MMU commands:"]
        # same layout as Klipper's HELP
        lines += [f"{name:<10}: {desc}" for name, desc in rows]
        self.respond_info("\n".join(lines))
        return True

    def cmd_mmu_status(self, gcmd: GCodeCommand) -> bool:
        """Print a one-shot, human readable summary of the MMU3 state.

        Only reports the tracked state, it does not query the sensors.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: Always True.
        """

        def text(value: None | int, prefix: str = "") -> str:
            return "none" if value is None else f"{prefix}{value}"

        lines = [
            "MMU status:",
            f"Enabled: {'yes' if self.is_enabled else 'no'}",
            f"Homed: {'yes' if self.is_homed else 'no'}",
            f"Paused: {'yes' if self.is_paused else 'no'}",
            f"Selected gate: {text(self.current_gate)}",
            f"Loaded gate: {text(self.loaded_gate)}",
            f"Loaded tool: {text(self.loaded_tool, 'T')}",
            f"Filament position: {self.filament_pos.name}",
            f"Action: {self.action}",
            f"Endless spool: {'enabled' if self.endless_spool_enabled else 'disabled'}",
        ]
        if self.pending_operation is not None:
            lines.append(f"Pending operation: {self.pending_operation.describe()}")
        else:
            lines.append("Pending operation: none")
        self.respond_info("\n".join(lines))
        self.print_gate_map()
        return True

    def cmd_mmu_get_param(self, gcmd: GCodeCommand) -> bool:
        """Get any of the MMU parameters/attributes.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        param: str = gcmd.get("PARAM")
        if hasattr(self, param):
            value = getattr(self, param)
            self.display_status_msg(f"{param}: {value}")
            return True

        self.display_status_msg(f"{param}: doesn't exist!")
        return False

    def cmd_mmu_set_param(self, gcmd: GCodeCommand) -> bool:
        """Set any of the MMU parameters/attributes.

        Args:
            gcmd (GCodeCommand): The G-Code command.

        Returns:
            bool: True if command completed successfully, False otherwise.
        """
        param: str = gcmd.get("PARAM")
        value: str = gcmd.get("VALUE")

        if param.startswith("_"):
            # protect private parameters
            return True

        # a misspelled name would silently add a new attribute, and a method
        # would be replaced by the value
        if not hasattr(self, param) or callable(getattr(self, param)):
            self.display_status_msg(f"{param}: doesn't exist!")
            return False

        if "," in value:
            temp_value = []
            for v in value.split(","):
                if IS_DIGIT.match(v):
                    v = float(v)
                temp_value.append(v)
            value = temp_value
        elif IS_DIGIT.match(value):
            value = float(value)
        elif value.lower() in ["true", "false"]:
            value = value.lower() == "true"
        setattr(self, param, value)
        self.display_status_msg(f"{param}: {value}")
        return True


def load_config(config: ConfigWrapper) -> MMU:
    """Load the [mmu] config section.

    Args:
        config (ConfigWrapper): The config wrapper.

    Returns:
        MMU: The MMU instance.
    """
    return MMU(config)
