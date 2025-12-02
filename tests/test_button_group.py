"""Tests for the Text class."""

# Third-Party Imports
import pytest

# Local Imports
from mainsail_prompts import Button, ButtonGroup, Color


@pytest.mark.parametrize(
    "buttons,expected", [
        (
            None,
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"'
        ),
        (
            [],
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"'
        ),
        (
            [
                Button(label="test button 2", gcode="test gcode", color=None)
            ],
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 2|test gcode|"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"'
        ),
        (
            [
                Button(label="test button 3", gcode=None, color=Color.Primary),
                Button(label="test button 3", gcode="test gcode", color=Color.Primary),
            ],
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 3||primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 3|test gcode|primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"'
        )
    ]
)
def test_to_gcode_returns_the_gcode_command(
    buttons, expected
):
    button_group = ButtonGroup(buttons=buttons)
    assert button_group.to_gcode() == expected