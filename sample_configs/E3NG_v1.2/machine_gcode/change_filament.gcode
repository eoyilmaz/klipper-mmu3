; Change filament G-Code Start
G1 Z{max_layer_z + 3.0} F1200
M400
M106 P1 S255
M106 P2 S255
{if old_filament_temp > 142 && next_extruder < 255}
    M104 S[old_filament_temp]
{endif}
T[next_extruder]
; G0 X212 Y248 F12000 ; After Cut nozzle will be at this position.
G92 E0
{if flush_length_1 > 1}
    M83
; FLUSH_START
    M400
    {if flush_length_1 > 23.7}
        G1 E23.7 F{flush_volumetric_speeds[previous_extruder]*0.3*60} ; do not need pulsatile flushing for start part
        G1 E{(flush_length_1 - 23.7)} F{flush_volumetric_speeds[previous_extruder]*0.3*60}
    {else}
        G1 E{flush_length_1} F{flush_volumetric_speeds[previous_extruder]*0.3*60}
    {endif}
; FLUSH_END
    WIPE_NOZZLE
{endif}
{if flush_length_2 > 1}
    M83
; FLUSH_START
    G1 E{flush_length_2} F{flush_volumetric_speeds[next_extruder]*0.3*60}
; FLUSH_END
    WIPE_NOZZLE
{endif}
{if flush_length_3 > 1}
    M83
; FLUSH_START
    G1 E{flush_length_3} F{flush_volumetric_speeds[next_extruder]*0.3*60}
; FLUSH_END
    WIPE_NOZZLE
{endif}
{if flush_length_4 > 1}
    M83
; FLUSH_START
    G1 E{flush_length_4} F{flush_volumetric_speeds[next_extruder]*0.3*60}
; FLUSH_END
    WIPE_NOZZLE
{endif}
; FLUSH_START
M400
;M109 S[new_filament_temp]
M104 S[new_filament_temp]
;G1 E2 F{flush_volumetric_speeds[next_extruder]*0.3*60} ;Compensate for filament spillage during waiting temperature
; FLUSH_END
M400
G92 E0
G1 E-[new_retract_length_toolchange] F1800
M400
;WIPE_NOZZLE
M400
G1 Z{max_layer_z + 3.0} F3000
{if layer_z <= (initial_layer_print_height + 0.001)}
    M204 S[initial_layer_acceleration]
{else}
    M204 S[default_acceleration]
{endif}
; Change filament G-Code End