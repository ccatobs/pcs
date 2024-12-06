import sys
import time

import numpy as np
import serial

units_key = {'1': 'kelvin',
             '2': 'ohms'}

units_lock = {'kelvin': '1',
              'ohms': '2'}

class LS325:
    """
        Lakeshore 370 class.

    Attributes:
        channels - list of channels, index corresponds to channel number with
                   index 0 corresponding to channel 1
    """

    _bytesize = serial.SEVENBITS
    _parity = serial.PARITY_ODD
    _stopbits = serial.STOPBITS_ONE

    def __init__(self, port, baudrate=9600, timeout=10, num_channels=16): #325 only has two input channels 'A' and 'B'
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        
        
        self.com = serial.Serial(self.port, self.baudrate, self._bytesize, self._parity, self._stopbits, self.timeout)
        
 
        
        
        
        
        self.num_channels = num_channels

        self.id = self.get_id()
        self.heater1 = Heater(self)
        
        

    def msg(self, message):
        """Send message to the Lakeshore 370 over RS-232.

        If we're asking for something from the Lakeshore (indicated by a ? in
        the message string), then we will attempt to ask twice before giving up
        due to potential communication timeouts.

        Parameters
        ----------
        message : str
            Message string as described in the Lakeshore 370 manual.

        Returns
        -------
        str
            Response string from the Lakeshore, if any. Else, an empty string.

        """
        msg_str = f'{message}\r\n'.encode()
        self.com.write(msg_str)
        resp = ''

        
        if '?' in message:
            resp = str(self.com.read_until(), 'utf-8').strip()
           
            # Try a few times, if we timeout, try again.
            try_count = 3
            while resp == '':
                if try_count == 0:
                    break

                print(f"Warning: Caught timeout waiting for response to {message}, waiting 1s and "
                      "trying again {try_count} more time(s) before giving up")
                time.sleep(1)

                # retry comms
                self.com.write(msg_str)
                resp = str(self.com.read_until(), 'utf-8').strip()
                try_count -= 1
         
        time.sleep(0.1)  # No comms for 100ms after sending message (manual says 50ms)

        return resp
        
    def get_id(self):
        """Get the ID number of the Lakeshore unit."""
        return self.msg('*IDN?')
        
class Heater:
    """Heater class for LS370 control

    :param ls: the lakeshore object we're controlling
    :type ls: Lakeshore370.LS370
    """

    def __init__(self, ls):
        self.ls = ls

        self.mode = None
        self.input = None
        self.powerup = None 
        self.units = None

        self.range = None
        self.resistance = None  # only for output = 0
        self.display = None
        
    def get_units(self):
        """Get the setpoint units with the CSET? command.

        :returns: units, either 'kelvin' or 'ohms'
        :rtype: str
        """
        resp = self.ls.msg("CSET?")
        return resp

    def set_units(self, resp):
        """Set the setpoint units with the CSET command.

        :param units: units, either 'kelvin' or 'ohms'
        :type units: str
        """
        self.ls.msg(resp)
        return self.get_units()
        
