"""Tests for the Text class."""

# Local Imports
from mainsail_prompts import Text


def test_to_gcode_returns_the_gcode_command():
    text = Text(text="test text.")
    assert text.to_gcode() == 'RESPOND TYPE=command MSG="action:prompt_text test text."'