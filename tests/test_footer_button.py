"""Tests for the Text class."""

# Third-Party Imports
import pytest

# Local Imports
from mainsail_prompts import FooterButton, Color


@pytest.mark.parametrize(
    "label,gcode,color,expected", [
        ("test button",   None, None, 'RESPOND TYPE=command MSG="action:prompt_footer_button test button||"'),
        ("test button 2", "test gcode", None, 'RESPOND TYPE=command MSG="action:prompt_footer_button test button 2|test gcode|"'),
        ("test button 3", None, Color.Primary, 'RESPOND TYPE=command MSG="action:prompt_footer_button test button 3||primary"'),
        ("test button 3", "test gcode", Color.Primary, 'RESPOND TYPE=command MSG="action:prompt_footer_button test button 3|test gcode|primary"'),
    ]
)
def test_to_gcode_returns_the_gcode_command(
    label, gcode, color, expected
):
    button = FooterButton(label=label, gcode=gcode, color=color)
    assert button.to_gcode() == expected