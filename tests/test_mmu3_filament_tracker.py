"""Tests for the filament position / bowden progress the MMU panel shows.

``filament_position`` is the distance of the filament tip from FINDA, and
``bowden_progress`` how far a FINDA <-> extruder move has got. Both are
followed live from the pulley stepper's host side step history and must never
query the MCU.
"""

# Standard Library Imports
import sys
import types

# Third-Party Imports
import pytest

# The real module imports Klipper's ``extras.manual_stepper``; stub it so the
# module under test can be imported standalone.
sys.modules.setdefault(
    "extras.manual_stepper",
    types.SimpleNamespace(ManualStepper=object),
)

# Local Imports
from extras.mmu3 import (  # noqa: E402
    MMU3,
    FilamentPos,
    FilamentTracker,
    tracks_filament,
)
from extras.mmu3_hh_compat import (  # noqa: E402
    FILAMENT_POS_HOMED_GATE,
    FILAMENT_POS_IN_BOWDEN,
    MmuStatus,
)
from tests.test_mmu3_hh_status import make_mmu  # noqa: E402

STEP_DIST = 0.01  # mm per step


class FakeMcu:
    """An MCU stand-in, print time is the event time."""

    def estimated_print_time(self, eventtime):
        return eventtime


class FakeMcuStepper:
    """A pulley ``MCU_stepper`` stand-in.

    ``queued`` is where the stepper ends up once every queued move is done,
    ``past`` maps a print time to the step count at that time. Any other
    attribute access (e.g. querying the MCU) fails the test.
    """

    def __init__(self) -> None:
        self.queued = 0
        self.past = {}

    def get_mcu(self):
        return FakeMcu()

    def get_step_dist(self):
        return STEP_DIST

    def get_mcu_position(self):
        return self.queued

    def get_past_mcu_position(self, print_time):
        return self.past.get(print_time, self.queued)


class FakePulley:
    """A pulley ``manual_stepper`` stand-in."""

    def __init__(self) -> None:
        self.mcu_stepper = FakeMcuStepper()

    def get_steppers(self):
        return [self.mcu_stepper]


class FakeToolhead:
    def flush_step_generation(self) -> None:
        pass


def make_tracked_mmu() -> MMU3:
    mmu = make_mmu()
    mmu.pulley_stepper = FakePulley()
    mmu.toolhead = FakeToolhead()
    return mmu


def steps(mm: float) -> int:
    return round(mm / STEP_DIST)


def status(mmu: MMU3, eventtime: float = 0.0) -> dict:
    return MmuStatus(mmu).get_status(eventtime)


# ---------------------------------------------------------------------------
# idle values at each FilamentPos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pos, expected",
    [
        (FilamentPos.UNLOADED, 0.0),
        (FilamentPos.AT_FINDA, 0.0),
        (FilamentPos.AT_EXTRUDER, 450.0),
        (FilamentPos.IN_HOTEND, 470.0),
        (FilamentPos.LOADED, 500.0),
    ],
)
def test_idle_position_is_nominal(pos, expected) -> None:
    mmu = make_tracked_mmu()
    mmu.filament_pos = pos
    st = status(mmu)
    assert st["filament_position"] == expected
    assert st["bowden_progress"] == -1


def test_idle_values_without_pulley_stepper() -> None:
    """The panel can poll before klippy:connect looked the steppers up."""
    mmu = make_mmu()
    mmu.filament_pos = FilamentPos.LOADED
    st = status(mmu)
    assert st["filament_position"] == 500.0
    assert st["bowden_progress"] == -1


# ---------------------------------------------------------------------------
# live tracking
# ---------------------------------------------------------------------------
def test_bowden_load_progress_is_live() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    mmu.filament_pos = FilamentPos.AT_FINDA

    with mmu.filament_tracker.track(is_bowden_move=True):
        stepper.queued = steps(450)  # the bulk move is queued
        stepper.past = {1.0: 0, 2.0: steps(112.5), 3.0: steps(225)}
        assert [status(mmu, t)["bowden_progress"] for t in (1.0, 2.0, 3.0)] == [
            0,
            25,
            50,
        ]
        assert status(mmu, 2.0)["filament_position"] == 112.5
        # the panel draws the filament in the bowden meanwhile
        assert status(mmu, 2.0)["filament_pos"] == FILAMENT_POS_IN_BOWDEN
        mmu.filament_pos = FilamentPos.AT_EXTRUDER

    st = status(mmu)
    assert st["bowden_progress"] == -1
    assert st["filament_position"] == 450.0


def test_bowden_progress_is_clamped_to_100() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    mmu.filament_pos = FilamentPos.AT_FINDA
    with mmu.filament_tracker.track(is_bowden_move=True):
        # the retries pushed past the nominal bowden length
        stepper.queued = steps(490)
        assert status(mmu, 5.0)["bowden_progress"] == 100
        assert status(mmu, 5.0)["filament_position"] == 490.0


