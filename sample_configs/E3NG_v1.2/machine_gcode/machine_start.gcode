CLEAR_PAUSE ; To prevent "Already Paused" errors...
; the tools the print uses, one line per MMU gate (the COLOR and NAME quotes
; are needed), MMU_PRINT_START warns if a tool loads an empty gate or a gate
; with a different material
MMU_SLICER_TOOL_MAP RESET=1 INITIAL_TOOL=[initial_tool] TOTAL_TOOLCHANGES=[total_toolchanges]
{if is_extruder_used[0]}MMU_SLICER_TOOL_MAP TOOL=0 COLOR="{filament_colour[0]}" MATERIAL="{filament_type[0]}" TEMP={nozzle_temperature_initial_layer[0]} NAME="{filament_settings_id[0]}"{endif}
{if is_extruder_used[1]}MMU_SLICER_TOOL_MAP TOOL=1 COLOR="{filament_colour[1]}" MATERIAL="{filament_type[1]}" TEMP={nozzle_temperature_initial_layer[1]} NAME="{filament_settings_id[1]}"{endif}
{if is_extruder_used[2]}MMU_SLICER_TOOL_MAP TOOL=2 COLOR="{filament_colour[2]}" MATERIAL="{filament_type[2]}" TEMP={nozzle_temperature_initial_layer[2]} NAME="{filament_settings_id[2]}"{endif}
{if is_extruder_used[3]}MMU_SLICER_TOOL_MAP TOOL=3 COLOR="{filament_colour[3]}" MATERIAL="{filament_type[3]}" TEMP={nozzle_temperature_initial_layer[3]} NAME="{filament_settings_id[3]}"{endif}
{if is_extruder_used[4]}MMU_SLICER_TOOL_MAP TOOL=4 COLOR="{filament_colour[4]}" MATERIAL="{filament_type[4]}" TEMP={nozzle_temperature_initial_layer[4]} NAME="{filament_settings_id[4]}"{endif}
{if is_extruder_used[5]}MMU_SLICER_TOOL_MAP TOOL=5 COLOR="{filament_colour[5]}" MATERIAL="{filament_type[5]}" TEMP={nozzle_temperature_initial_layer[5]} NAME="{filament_settings_id[5]}"{endif}
{if is_extruder_used[6]}MMU_SLICER_TOOL_MAP TOOL=6 COLOR="{filament_colour[6]}" MATERIAL="{filament_type[6]}" TEMP={nozzle_temperature_initial_layer[6]} NAME="{filament_settings_id[6]}"{endif}
{if is_extruder_used[7]}MMU_SLICER_TOOL_MAP TOOL=7 COLOR="{filament_colour[7]}" MATERIAL="{filament_type[7]}" TEMP={nozzle_temperature_initial_layer[7]} NAME="{filament_settings_id[7]}"{endif}
{if is_extruder_used[8]}MMU_SLICER_TOOL_MAP TOOL=8 COLOR="{filament_colour[8]}" MATERIAL="{filament_type[8]}" TEMP={nozzle_temperature_initial_layer[8]} NAME="{filament_settings_id[8]}"{endif}
{if is_extruder_used[9]}MMU_SLICER_TOOL_MAP TOOL=9 COLOR="{filament_colour[9]}" MATERIAL="{filament_type[9]}" TEMP={nozzle_temperature_initial_layer[9]} NAME="{filament_settings_id[9]}"{endif}
{if is_extruder_used[10]}MMU_SLICER_TOOL_MAP TOOL=10 COLOR="{filament_colour[10]}" MATERIAL="{filament_type[10]}" TEMP={nozzle_temperature_initial_layer[10]} NAME="{filament_settings_id[10]}"{endif}
{if is_extruder_used[11]}MMU_SLICER_TOOL_MAP TOOL=11 COLOR="{filament_colour[11]}" MATERIAL="{filament_type[11]}" TEMP={nozzle_temperature_initial_layer[11]} NAME="{filament_settings_id[11]}"{endif}
MMU_PRINT_START ; start the MMU print job, resets the job statistics, checks the tools
SET_PIN PIN=main_led VALUE=1.00
; SET_FILAMENT_SENSOR SENSOR=my_filament_sensor ENABLE=0
; SET_FILAMENT_SENSOR SENSOR=encoder_sensor ENABLE=0

; clear any remaining skew, eddy tap offset and bed mesh values
SET_SKEW CLEAR=1
BED_MESH_CLEAR

G90 ; use absolute coordinates
M83 ; extruder relative mode
M140 S{first_layer_bed_temperature[0]} ; set final bed temp
M104 S130; S{first_layer_temperature[0]} ; set final nozzle temp
M190 S{first_layer_bed_temperature[0]} ; wait for bed temp to stabilize

; 1. Initial Home (Native Eddy handles drift automatically based on its temperature)
G28 ; home all axis

; 2. Tram the bed
Z_TILT_ADJUST ; home and auto align z-axis
G28 Z

; 3. Get a drift-free Z reference via a tap probe (uses the already-calibrated
;    tap_threshold - does NOT re-run guess/refine/verify)
WIPE_NOZZLE
G0 Z10 F600                          ; lift clear of the bed before travelling
G0 X117.5 Y117.5 F{travel_speed*60}  ; move to the tap position (bed center)
TAP_Z_CALIBRATE                      ; probes via tap (3 samples, averaged) and corrects Klipper's Z frame (macro in printer.cfg)

; probe adaptively
BED_MESH_CALIBRATE METHOD=rapid_scan mesh_min={adaptive_bed_mesh_min[0]},{adaptive_bed_mesh_min[1]} mesh_max={adaptive_bed_mesh_max[0]},{adaptive_bed_mesh_max[1]} ALGORITHM=[bed_mesh_algo] PROBE_COUNT={bed_mesh_probe_count[0]},{bed_mesh_probe_count[1]} ADAPTIVE=0

SKEW_PROFILE LOAD=Califlower

; select extruder
; sometimes loading the material causes
; a lot of purge do this around the start of the bed
G1 Z20 F240
G1 X218 Y248 F{travel_speed*0.5*60}

M109 S{first_layer_temperature[0]} ; wait for nozzle temp to stabilize

PURGE_PLATFORM_EXTEND
M106 P1 S255
M106 P2 S255
T[initial_tool] ; load the first tool, same as MMU_CHANGE_TOOL TOOL=[initial_tool]
G92 E0
G0 E10 F3000
PURGE_PLATFORM_RETRACT
WIPE_NOZZLE
PURGE_PLATFORM_EXTEND
M106 P1 S0
M106 P2 S0

; prime the nozzle
G1 Z20 F240
G1 X20 Y2 F3000
G1 Z{initial_layer_print_height} F240
G92 E0
G1 X200 E6.36 F3000
G1 Y2.4 F5000
G92 E0
G1 X20 E6.36 F3000 ; prime the nozzle
G92 E0
G1 Y2.8 F5000
G1 X200 E6.36 F3000 ; prime the nozzle
G92 E0
G1 Y3.2 F5000
G1 X20 E6.36 F3000 ; prime the nozzle
G92 E0
G1 E-2 F3000 ; retract filament
G92 E0

; Go to the filament change point
G1 X212 Y248 F{travel_speed*0.5*60}
;G1 E2 F3000 ; un-retract filament

; MZ FLOW TEMP START