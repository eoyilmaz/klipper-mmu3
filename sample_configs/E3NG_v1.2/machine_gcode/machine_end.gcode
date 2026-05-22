; MZ FLOW TEMP END

; Unload filament
UT

{if max_layer_z < max_print_height}
G1 Z{z_offset+min(max_layer_z+2, max_print_height)} F600 ; Move print head up
{endif}
G1 X{print_bed_min[0] + 20} Y{print_bed_max[1] - 20} F{travel_speed * 0.5 * 60} ; present print
{if max_layer_z < max_print_height-10}G1 Z{z_offset+min(max_layer_z+5, max_print_height-10)} F600 ; Move print head further up{endif}

SET_SKEW CLEAR=1
PROBE_EDDY_NG_SET_TAP_OFFSET VALUE=0
BED_MESH_CLEAR

M140 S0 ; turn off heatbed
M104 S0 ; turn off temperature
M107 ; turn off fan
M84 ; disable motors
; Disable MMU Steppers
MANUAL_STEPPER STEPPER=pulley_stepper ENABLE=0
MANUAL_STEPPER STEPPER=idler_stepper ENABLE=0
MANUAL_STEPPER STEPPER=selector_stepper ENABLE=0

; SET_PIN PIN=main_led VALUE=0 ; turn off main LED
