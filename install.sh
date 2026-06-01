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
# Linking mmu3.py
mmu3_path=~/klipper/klippy/extras/
mmu3_name=mmu3.py
# always force symlink
ln -sf "$repo_path/extras/$mmu3_name" $mmu3_path
echo "Linking $mmu3_name to $mmu3_path successfully complete!"

# --------------------------------------------------------------
# Linking mmu3_mainsail_prompts.py
mmu3_mainsail_prompts_path=~/klipper/klippy/extras/
mmu3_mainsail_prompts_name=mmu3_mainsail_prompts.py
# always force symlink
ln -sf "$repo_path/extras/$mmu3_mainsail_prompts_name" $mmu3_mainsail_prompts_path
echo "Linking $mmu3_mainsail_prompts_name to $mmu3_mainsail_prompts_path successfully complete!"

# --------------------------------------------------------------
# Update printer.cfg
cfg_path=~/printer_data/config/
cfg_incl_path=~/printer_data/config/printer.cfg

read -p " Do you want to install MMU3 5x or 12x? (y=5x / n=12x): " answer
if [ "$answer" != "${answer#[Yy]}" ]; then
    cfg_name=mmu3.cfg
else
    cfg_name=mmu3-12x.cfg
fi
cp -f "$repo_path/$cfg_name" $cfg_path # Overwrite

# Adding the [include mmu3.cfg] line to printer.cfg
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

cfg_name=mmu3_menus.cfg
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
