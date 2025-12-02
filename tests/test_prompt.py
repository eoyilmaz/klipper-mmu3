"""Tests for the Text class."""

# Third-Party Imports
import pytest

# Local Imports
from mainsail_prompts import Button, ButtonGroup, Color, FooterButton, Prompt


@pytest.mark.parametrize(
    "headline,widgets,expected", [
        (
            "Test Headline",
            None,
            'RESPOND TYPE=command MSG="action:prompt_begin Test Headline"\n'
            'RESPOND TYPE=command MSG="action:prompt_show"'
        ),
        (
            "Test Headline 2",
            [],
            'RESPOND TYPE=command MSG="action:prompt_begin Test Headline 2"\n'
            'RESPOND TYPE=command MSG="action:prompt_show"'
        ),
        (
            "Test Headline 3",
            [ButtonGroup()],
            'RESPOND TYPE=command MSG="action:prompt_begin Test Headline 3"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"\n'
            'RESPOND TYPE=command MSG="action:prompt_show"'
        ),
        (
            "Test Headline 4",
            [
                ButtonGroup(
                    buttons=[
                        Button(label="test button 2", gcode="test gcode", color=None)
                    ]
                )
            ],
            'RESPOND TYPE=command MSG="action:prompt_begin Test Headline 4"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 2|test gcode|"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"\n'
            'RESPOND TYPE=command MSG="action:prompt_show"'
        ),
        (
            "Test Headline 5",
            [
                ButtonGroup(
                    buttons=[
                        Button(label="test button 3", gcode=None, color=Color.Primary),
                        Button(label="test button 4", gcode="test gcode", color=Color.Primary),
                    ],
                ),
                ButtonGroup(
                    buttons=[
                        Button(label="test button 5", gcode=None, color=Color.Primary),
                        Button(label="test button 6", gcode="test gcode", color=Color.Primary),
                    ],
                ),
                ButtonGroup(
                    buttons=[
                        FooterButton(label="test footer button 7", gcode=None, color=Color.Primary),
                        FooterButton(label="test footer button 8", gcode="test gcode", color=Color.Primary),
                    ],
                )
            ],
            'RESPOND TYPE=command MSG="action:prompt_begin Test Headline 5"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 3||primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 4|test gcode|primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 5||primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button test button 6|test gcode|primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_start"\n'
            'RESPOND TYPE=command MSG="action:prompt_footer_button test footer button 7||primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_footer_button test footer button 8|test gcode|primary"\n'
            'RESPOND TYPE=command MSG="action:prompt_button_group_end"\n'
            'RESPOND TYPE=command MSG="action:prompt_show"'
        )
    ]
)
def test_to_gcode_returns_the_gcode_command(
    headline, widgets, expected
):
    prompt = Prompt(headline=headline, widgets=widgets)
    assert prompt.to_gcode() == expected