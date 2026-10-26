; Change filament G-Code Start
{if flush_length_1 + flush_length_2 + flush_length_3 + flush_length_4 > 1}
; directly go to the nozzle wiper starting position
; as when we raise the nozzle here, it accumulates
; too much filament on top of the wipe tower
G1 X175 Y248 F15000
G1 X214
G1 Z{max_layer_z + 3.0} F1200
M400
M106 P1 S0
M106 P2 S0
PURGE_PLATFORM_EXTEND ; be sure the platform is extended
{endif}

{if old_filament_temp > 142 && next_extruder < 255}
    M104 S[old_filament_temp]
{endif}
; same as MMU_CHANGE_TOOL TOOL=[next_extruder], but the slicer only
; recognizes a T command as the tool change, with anything else it adds its
; own T command after this G-code
T[next_extruder]
G92 E0
; {if flush_length_1 + flush_length_2 + flush_length_3 + flush_length_4 <= 1}
;     WIPE_NOZZLE; no flushing has happened, just wipe the nozzle
; {endif}
{if flush_length_1 > 1}
    ; stop fans so that the first bit of filament lands in to the platform
    M106 P1 S0
    M106 P2 S0
    M83
    G0 X214 Y248 F15000 ; Be sure that we are at flush position
    PURGE_PLATFORM_EXTEND
    M400
; FLUSH_START
    {if flush_length_1 > 23.7}
        G1 E23.7 F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
        G1 E{(flush_length_1 - 23.7) * 0.20} F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
        ; now start the fans so that the blob gets cooled down
        M106 P1 S64
        M106 P2 S64
        G1 E{(flush_length_1 - 23.7) * 0.80} F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
    {else}
        G1 E{flush_length_1} F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
    {endif}
; FLUSH_END
    M106 P1 S255
    M106 P2 S255
    PURGE_PLATFORM_RETRACT
    G92 E0
    G1 E-1.5 F1800; retract before wiping
    M400
    M106 P1 S0
    M106 P2 S0
    WIPE_NOZZLE
    G1 E1.5 F1800; un-retract
    PURGE_PLATFORM_EXTEND
    M400
{endif}
{if flush_length_2 > 1}
    ; stop fans so that the first bit of filament lands in to the platform
    M106 P1 S0
    M106 P2 S0
    M83
    G0 X214 Y248 F15000 ; Be sure that we are at flush position
    PURGE_PLATFORM_EXTEND
    M400
; FLUSH_START
    G1 E{flush_length_2 * 0.20} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
    ; now start the fans so that the blob gets cooled down
    M106 P1 S64
    M106 P2 S64
    G1 E{flush_length_2 * 0.80} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
; FLUSH_END
    M106 P1 S255
    M106 P2 S255
    PURGE_PLATFORM_RETRACT
    G92 E0
    G1 E-1.5 F1800; retract before wiping
    M400
    M106 P1 S0
    M106 P2 S0
    WIPE_NOZZLE
    G1 E1.5 F1800; un-retract
    PURGE_PLATFORM_EXTEND
    M400
{endif}
{if flush_length_3 > 1}
    ; stop fans so that the first bit of filament lands in to the platform
    M106 P1 S0
    M106 P2 S0
    M83
    G0 X214 Y248 F15000 ; Be sure that we are at flush position
    PURGE_PLATFORM_EXTEND
    M400
; FLUSH_START
    G1 E{flush_length_3 * 0.20} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
    ; now start the fans so that the blob gets cooled down
    M106 P1 S64
    M106 P2 S64
    G1 E{flush_length_3 * 0.80} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
; FLUSH_END
    M106 P1 S255
    M106 P2 S255
    PURGE_PLATFORM_RETRACT
    G92 E0
    G1 E-1.5 F1800; retract before wiping
    M400
    M106 P1 S0
    M106 P2 S0
    WIPE_NOZZLE
    G1 E1.5 F1800; un-retract
    PURGE_PLATFORM_EXTEND
    M400
{endif}
{if flush_length_4 > 1}
    ; stop fans so that the first bit of filament lands in to the platform
    M106 P1 S0
    M106 P2 S0
    M83
    G0 X214 Y248 F15000 ; Be sure that we are at flush position
    PURGE_PLATFORM_EXTEND
    M400
; FLUSH_START
    G1 E{flush_length_4 * 0.20} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
    ; now start the fans so that the blob gets cooled down
    M106 P1 S64
    M106 P2 S64
    G1 E{flush_length_4 * 0.80} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
; FLUSH_END
    M106 P1 S255
    M106 P2 S255
    PURGE_PLATFORM_RETRACT
    G92 E0
    G1 E-1.5 F1800; retract before wiping
    M400
    M106 P1 S0
    M106 P2 S0
    WIPE_NOZZLE
    G1 E1.5 F1800; un-retract
    PURGE_PLATFORM_EXTEND
    M400
{endif}
; FLUSH_START
PURGE_PLATFORM_EXTEND
M106 P1 S255
M106 P2 S255
;M400
;M109 S[new_filament_temp]
M104 S[new_filament_temp]
;G1 E2 F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60} ;Compensate for filament spillage during waiting temperature
; FLUSH_END
M400
G92 E0
; G1 E1.5 F1800 ; un-retract
;M400
; G1 Z{max_layer_z + 3.0} F3000
;{if layer_z <= (initial_layer_print_height + 0.001)}
;    M204 S[initial_layer_acceleration]
;{else}
;    M204 S[default_acceleration]
;{endif}
; Change filament G-Code End