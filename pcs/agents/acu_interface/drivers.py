#!/bin/python
import calendar
import datetime
import math
import time
from dataclasses import dataclass

import numpy as np
#: The number of seconds in a day.
DAY = 86400

@dataclass
class TrackPoint:
    #: Timestamp of the point (unix timestamp)
    timestamp: float

    #: Azimuth (deg).
    az: float

    #: Elevation (deg).
    el: float

    #: Azimuth velocity (deg/s).
    az_vel: float

    #: Elevation velocity (deg/s).
    el_vel: float

    #: Az flag: 0 if stationary, 1 if non-final point of const-vel
    #: scan segment; 2 if final point of const-vel segment.
    az_flag: int = 0

    #: El flag: like az_flag but for el.
    el_flag: int = 0

    #: If 1, indicates that once this point is uploaded the next point
    #: in sequence also needs to be soon uploaded.  Used at start of a
    #: new const-vel scan segment.
    group_flag: int = 0

def timecode(acutime, now=None):
    """Takes the time code produced by the ACU status stream and returns
    a unix timestamp.

    Parameters:
        acutime (float): The time recorded by the ACU status stream,
            corresponding to the fractional day of the year.
        now (float): The time, as unix timestamp, to assume it is now.
            This is for testing, it defaults to time.time().
    """
    sec_of_day = (acutime - 1) * DAY
    if now is None:
        now = time.time()  # testing

    # This guard protects us at end of year, when time.time() and
    # acutime might correspond to different years.
    if acutime > 180:
        context = datetime.datetime.utcfromtimestamp(now - 30 * DAY)
    else:
        context = datetime.datetime.utcfromtimestamp(now + 30 * DAY)

    year = context.year
    gyear = calendar.timegm(time.strptime(str(year), '%Y'))
    comptime = gyear + sec_of_day
    return comptime


#: Minimum number of points to group together at the start of a new
#: const-vel leg, matching the SO ACU driver convention.
MIN_GROUP_NEW_LEG = 4


def trajectory_to_track_points(trajectory, batch_size=500):
    """Convert a fyst_trajectories Trajectory object into a generator
    that yields lists of TrackPoint objects, compatible with _run_track.

    The Trajectory scan_flag values (1=science sweep, 2=turnaround) are
    mapped to the ACU az_flag convention:
      - science sweep interior points  -> az_flag=1
      - final point of a sweep leg     -> az_flag=2
      - turnaround / unclassified      -> az_flag=0

    A group_flag=1 is set on the first MIN_GROUP_NEW_LEG points of each
    new const-vel leg to match the batching behaviour of
    generate_constant_velocity_scan.

    Args:
        trajectory: fyst_trajectories.trajectory.Trajectory instance.
        batch_size (int): number of TrackPoint objects per yielded batch.

    Yields:
        list of TrackPoint
    """
    from fyst_trajectories.trajectory import SCAN_FLAG_SCIENCE, SCAN_FLAG_TURNAROUND

    t0 = trajectory.start_time.unix if trajectory.start_time is not None else time.time()
    times = trajectory.times
    azs = trajectory.az
    els = trajectory.el
    az_vels = trajectory.az_vel
    el_vels = trajectory.el_vel
    flags = trajectory.scan_flag  # may be None

    n = len(times)
    points = []
    group_countdown = 0

    for i in range(n):
        sf = int(flags[i]) if flags is not None else 0

        if sf == SCAN_FLAG_SCIENCE:
            # Flag=2 on the last point of each sweep leg (transition to non-science).
            is_last_science = (i == n - 1) or (int(flags[i + 1]) != SCAN_FLAG_SCIENCE)
            az_flag = 2 if is_last_science else 1
        else:
            az_flag = 0

        # Detect start of a new const-vel leg to set group_flag.
        if sf == SCAN_FLAG_SCIENCE and (i == 0 or int(flags[i - 1]) != SCAN_FLAG_SCIENCE):
            group_countdown = MIN_GROUP_NEW_LEG

        group_flag = 1 if group_countdown > 0 else 0
        if group_countdown > 0:
            group_countdown -= 1

        points.append(TrackPoint(
            timestamp=t0 + float(times[i]),
            az=float(azs[i]),
            el=float(els[i]),
            az_vel=float(az_vels[i]),
            el_vel=float(el_vels[i]),
            az_flag=az_flag,
            el_flag=0,
            group_flag=group_flag,
        ))

        if len(points) >= batch_size:
            yield points
            points = []

    if points:
        yield points