def test_measured_load_length_is_kept_while_the_position_holds() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    mmu.filament_pos = FilamentPos.AT_FINDA
    with mmu.filament_tracker.track(is_bowden_move=True):
        stepper.queued = steps(470)
        mmu.filament_pos = FilamentPos.AT_EXTRUDER
    assert status(mmu)["filament_position"] == 470.0

    # set some other way, e.g. MMU_RECOVER: back to the nominal value
    mmu.filament_pos = FilamentPos.LOADED
    assert status(mmu)["filament_position"] == 500.0


def test_bowden_unload_counts_down_to_finda() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    stepper.queued = steps(1000)
    mmu.filament_pos = FilamentPos.AT_EXTRUDER

    with mmu.filament_tracker.track(is_bowden_move=True):
        # the full unload move, stopped by FINDA
        stepper.queued = steps(1000 - 830)
        stepper.past = {1.0: steps(1000), 2.0: steps(1000 - 112.5)}
        assert status(mmu, 1.0)["bowden_progress"] == 100
        assert status(mmu, 2.0)["bowden_progress"] == 75
        assert status(mmu, 2.0)["filament_position"] == 337.5
        # past FINDA the distance does not go negative
        assert status(mmu, 3.0)["filament_position"] == 0.0
        mmu.filament_pos = FilamentPos.AT_FINDA

    assert status(mmu)["filament_position"] == 0.0


def test_step_starts_from_the_measured_position() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    mmu.filament_pos = FilamentPos.AT_FINDA
    with mmu.filament_tracker.track(is_bowden_move=True):
        stepper.queued = steps(460)
        mmu.filament_pos = FilamentPos.AT_EXTRUDER

    with mmu.filament_tracker.track():
        stepper.queued = steps(460 + 20)
        # not a bowden move
        assert status(mmu, 9.0)["bowden_progress"] == -1
        assert status(mmu, 9.0)["filament_pos"] != FILAMENT_POS_IN_BOWDEN
        assert status(mmu, 9.0)["filament_position"] == 480.0
        mmu.filament_tracker.advance(30)  # extruder only, pulley released
        mmu.filament_pos = FilamentPos.LOADED

    assert status(mmu)["filament_position"] == 510.0


def test_advance_outside_a_step_is_ignored() -> None:
    mmu = make_tracked_mmu()
    mmu.filament_pos = FilamentPos.LOADED
    mmu.filament_tracker.advance(30)
    assert status(mmu)["filament_position"] == 500.0


def test_nested_tracking_merges_into_the_outer_step() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    tracker = mmu.filament_tracker
    mmu.filament_pos = FilamentPos.AT_FINDA
    with tracker.track(is_bowden_move=True):
        stepper.queued = steps(100)
        with tracker.track():
            stepper.queued = steps(200)
            assert tracker.is_bowden_move
        assert tracker.is_tracking
        assert tracker.position(0.0) == 200.0
    assert not tracker.is_tracking


def test_failed_step_stops_tracking_without_measuring() -> None:
    mmu = make_tracked_mmu()
    stepper = mmu.pulley_stepper.mcu_stepper
    mmu.filament_pos = FilamentPos.AT_EXTRUDER
    with pytest.raises(RuntimeError):  # noqa: SIM117, parenthesized needs 3.10
        with mmu.filament_tracker.track(is_bowden_move=True):
            stepper.queued = steps(-100)
            raise RuntimeError("stepper error")
    assert not mmu.filament_tracker.is_tracking
    assert status(mmu)["filament_position"] == 450.0
    assert status(mmu)["bowden_progress"] == -1


def test_tracks_filament_decorator() -> None:
    seen = {}

    class Fake:
        def __init__(self, mmu):
            self.filament_tracker = mmu.filament_tracker

        @tracks_filament(is_bowden_move=True)
        def move(self):
            seen["bowden"] = self.filament_tracker.is_bowden_move
            return True

    mmu = make_tracked_mmu()
    assert Fake(mmu).move() is True
    assert seen == {"bowden": True}
    assert not mmu.filament_tracker.is_tracking


def test_filament_pos_outside_bowden_move_is_unchanged() -> None:
    mmu = make_tracked_mmu()
    mmu.filament_pos = FilamentPos.AT_FINDA
    assert status(mmu)["filament_pos"] == FILAMENT_POS_HOMED_GATE


def test_nominal_position_follows_configured_lengths() -> None:
    mmu = make_mmu()
    mmu.bowden_load_length1 = 600
    mmu.bowden_load_length3 = 25
    mmu.extra_load_length = 0
    tracker = FilamentTracker(mmu)
    assert tracker.nominal_position(FilamentPos.AT_EXTRUDER) == 600
    assert tracker.nominal_position(FilamentPos.LOADED) == 625
