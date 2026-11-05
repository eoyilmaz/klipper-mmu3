; Change filament G-Code Start
{if flush_length > 1}
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
T[next_extruder]
{if flush_length > 1}
    G92 E0 ; keep this for the VG1
    M83 ; keep this for the VG1
    ; Klipper ignores these comments; Orca reads the ;VG1 lines as the flush, so
    ; the flushed filament and the time to extrude it are still estimated
; VFLUSH_START
    {if flush_length > 23.7}
;VG1 E23.7 F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
;VG1 E{flush_length - 23.7} F{flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
    {else}
;VG1 E{flush_length} F{flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60}
    {endif}
; VFLUSH_END
    ; FLUSH in printer.cfg splits the flush into blobs; it uses only the total,
    ; as Orca leaves flush_length_1..4 at 0 for a flush below ~67 mm3
    FLUSH LENGTH={flush_length} FIRST_SPEED={flush_volumetric_speeds[previous_extruder]/2.405*0.7215*60} SPEED={flush_volumetric_speeds[next_extruder]/2.405*0.7215*60}
{endif}
PURGE_PLATFORM_EXTEND
M106 P1 S255
M106 P2 S255
M104 S[new_filament_temp]
M400
G92 E0
; Change filament G-Code End
