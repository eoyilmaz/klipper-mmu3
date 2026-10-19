"""Tests for the ``[mmu3 MMU3]`` migration stub."""

# Third-Party Imports
import pytest

# Local Imports
from extras import mmu3


class FakeConfigError(Exception):
    """Klipper's ``config.error`` stand-in."""


class FakeConfig:
    """A ``ConfigWrapper`` stand-in for a single section."""

    error = FakeConfigError

    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


@pytest.mark.parametrize(
    ("load", "section"),
    [
        (mmu3.load_config_prefix, "mmu3 MMU3"),
        (mmu3.load_config, "mmu3"),
    ],
)
def test_old_section_explains_the_rename(load, section) -> None:
    with pytest.raises(FakeConfigError) as exc_info:
        load(FakeConfig(section))
    message = str(exc_info.value)
    assert f"[{section}] section was renamed to [mmu]" in message
    assert "printer.mmu" in message
    assert "install.sh" in message
