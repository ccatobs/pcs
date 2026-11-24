"""parse_az_scan_params, the check of az_scan's scan_params, called directly.

The task-level tests (test_az_scan_start_time.py, test_az_scan_sun_check.py)
show that a refusal fails the task and sends nothing; these pin each check's
refusal, the key set against aculib's signature, that the caller's dictionary is
left as given, and which fault is named when there are several. ocs-free: imports
trajectory.py and aculib.
"""

import inspect

import pytest

from pcs.agents.acu_interface.aculib import observatory_control_system
from pcs.agents.acu_interface.trajectory import AZ_SCAN_PARAM_KEYS, parse_az_scan_params

SCAN = dict(start_time="2099-01-01T00:00:00Z", azimuth_range=[100, 140], elevation=50,
            speed=0.5, num_scans=2, turnaround_time=2)

#: 2099-01-01T00:00:00 UTC as Unix seconds.
UNIX_2099 = 4070908800.0


def test_the_keys_are_the_arguments_of_aculib_azimuth_scan():
    parameters = inspect.signature(observatory_control_system.azimuth_scan).parameters
    assert AZ_SCAN_PARAM_KEYS == set(parameters) - {"self"}


def test_a_copy_is_returned_with_the_start_as_unix_seconds_and_the_pattern_as_floats():
    given = dict(SCAN)
    scan_params, azimuth_range, elevation, speed, num_scans, turnaround_time = (
        parse_az_scan_params(given))
    assert given == SCAN
    assert scan_params == dict(SCAN, start_time=UNIX_2099)
    assert (azimuth_range, elevation, speed, num_scans, turnaround_time) == (
        [100.0, 140.0], 50.0, 0.5, 2.0, 2.0)
    assert all(type(value) is float
               for value in [*azimuth_range, elevation, speed, num_scans, turnaround_time])


@pytest.mark.parametrize("change, named", [
    ({"mode": 1, "start_time": "tomorrow"}, "unexpected key(s) ['mode']"),
    ({"start_time": "tomorrow", "elevation": None}, "start_time 'tomorrow'"),
    ({"azimuth_range": [1.0], "elevation": "x"}, "azimuth_range: 1 values, not two"),
    ({"elevation": "x", "speed": None}, "elevation: could not convert"),
    ({"speed": 0.0, "turnaround_time": float("nan")}, "turnaround_time: not finite"),
    ({"num_scans": 2.5, "speed": 0.0}, "speed: not positive"),
], ids=["key-before-start", "start-before-pattern", "range-before-elevation",
        "conversion-in-key-order", "conversions-before-ranges", "speed-before-num_scans"])
def test_the_first_fault_in_the_order_of_the_checks_is_named(change, named):
    scan = {key: value for key, value in dict(SCAN, **change).items() if value is not None}
    with pytest.raises(ValueError, match="not launched") as refusal:
        parse_az_scan_params(scan)
    assert named in str(refusal.value), refusal.value


@pytest.mark.parametrize("change, named", [
    ({"start_time": "tomorrow"},
     "start_time 'tomorrow' is neither Unix seconds nor an ISO-8601 UTC string"),
    ({"azimuth_range": [100.0, float("inf")]}, "azimuth_range: not finite"),
    ({"speed": 0.0}, "speed: not positive"),
    ({"num_scans": -1}, "num_scans: not a whole number of at least 0"),
    ({"turnaround_time": -1.0}, "turnaround_time: negative"),
], ids=["start_time", "azimuth_range-infinite", "speed-zero", "num_scans-negative",
        "turnaround_time-negative"])
def test_each_check_refuses_naming_its_key(change, named):
    with pytest.raises(ValueError, match="not launched") as refusal:
        parse_az_scan_params(dict(SCAN, **change))
    assert named in str(refusal.value), refusal.value
