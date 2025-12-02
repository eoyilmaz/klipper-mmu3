"""Color enum related test are situated here."""

# Standard Library Imports
import sys
from enum import Enum

# Third-Party Imports
import pytest

# Local imports
from mainsail_prompts import Color



@pytest.mark.parametrize(
    "color",
    [
        Color.Primary,
        Color.Secondary,
        Color.Info,
        Color.Warning,
        Color.Error,
    ],
)
def test_it_is_an_enum(color):
    """Color is an Enum."""
    assert isinstance(color, Enum)


@pytest.mark.parametrize(
    "color,expected_value",
    [
        [Color.Primary, "primary"],
        [Color.Secondary, "secondary"],
        [Color.Info, "info"],
        [Color.Warning, "warning"],
        [Color.Error, "error"],
    ],
)
def test_enum_values(color, expected_value):
    """Test enum values."""
    assert color.value == expected_value


@pytest.mark.parametrize(
    "color,expected_name",
    [
        [Color.Primary, "Primary"],
        [Color.Secondary, "Secondary"],
        [Color.Info, "Info"],
        [Color.Warning, "Warning"],
        [Color.Error, "Error"],
    ],
)
def test_enum_names(color, expected_name):
    """Test enum names."""
    assert color.name == expected_name


@pytest.mark.parametrize(
    "color,expected_value",
    [
        [Color.Primary, "primary"],
        [Color.Secondary, "secondary"],
        [Color.Info, "info"],
        [Color.Warning, "warning"],
        [Color.Error, "error"],
    ],
)
def test_enum_as_str(color, expected_value):
    """Test enum names."""
    assert str(color) == expected_value


def test_to_color_color_is_skipped():
    """Color.to_color() color is skipped."""
    with pytest.raises(TypeError) as cm:
        _ = Color.to_color()

    py_error_message = {
        8: "to_color() missing 1 required positional argument: 'color'",
        9: "to_color() missing 1 required positional argument: 'color'",
        10: "Color.to_color() missing 1 required positional argument: 'color'",
        11: "Color.to_color() missing 1 required positional argument: 'color'",
        12: "Color.to_color() missing 1 required positional argument: 'color'",
        13: "Color.to_color() missing 1 required positional argument: 'color'",
    }[sys.version_info.minor]
    assert str(cm.value) == py_error_message


def test_to_color_color_is_none():
    """Color.to_color() color is None."""
    with pytest.raises(TypeError) as cm:
        _ = Color.to_color(None)
    assert str(cm.value) == (
        "color should be a Color enum value or one of ['Primary', 'Secondary', "
        "'Info', 'Warning', 'Error', 'primary', 'secondary', 'info', 'warning', "
        "'error'], not NoneType: 'None'"
    )


def test_to_color_color_is_not_a_str():
    """Color.to_color() color is not a str."""
    with pytest.raises(TypeError) as cm:
        _ = Color.to_color(12334.123)

    assert str(cm.value) == (
        "color should be a Color enum value or one of ['Primary', 'Secondary', "
        "'Info', 'Warning', 'Error', 'primary', 'secondary', 'info', 'warning', "
        "'error'], not float: '12334.123'"
    )


def test_to_color_color_is_not_a_valid_str():
    """Color.to_color() color is not a valid str."""
    with pytest.raises(ValueError) as cm:
        _ = Color.to_color("not a valid value")

    assert str(cm.value) == (
        "color should be a Color enum value or one of ['Primary', 'Secondary', "
        "'Info', 'Warning', 'Error', 'primary', 'secondary', 'info', 'warning', "
        "'error'], not 'not a valid value'"
    )


@pytest.mark.parametrize(
    "color_name,color",
    [
        # Primary
        ["Primary", Color.Primary],
        ["primary", Color.Primary],
        ["PRIMARY", Color.Primary],
        ["PrImArY", Color.Primary],
        ["pRiMaRy", Color.Primary],
        # Secondary
        ["Secondary", Color.Secondary],
        ["secondary", Color.Secondary],
        ["SECONDARY", Color.Secondary],
        ["SeCoNdArY", Color.Secondary],
        ["sEcOnDaRy", Color.Secondary],
        # Info
        ["Info", Color.Info],
        ["info", Color.Info],
        ["INFO", Color.Info],
        ["InFo", Color.Info],
        ["iNfO", Color.Info],
        # Warning
        ["Warning", Color.Warning],
        ["warning", Color.Warning],
        ["WARNING", Color.Warning],
        ["WaRnInG", Color.Warning],
        ["wArNiNg", Color.Warning],
        # Error
        ["Error", Color.Error],
        ["error", Color.Error],
        ["ERROR", Color.Error],
        ["ErRoR", Color.Error],
        ["eRrOr", Color.Error],
    ],
)
def test_schedule_color_to_color_is_working_properly(color_name, color):
    """Color can parse schedule color names."""
    assert Color.to_color(color_name) == color
