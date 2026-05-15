SET_PIN PIN=main_led VALUE=1.00
; SET_FILAMENT_SENSOR SENSOR=my_filament_sensor ENABLE=0
; SET_FILAMENT_SENSOR SENSOR=encoder_sensor ENABLE=0
; clear any remaning skew, eddy tap offset and bed mesh values
SET_SKEW CLEAR=1
PROBE_EDDY_NG_SET_TAP_OFFSET VALUE=0
BED_MESH_CLEAR
G90 ; use absolute coordinates
M83 ; extruder relative mode
M140 S{first_layer_bed_temperature[0]} ; set final bed temp
M104 S130; S{first_layer_temperature[0]} ; set final nozzle temp
;G4 P10000 ; allow partial nozzle warmup
M190 S{first_layer_bed_temperature[0]} ; wait for bed temp to stabilize
G28 ; home all axis
Z_TILT_ADJUST; home and auto align z-axis
WIPE_NOZZLE
G0 X117.5 Y117.5; move to the tap position
PROBE_EDDY_NG_TAP TARGET_Z=-1;
G28 Z; home Z-axis only again
; probe entire bed
;BED_MESH_CALIBRATE METHOD=rapid_scan;
; probe adaptively
BED_MESH_CALIBRATE METHOD=rapid_scan mesh_min={adaptive_bed_mesh_min[0]},{adaptive_bed_mesh_min[1]} mesh_max={adaptive_bed_mesh_max[0]},{adaptive_bed_mesh_max[1]} ALGORITHM=[bed_mesh_algo] PROBE_COUNT={bed_mesh_probe_count[0]},{bed_mesh_probe_count[1]} ADAPTIVE=0
M109 S{first_layer_temperature[0]} ; wait for nozzle temp to stabilize
SKEW_PROFILE LOAD=Califlower

; select extruder
; sometimes loading the material causes
; a lot of purge do this araound the start of the bed
G1 Z20 F240
G1 X212 Y248 F{travel_speed*0.5*60}
;HOME_MMU
T[initial_tool]
WIPE_NOZZLE

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
G1 E2 F3000 ; un-retract filament
WIPE_NOZZLE

; MZ FLOW TEMP START