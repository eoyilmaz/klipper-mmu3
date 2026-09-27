#!/bin/bash
maintainer=eoyilmaz
repo=klipper-mmu3

script_path=$(realpath $(echo $0))
repo_path=$(dirname $script_path)

# --------------------------------------------------------------
# Check if running as root
if [ "$(id -u)" = "0" ]; then
    echo "Script must run from non-root !!!"
    exit 1
fi

# --------------------------------------------------------------
# Linking the Klipper modules
extras_path=~/klipper/klippy/extras/

# Happy Hare is also configured with [mmu]: its extras/mmu/ package would be
# imported instead of mmu.py, and its extras/mmu.py would be replaced
mmu_py="$extras_path/mmu.py"
if [ -d "$extras_path/mmu" ]; then
    happy_hare=1
elif [ -e "$mmu_py" ] || [ -L "$mmu_py" ]; then
    # anything but a link to this repository's mmu.py
    [ "$(realpath "$mmu_py" 2>/dev/null)" != "$(realpath "$repo_path/extras/mmu.py")" ] && happy_hare=1
fi
if [ -n "$happy_hare" ]; then
    echo "Happy Hare seems to be installed ($extras_path has mmu or mmu.py), it uses the same [mmu] section."
    echo "Uninstall Happy Hare before installing klipper-mmu3."
    exit 1
fi

# mmu.py is the [mmu] section. mmu3.py only stops Klipper with a message that
# explains the rename when a config still has the old [mmu3 MMU3] section.
for module_name in mmu.py mmu3.py mmu_gate_map.py mmu_hh_compat.py mmu_mainsail_prompts.py; do
    # always force symlink
    ln -sf "$repo_path/extras/$module_name" $extras_path
    echo "Linking $module_name to $extras_path successfully complete!"
done

# the helper modules before the [mmu3 MMU3] -> [mmu] rename, their links point
# to files that are gone now
for module_name in mmu3_gate_map.py mmu3_hh_compat.py mmu3_mainsail_prompts.py; do
    if [ -L "$extras_path/$module_name" ] && [ ! -e "$extras_path/$module_name" ]; then
        rm "$extras_path/$module_name"
        echo "Removed the old $module_name link from $extras_path"
    fi
done

# --------------------------------------------------------------
# Update printer.cfg
cfg_path=~/printer_data/config/
cfg_incl_path=~/printer_data/config/printer.cfg

while [ -z "$cfg_name" ]; do
    read -p " Which MMU do you want to install? (1=MMU3 5x / 2=MMU3-12x / 3=MMU3-12x-NG): " answer
    case "$answer" in
        1) cfg_name=mmu.cfg ;;
        2) cfg_name=mmu-12x.cfg ;;
        3) cfg_name=mmu-12x-ng.cfg ;;
        *) echo "Please enter 1, 2 or 3." ;;
    esac
done
cp -f "$repo_path/$cfg_name" $cfg_path # Overwrite

# Removing the [include] of the config file names before the [mmu3 MMU3] ->
# [mmu] rename, the new config file is included below
old_include='^\[include mmu3(-12x|-12x-ng)?\.cfg\]$'
if [ -f "$cfg_incl_path" ] && grep -Eq "$old_include" "$cfg_incl_path"; then
    sudo service klipper stop
    sed -Ei "/$old_include/d" "$cfg_incl_path"
    sudo service klipper start
    echo "Removed the old [include mmu3*.cfg] from $cfg_incl_path, copy your settings from the old mmu3 config file to $cfg_name"
fi

# Adding the [include mmu.cfg] line to printer.cfg
if [ -f "$cfg_incl_path" ]; then
    if ! grep -q "^\[include $cfg_name\]$" "$cfg_incl_path"; then
        sudo service klipper stop
        sed -i "1i\[include $cfg_name]" "$cfg_incl_path"
        # echo "Including $cfg_name to $cfg_incl_path successfully complete"
        sudo service klipper start
    else
        echo "Including $cfg_name aborted, $cfg_name already exists in $cfg_incl_path"
    fi
fi

cfg_name=beep.cfg
ln -sf "$repo_path/$cfg_name" $cfg_path # Overwrite

cfg_name=mmu_menus.cfg
ln -sf "$repo_path/$cfg_name" $cfg_path # Overwrite

# Adding the [respond] line to printer.cfg
if [ -f "$cfg_incl_path" ]; then
    if ! grep -q "^\[respond\]$" "$cfg_incl_path"; then
        sudo service klipper stop
        sed -i "1i\[respond]" "$cfg_incl_path"
        # echo "Including [respond] to $cfg_incl_path successfully complete"
        sudo service klipper start
    else
        echo "Including [respond] aborted, [respond] already exists in $cfg_incl_path"
    fi
fi

# --------------------------------------------------------------
# Adding update block to moonraker.conf
blk_path=~/printer_data/config/moonraker.conf
if [ -f "$blk_path" ]; then
    if ! grep -q "^\[update_manager $repo\]$" "$blk_path"; then
        read -p " Do you want to install the updater? (y/n): " answer
        if [ "$answer" != "${answer#[Yy]}" ]; then
          sudo service moonraker stop
          sed -i "\$a \ " "$blk_path"
          sed -i "\$a [update_manager $repo]" "$blk_path"
          sed -i "\$a type: git_repo" "$blk_path"
          sed -i "\$a path: $repo_path" "$blk_path"
          sed -i "\$a origin: https://github.com/$maintainer/$repo.git" "$blk_path"
          sed -i "\$a primary_branch: main" "$blk_path"
          sed -i "\$a managed_services: klipper" "$blk_path"
          echo "Including [update_manager] to $blk_path successfully complete"
          sudo service moonraker start
        else
          echo "Installing updater aborted"
        fi
    else
        echo "Including [update_manager] aborted, [update_manager] already exists in $blk_path"
    fi
fi

# --------------------------------------------------------------
# Install Python dependencies to Klipper
# source ~/klippy-env/bin/activate
# pip install uv
# uv pip install -r $repo_path/requirements.txt
# deactivate
