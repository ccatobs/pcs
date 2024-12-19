#!/bin/python
import calendar
import datetime
import math
import time

import numpy as np
#: The number of seconds in a day.
DAY = 86400

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


