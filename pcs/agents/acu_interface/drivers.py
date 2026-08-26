import calendar
import datetime
import time

DAY = 86400


def timecode(acutime, now=None):
    """Convert ACU fractional day-of-year time code to a unix timestamp."""
    sec_of_day = (acutime - 1) * DAY
    if now is None:
        now = time.time()

    if acutime > 180:
        context = datetime.datetime.utcfromtimestamp(now - 30 * DAY)
    else:
        context = datetime.datetime.utcfromtimestamp(now + 30 * DAY)

    year = context.year
    gyear = calendar.timegm(time.strptime(str(year), '%Y'))
    return gyear + sec_of_day
