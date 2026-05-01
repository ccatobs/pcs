from serial import Serial, EIGHTBITS, STOPBITS_ONE, PARITY_NONE
import serial
import time
import math
import usb.core
import usb.util

STATE_STABLE_ZERO = 2
STATE_UNSTABLE_POSITIVE = 3
STATE_STABLE_POSITIVE = 4
STATE_NEGATIVE = 5  # returned both for stable and unstable values

UNITS_KG = 3
UNITS_LB = 12

SCALE_TENTHS = 255
SCALE_HUNDREDTHS = 256

SLEEP_NO_DEVICE = 1
SLEEP_STABLE = 1
SLEEP_UNSTABLE = 0.1

def parse_reading(data):

    state_flag = data[1]
    stable_states = [STATE_STABLE_ZERO, STATE_STABLE_POSITIVE]
    is_stable = state_flag in stable_states
    is_negative = state_flag == STATE_NEGATIVE

    scale_flag = data[3]
    if scale_flag == SCALE_TENTHS:
        scale_factor = 0.1
    elif scale_flag == SCALE_HUNDREDTHS:
        scale_factor = 0.01
    else:
        scale_factor = 100  # want an obviously wrong value

    weight = scale_factor * (data[4] + (256 * data[5]))
    if is_negative:
        weight = weight * -1

    unit_flag = data[2]
    if unit_flag == UNITS_KG:
        unit = 'kg'
    elif unit_flag == UNITS_LB:
        unit = 'lbs'
    else:
        unit = unit_flag

    return {
        'is_stable': is_stable,
        'weight': math.trunc(weight*10)/10,
        'unit': unit
    }

class Module:
    """
        Allows communication to Dymo Module.
        Contains list of inputs which can be read from.
    """
    def __init__(self, port="/dev/ADAM"):
        """
            Establish Serial communication.
        """
        self.port = port
        self.device = Serial(self.port)
        #print(self.device)

        # was it found?
        if self.device is None:
            return None

        # use the first/default configuration
        #try:
         #   device.set_configuration()
        #except Exception:
         #   return None

        #return self.device


    def read_weight(self):
        """
            Sends command to read weight from scale interface.
        """
        self.device.write(b'P\r\n')
        time.sleep(0.1)
        read = self.device.readline().decode().strip()
        value = float(read.split()[1])
        unit = read.split()[2]
        #print(value, unit)
        try:
            return value
        except ValueError:
            print(value)
            return(-99)
