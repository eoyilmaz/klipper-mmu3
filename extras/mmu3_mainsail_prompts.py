# Standard Library Imports
from __future__ import annotations

from enum import Enum


class Color(Enum):
    Primary = "primary"
    Secondary = "secondary"
    Info = "info"
    Warning = "warning"
    Error = "error"

    def __str__(self) -> str:
        """Return the string representation.

        Returns:
            str: The string representation.
        """
        return str(self.value)

    @classmethod
    def to_color(cls, color: str | Color) -> Color:
        """Convert the given color value to a Color enum.

        Args:
            color (str | Color): The value to convert to a Color.

        Raises:
            TypeError: Input value type is invalid.
            ValueError: Input value is invalid.

        Returns:
            Color: The enum.
        """
        if not isinstance(color, (str, Color)):
            raise TypeError(
                "color should be a Color enum value or one of {}, not {}: '{}'".format(
                    [c.name.title() for c in cls] + [c.value for c in cls],
                    color.__class__.__name__,
                    color,
                )
            )
        if isinstance(color, str):
            color_name_lut = dict([(c.name.lower(), c.name) for c in cls])
            color_name_lut.update(dict([(c.value.lower(), c.name) for c in cls]))
            color_lower_case = color.lower()
            if color_lower_case not in color_name_lut:
                raise ValueError(
                    "color should be a Color enum value or one of {}, not '{}'".format(
                        [c.name.title() for c in cls] + [c.value for c in cls], color
                    )
                )

            return cls.__members__[color_name_lut[color_lower_case]]

        return color


class MainsailPromptBase:
    """The base class for all prompt related classes."""

    def to_gcode(self) -> str:
        """Return the GCode command representation of this instance.

        Returns:
            str: The GCode command representation of this instance.
        """
        raise NotImplementedError("This needs to be implemented in the derived class")


class Text(MainsailPromptBase):
    """Implements the Mainsail prompt texts."""

    def __init__(self, text: str) -> None:
        self.text = text

    def to_gcode(self) -> str:
        return f'RESPOND TYPE=command MSG="action:prompt_text {self.text}"'


class Button(MainsailPromptBase):
    """Implements the Mainsail prompt buttons.

    Args:
        label (str):
        gcode (None | str): The GCode to run when the button clicked. Default
            is None.
        color (None | Color): The color of the button. Default is None.
    """

    def __init__(
        self, label: str, gcode: None | str, color: None | Color = None
    ) -> None:
        self.label = label
        self.gcode = gcode if gcode else ""
        self.color = color if color else ""

    def to_gcode(self) -> str:
        """Return the GCode representation.

        Returns:
            str: The string representation of the button.
        """
        return f'RESPOND TYPE=command MSG="action:prompt_button {self.label}|{self.gcode}|{self.color}"'


class FooterButton(Button):
    """Implements the Mainsail prompt footer buttons.

    Args:
        label (str):
        gcode (None | str): The GCode to run when the button clicked. Default
            is None.
        color (None | Color): The color of the button. Default is None.
    """

    def to_gcode(self) -> str:
        """Return the GCode representation.

        Returns:
            str: The string representation of the button.
        """
        return (
            super()
            .to_gcode()
            .replace("action:prompt_button", "action:prompt_footer_button")
        )


class ButtonGroup(MainsailPromptBase):
    """ButtonGroup implements Mainsail button groups."""

    def __init__(self, buttons: None | list[Button] = None) -> None:
        self.buttons = buttons if buttons else []

    def to_gcode(self):
        gcode_buffer = [f'RESPOND TYPE=command MSG="action:prompt_button_group_start"']
        # process buttons
        for button in self.buttons:
            gcode_buffer.append(button.to_gcode())
        gcode_buffer.append(
            f'RESPOND TYPE=command MSG="action:prompt_button_group_end"'
        )
        return "\n".join(gcode_buffer)


class Prompt(MainsailPromptBase):
    """Wrapper for Mainsail prompts.

    This shows a prompt in Mainsail UI for the user to interact with.
    """

    def __init__(
        self,
        headline: str = "",
        widgets: None | list[Text | Button | ButtonGroup] = None,
    ):
        self.headline = headline
        self.widgets = widgets if widgets else []

    def to_gcode(self) -> str:
        """Render the gcode version of this Prompt.

        Returns:
            str: The GCode string that corresponds to this Prompt instance.
        """
        gcode_buffer = [
            f'RESPOND TYPE=command MSG="action:prompt_begin {self.headline}"'
        ]
        for widget in self.widgets:
            gcode_buffer.append(widget.to_gcode())
        gcode_buffer.append('RESPOND TYPE=command MSG="action:prompt_show"')
        return "\n".join(gcode_buffer)
