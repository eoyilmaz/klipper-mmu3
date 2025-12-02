"""MainsailPromptBase related tests are situated here."""

# Third-Party Imports
import pytest

# Local Imports
from mainsail_prompts import MainsailPromptBase



def test_to_gcode_raises_not_implemented_error():
    """to_gcode() raises NotImplementedError."""
    prompt = MainsailPromptBase()
    with pytest.raises(NotImplementedError) as cm:
        prompt.to_gcode()

    assert str(cm.value) == "This needs to be implemented in the derived class"

