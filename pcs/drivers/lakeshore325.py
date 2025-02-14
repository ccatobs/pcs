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
        Lakeshore 325 class.

    Attributes:
        channels - list of channels, index corresponds to channel number with
                   index 0 corresponding to channel 1
    """

    _bytesize = serial.SEVENBITS
    _parity = serial.PARITY_ODD
    _stopbits = serial.STOPBITS_ONE

    def __init__(self, port, baudrate=9600, timeout=10): #325 only has two input channels 'A' and 'B'
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.com = serial.Serial(self.port, self.baudrate, self._bytesize, self._parity, self._stopbits, self.timeout)
        self.id = self.get_id()
        self.channel_A = Channel(self, 'A')
        self.channel_B = Channel(self, 'B')
        self.heater_1 = Heater(self, 1)
        self.heater_2 = Heater(self, 2)
        

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
        
        
class Channel:

    def __init__(self, ls, channel_name):
        self.ls = ls
        self.name = channel_name
        self.sensor_type = self._get_input_type()#INTYPE

        #self.celsius_reading = get_celsius_reading() #CRDG? Celsius Reading Query
        #self.resistance_reading = get_resistance() #SRDG? Sensor Units Input Reading Query 

        #CSET? get units
        #TLIMIT Temperature Limit Command  
        #FILTER? 
        #INCRV 
        
    def _get_input_type(self):
        """Get the current sensor type set to the channel"""
        return self.ls.msg('INTYPE? {}'.format(self.name))
        
    def get_resistance(self):
        "returns resistance of channel"
        return float(self.ls.msg(F'SRDG? {self.name}'))

    def get_celsius_reading(self):
        "returns temp in celsius" #CRDG? Celsius Reading Query
        return self.ls.msg(F'CRDG? {self.name}')
        
    def get_kelvin_reading(self):
        #KRDG? Kelvin Reading Query
        return self.ls.msg(F'KRDG? {self.name}')

    def set_temp_limit(self, templim):
        #Temperature Limit Command  
        return self.ls.msg(F'TLIMIT {self.name} {templim}')

    def get_temp_limit(self):
        #Temperature Limit Query  
        return self.ls.msg(F'TLIMIT {self.name}')

    def get_filter_readings(self):
        #FILTER? Input Filter Parameter Query 
        return self.ls.msg(F'FILTER? {self.name}')

    def def_curve_num(self):
        #Input Curve Number Command 
        return self.ls.msg(F'INCRV {self.name}')

    def def_curve_num(self):   
        #Input Curve Number Query 
        return self.ls.msg(F'INCRV? {self.name}')


    #RDGST  
        
        
        
        

class Heater:
    """Heater class for LS370 control

    :param ls: the lakeshore object we're controlling
    :type ls: Lakeshore370.LS370
    """

    def __init__(self, ls, heater_id):
        self.ls = ls
        self.id = heater_id
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
        
