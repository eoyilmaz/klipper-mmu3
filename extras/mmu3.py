"""Migration stub for the old ``[mmu3 MMU3]`` config section.

The MMU3 extension is now ``extras/mmu.py`` and is configured with Happy
Hare's ``[mmu]`` section. Klipper still finds this module through the symlink
older versions of ``install.sh`` created, so a leftover ``[mmu3 MMU3]``
section stops Klipper with a message that explains the rename instead of
loading an outdated MMU3.
"""

# Standard Library Imports
from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from configfile import ConfigWrapper


RENAME_MESSAGE = (
    "The [{section}] section was renamed to [mmu] and the config files to "
    "mmu.cfg / mmu-12x.cfg / mmu-12x-ng.cfg. Run install.sh from the "
    "klipper-mmu3 repository again, it links the new modules, installs the new "
    "config file and switches the [include] in printer.cfg to it. Then copy "
    "your settings from the old mmu3 config file, and replace "
    "printer['mmu3 MMU3'] with printer.mmu in your own macros."
)


def load_config(config: ConfigWrapper) -> NoReturn:
    """Refuse the old ``[mmu3]`` section.

    Args:
        config (ConfigWrapper): The config wrapper.

    Raises:
        config.error: Always, the section was renamed to ``[mmu]``.
    """
    raise config.error(RENAME_MESSAGE.format(section=config.get_name()))


def load_config_prefix(config: ConfigWrapper) -> NoReturn:
    """Refuse the old ``[mmu3 MMU3]`` section.

    Args:
        config (ConfigWrapper): The config wrapper.

    Raises:
        config.error: Always, the section was renamed to ``[mmu]``.
    """
    raise config.error(RENAME_MESSAGE.format(section=config.get_name()))
