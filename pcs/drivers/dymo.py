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
    def __init__(self, vid=0x0922, pid=0x8009):
        """
            Establish USB communication.
        """

        self.device = usb.core.find(idVendor = vid, idProduct = pid)
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

        if self.device.is_kernel_driver_active(0):
            try:
                self.device.detach_kernel_driver(0)
                print("Kernel driver detached")
            except usb.core.USBError as e:
                print(f"Could not detach: {e}")

        endpoint = self.device[0][(0, 0)][0]
        #print(endpoint)
        data = self.device.read(endpoint.bEndpointAddress, endpoint.wMaxPacketSize)
        #print(data)
        data = parse_reading(data)
        #print(data)
        weight = data['weight']

        return weight
