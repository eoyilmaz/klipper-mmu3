# Happy Hare Compatibility Roadmap

The MMU3 extension reports its state in [Happy Hare](https://github.com/moggieuk/Happy-Hare)'s
format (`printer.mmu` / `printer.mmu_machine`), so Mainsail's and Fluidd's MMU panels work without
changes (see [#45](https://github.com/eoyilmaz/klipper-mmu3/issues/45)). This document lists what
is still missing to make the MMU3 behave more like a Happy Hare MMU, as a punch list grouped into
phases.

Each item says what to do, why it matters, and where it touches the code. Phases are ordered by
value versus effort; items inside a phase are independent unless noted.

## Terminology

Happy Hare separates two ideas that the MMU3 extension currently treats as one:

| Happy Hare | Meaning | MMU3 today |
|---|---|---|
| **tool** | What the slicer / G-code asks for (`T0`…`Tn`, `MMU_CHANGE_TOOL TOOL=`) | `tool` |
| **gate** | A physical lane of the MMU, where a spool is fed in | `tool` (`current_tool` is the selector position) |
| **TTG map** | Tool-to-gate mapping, `Tn → gate m` | Fixed identity (`Tn → gate n`) |

As long as the TTG map is the identity, one name is enough. Phase 3 introduces real remapping and
is where the tool / gate split becomes necessary.

## Status Legend

- `[x]` done
- `[ ]` to do
- `[-]` decided not to do (reason given)

## Phase 0 — Done (#45)

- [x] `mmu` and `mmu_machine` status objects in Happy Hare's shape (`extras/mmu3_hh_compat.py`).
- [x] Live `action` reporting (Selecting, Loading, Loading Ext, Unloading, Forming Tip, Cutting
  Tip, Heating, Homing, ...).
- [x] Per-gate filament map persisted with `save_variables` (`extras/mmu3_gate_map.py`,
  `MMU_GATE_MAP`).
- [x] Spoolman `readonly` mode: Moonraker's active spool follows the loaded gate.
- [x] Happy Hare command names: `MMU_LOAD`, `MMU_UNLOAD`, `MMU_EJECT`, `MMU_HOME`, `MMU_UNLOCK`,
  `MMU_SELECT`, `MMU_UNSELECT`, `MMU_MOTORS_OFF`, `MMU_CHANGE_TOOL`, `MMU_PRELOAD`, `MMU_RECOVER`.
- [x] Deprecated aliases for the old names (`LT`, `UT`, `HOME_MMU`, `UNLOCK_MMU`, `SELECT_TOOL`,
  `UNSELECT_TOOL`).
- [x] "Not supported on MMU3" replies for panel commands without an MMU3 equivalent.
- [x] Stub `_MMU_SOFTWARE_VARS` macro required by Mainsail's panel.

## Phase 1 — Polish the Existing Emulation

Small, low-risk items that make the panel and existing commands more complete.

- [ ] **Implement `MMU_CHECK_GATE`.** ([#48](https://github.com/eoyilmaz/klipper-mmu3/issues/48))
  Currently replies "not supported", but the MMU3 can do it: feed each gate to FINDA and back like
  `PRE_LOAD_FILAMENT_TO_FINDA`, and record the result with `set_gate_status()`. Support `GATE=`,
  `GATES=0,2,3`, `TOOL=` and `ALL=1`. Must refuse while filament is loaded (same check as
  `MMU_PRELOAD`). The panel's "Check gates" button then works.
- [ ] **Accept `TOOL=` on `MMU_SELECT`, `MMU_LOAD` and `MMU_PRELOAD`.**
  ([#49](https://github.com/eoyilmaz/klipper-mmu3/issues/49)) Happy Hare accepts `TOOL=` or `GATE=`
  on most commands. Extend `get_gate_param()`.
- [ ] **Unify `MMU_ENABLE` / `MMU_DISABLE` with Happy Hare's `MMU ENABLE=0|1`.**
  ([#50](https://github.com/eoyilmaz/klipper-mmu3/issues/50)) Register the `MMU` command, keep the
  old names as deprecated aliases. Also add `MMU_HELP` and `MMU_STATUS` (a one-shot human readable
  summary).
- [ ] **Report `filament_position` (mm) and `bowden_progress` (%).**
  ([#51](https://github.com/eoyilmaz/klipper-mmu3/issues/51)) Both are hard coded today. Track the
  gear stepper position during bowden moves so the panel's filament bar animates. Must be read from
  already known values in `get_status()`, never from the MCU.
- [ ] **Cache the last FINDA reading for `sensors.mmu_gate`.**
  ([#52](https://github.com/eoyilmaz/klipper-mmu3/issues/52)) It is derived from `filament_pos`
  today because `get_status()` must not query MCU endstops. Store the result of each
  `is_filament_in_finda()` call and report that instead.
- [ ] **Clear gate status on runout / failed load.**
  ([#53](https://github.com/eoyilmaz/klipper-mmu3/issues/53)) Mark the gate `EMPTY` when the
  filament switch sensor reports runout during a print, not only when loading to FINDA fails.
- [ ] **Show the filament motion sensor as a clog / runout sensor.**
  ([#54](https://github.com/eoyilmaz/klipper-mmu3/issues/54)) The MMU3 already supports a
  `filament_motion_sensor`; expose it as `clog_detection_enabled` and in `sensors`, so the panel
  shows it.
- [ ] **Fill `_MMU_SOFTWARE_VARS` with real values**
  ([#55](https://github.com/eoyilmaz/klipper-mmu3/issues/55)) where Mainsail reads them, instead of
  the stub.
- [ ] **Verify Fluidd.** ([#56](https://github.com/eoyilmaz/klipper-mmu3/issues/56)) Only Mainsail
  was tested on hardware. Check that Fluidd's MMU card renders and its buttons send commands we
  handle.
- [ ] **Remove the deprecated aliases** ([#57](https://github.com/eoyilmaz/klipper-mmu3/issues/57))
  (`LT`, `UT`, `HOME_MMU`, `UNLOCK_MMU`, `SELECT_TOOL`, `UNSELECT_TOOL`) in a later release, after
  announcing it in the release notes.

## Phase 2 — Print Lifecycle and Macros

Makes the MMU3 fit into Happy Hare-style print start / end G-code and user macros.

- [ ] **`MMU_PRINT_START` / `MMU_PRINT_END`.**
  ([#58](https://github.com/eoyilmaz/klipper-mmu3/issues/58)) Reset job stats, check that all gates
  used by the print are available, and switch `print_state` explicitly instead of only following
  `print_stats`. Report `print_start_detection`.
- [ ] **User callback macros.** ([#59](https://github.com/eoyilmaz/klipper-mmu3/issues/59)) Call
  optional user macros at fixed points, like Happy Hare's `_MMU_PRE_UNLOAD`, `_MMU_POST_UNLOAD`,
  `_MMU_PRE_LOAD`, `_MMU_POST_LOAD` and `_MMU_ACTION_CHANGED`. Only call a macro if it exists, so
  existing setups are unaffected. Check the exact names and parameters against Happy Hare's
  `mmu_sequence.cfg` before implementing.
- [ ] **`MMU_FORM_TIP` and `MMU_CUT`.** ([#60](https://github.com/eoyilmaz/klipper-mmu3/issues/60))
  Standalone commands for tip forming (ramming) and the in-extruder cut, so they can be tested and
  tuned outside a tool change. The code paths already exist (`ramming_slicer`,
  `CUT_FILAMENT_IN_EXTRUDER`).
- [ ] **Sample slicer G-code in Happy Hare style.**
  ([#61](https://github.com/eoyilmaz/klipper-mmu3/issues/61)) Update `sample_configs/` so start /
  end G-code uses `MMU_PRINT_START`, `MMU_PRINT_END` and `MMU_CHANGE_TOOL`.

## Phase 3 — Tool / Gate Split, TTG Map and Endless Spool

The largest change. Needed before any feature where tool `n` is not gate `n`.

- [ ] **Separate tool and gate internally.**
  ([#62](https://github.com/eoyilmaz/klipper-mmu3/issues/62)) Rename the physical side to `gate`:
  selector position (`current_tool` → `current_gate`), gate map, FINDA checks, `tool_mapping`. Keep
  `tool` for what the slicer asks for. Do it as a pure rename first (no behaviour change), with the
  tests green, before any other Phase 3 item.
- [ ] **TTG map.** ([#63](https://github.com/eoyilmaz/klipper-mmu3/issues/63)) Persist a `ttg_map`
  with `save_variables`, report it in `get_status()`, and resolve `Tn` / `MMU_CHANGE_TOOL TOOL=n`
  through it. Implement `MMU_TTG_MAP` (show / set / reset) and `MMU_REMAP_TTG`. Mainsail's tool
  mapping dialog then works.
- [ ] **Endless spool.** ([#64](https://github.com/eoyilmaz/klipper-mmu3/issues/64)) On runout,
  switch the tool to the next available gate in the same `endless_spool_groups` group and continue
  printing. Needs reliable runout detection (filament switch sensor, or the motion sensor from Phase
  1) and the TTG map. Implement `MMU_ENDLESS_SPOOL`.
- [ ] **Slicer tool map (`MMU_SLICER_TOOL_MAP`).**
  ([#65](https://github.com/eoyilmaz/klipper-mmu3/issues/65)) Record the tools, colours and
  materials the print file uses, so the panel can show them and `MMU_PRINT_START` can warn about
  mismatches with the gate map. Happy Hare fills this from a Moonraker component that scans the
  G-code file; decide whether to reuse that component or pass the data from the slicer's start
  G-code.

## Phase 4 — Spoolman Push / Pull

- [ ] **Spoolman `push` / `pull` modes.**
  ([#66](https://github.com/eoyilmaz/klipper-mmu3/issues/66)) Store the gate assignment in Spoolman
  itself, so several printers share one spool database and a spool keeps its gate across printers.
  Happy Hare does this through its Moonraker component (`MMU_SPOOLMAN`). Needs a Moonraker-side
  component or the Spoolman API called from Moonraker; the Klipper side cannot talk to Spoolman
  directly.
- [ ] **`pending_spool_id`.** ([#67](https://github.com/eoyilmaz/klipper-mmu3/issues/67)) Support
  "scan a spool, then pick a gate" flows (e.g. QR code scanners that set a pending spool).

## Phase 5 — Upstream (Mainsail / Fluidd)

- [ ] **Hide the standalone bypass spool.**
  ([#68](https://github.com/eoyilmaz/klipper-mmu3/issues/68)) Mainsail draws a bypass spool unless a
  unit reports `has_bypass: true` (`showStandaloneBypass` in `MmuPanel.vue`). Propose a PR to also
  check `printer.mmu.has_bypass`, so MMUs without a bypass don't show one.
- [ ] **MMU3 logo.** ([#69](https://github.com/eoyilmaz/klipper-mmu3/issues/69))
  `mmu_machine.unit_0.vendor` is `"Prusa"`, which Mainsail has no logo for. Propose adding one
  upstream.

## Not Planned

- [-] **Bypass.** The MMU3 has no bypass path.
- [-] **Encoder-based features** (encoder clog detection, `MMU_CALIBRATE_ENCODER`). The MMU3 has no
  encoder; the filament motion sensor covers clog detection.
- [-] **Multiple MMU units.** One MMU3 per printer; `num_units` stays 1.
- [-] **Synced gear motor (`sync_drive`, `MMU_SYNC_GEAR_MOTOR`).** Possible in theory, but the MMU3
  releases the filament with the idler during printing and gains nothing from it.
