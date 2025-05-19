import sys
import time
import numpy as np
import serial

units_key = {'1': 'Kelvin',
             '2': 'Celcius',
             '3': 'Sensor'}

units_lock = {'Kelvin': 1,
              'Celcius': 2,
              'Sensor': 3}

format_key = {'1': "mV/K",
              '2': "V/K",
              '3': "Ohm/K",
              '4': "log(Ohm)/K"}

format_lock = {"mV/K": 1,
               "V/K": 2,
               "Ohm/K": 3,
               "log(Ohm)/K": 4}
HTRRES_key = {'1': 25,
              '2': 50}
HTRRES_lock = {25: 1,
               50: 2}
RANGE_1_key = {'0': "OFF", 
               '1': "LOW", 
               '2': "HIGH"}
RANGE_1_lock = {"OFF": 0,
                "LOW": 1,
                "HIGH": 2}
RANGE_2_key = {'0': "OFF", 
               '1': "ON"}
RANGE_2_lock = {"OFF": 0,
                "ON": 1}
CMODE_lock = {"Manual PID": 1, 
             "Zone": 2,
             "Open Loop": 3,
             "AutoTune PID": 4,
             "AutoTune PI": 5,
             "AutoTune P": 6}
CMODE_key = {'1': "Manual PID",
              '2': "Zone",
              '3': "Open Loop",
              '4': "AutoTune PID",
              '5': "AutoTune PI",
              '6': "AutoTune P"}    
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
        self.loop_1 = Heater(self, 1)
        self.loop_2 = Heater(self, 2)
        

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
        return self.ls.msg(F'SRDG? {self.name}')

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

    def set_curve_num(self, curve_num):
        #Input Curve Number Command 
        return self.ls.msg(F'INCRV {self.name}, {curve_num}')

    def get_curve_num(self):   
        #Input Curve Number Query 
        return self.ls.msg(F'INCRV? {self.name}')


        
        
        

class Heater:

    def __init__(self, ls, loop_id):
        self.ls = ls
        self.id = loop_id
        self.range = None
        self.resistance = None 

        
    def get_units(self):
    
        resp = self.ls.msg("CSET?").split(",")
        units = units_key[resp[1]]
        return units

    def set_units(self, units):
    
        valid_keys = {key.upper(): key for key in units_lock}
        if units.upper() not in valid_keys:
            raise ValueError(f"Invalid unit type: {units}. Must be one of {list(units_lock.keys())}")
        units_int = valid_keys[units.upper()]
        channel = self.get_control_input()
        return self.ls.msg(f"CSET {self.id},{channel},{units_int},0,2")
    
    def set_resistance(self, ohms):
        if ohms not in HTRRES_lock:
            raise ValueError(f"Invalid heater resistance value: {ohms}. Must be one of {list(HTRRES_lock.keys())}")
        
        response = self.ls.msg(f"HTRRES {self.id},{HTRRES_lock[ohms]}")
        self.resistance = ohms
        return response
        
    def get_resistance(self):
        resp = self.ls.msg(f"HTRRES? {self.id}")
        resistance = HTRRES_key[resp]
        return resistance
    
    def set_range(self, loop_range):
        range_locks = {1: RANGE_1_lock, 2: RANGE_2_lock}
        
        loop_range = loop_range.upper()
        lock_dict = range_locks[self.id]
        valid_keys = {key.upper(): key for key in lock_dict}

        if loop_range not in valid_keys:
            raise ValueError(f"Invalid heater range: {loop_range}. Must be one of {list(lock_dict.keys())}")

        response = self.ls.msg(f"RANGE {self.id},{lock_dict[loop_range]}")
        self.range = loop_range
        return response
        
    def get_range(self):
        range_keys = {1: RANGE_1_key, 2: RANGE_2_key}
        key_dict = range_keys[self.id]
        resp = self.ls.msg(f"RANGE? {self.id}")
        loop_range = key_dict[resp]
        return loop_range
         
    def set_control_input(self, channel):
        channel = channel.upper()
        if channel not in ('A', 'B'):
            raise ValueError(f"Invalid channel ({channel}) Must be A or B")
            
        return self.ls.msg(f"CSET {self.id},{channel},1,0,2")
        
    def get_control_input(self):
        response = self.ls.msg(f"CSET? {self.id}").split(",")
        channel = response[0].upper
        return channel
    
    def set_setpoint(self, temp):
        temp = float(temp)
        return self.ls.msg(f"SETP {self.id},{temp}")
        
    def get_setpoint(self):
        resp =self.ls.msg(f"SETP? {self.id}")
        clean = ''.join(c for c in resp if c.isdigit() or c in ['.', '-', '+'])
        num = float(clean)
        num = f"{num:.3f}"
                
        return num
    
    def set_mode(self, mode):
        mode = mode.upper()
        valid_keys = {key.upper(): key for key in CMODE_lock}
        if mode not in valid_keys:
            raise ValueError(f"Invalid mode: {mode}. Must be one of {list(CMODE_lock.keys())}")
        
        return self.ls.msg(f"CMODE {self.id},{valid_keys[mode]}")

    def get_mode(self):
        response = self.ls.msg(f"CMODE? {self.id}")
        return CMODE_key[response]
        
    def set_pid(self, p, i, d):
    
        assert float(p) <= 1000 and float(p) >= 0
        assert float(i) <= 1000 and float(i) >= 1
        assert float(d) <= 200 and float(d) >= 1

        resp = self.ls.msg(f"PID {self.id},{p},{i},{d}")
        return resp

    
    def get_pid(self):
        resp = self.ls.msg("PID? {self.id}").split(',')
        formatted = []
        for val in resp:
            try:
                clean = ''.join(c for c in val if c.isdigit() or c in ['.', '-', '+'])
                num = float(clean)
                formatted.append(f"{num:.1f}")  # Keep one decimal place
            except ValueError:
                formatted.append('Invalid')
                
        return formatted[0], formatted[1], formatted[2]
        
class Curve:

    def __init__(self, ls, curve_num):
        self.ls = ls
        self.curve_num = curve_num

        self.name = None
        self.serial_number = None
        self.format = None
        self.limit = None
        self.coefficient = None
        self.get_header()  # populates above values
   
   
    def get_header(self):
        """Get curve header description.

        :returns: response from CRVHDR? in list
        :rtype: list of str
        """
        resp = self.ls.msg(f"CRVHDR? {self.curve_num}").split(',')

        _name = resp[0].strip()
        _sn = resp[1].strip()
        _format = resp[2]
        _limit = float(resp[3])
        _coefficient = resp[4]

        self.name = _name
        self.serial_number = _sn

        self.format = format_key[_format]

        self.limit = _limit
        self.coefficient = tempco_key[_coefficient]

        return resp
        
    def _set_header(self, params):
        """Set the Curve Header with the CRVHDR command.

        Parameters should be <name>, <SN>, <format>, <limit value>,
        <coefficient>. We will determine <curve> from attributes. This
        allows us to use output from get_header directly, as it doesn't return
        the curve number.

        <name> is limited to 15 characters. Longer names take the fist 15 characters
        <sn> is limited to 10 characters. Longer sn's take the last 10 digits

        :param params: CRVHDR parameters
        :type params: list of str

        :returns: response from ls.msg
        """
        assert len(params) == 5

        _curve_num = self.curve_num
        _name = params[0][:15]
        _sn = params[1][-10:]
        _format = params[2]
        assert _format.strip() in ['1','2','3', '4']
        _limit = params[3]
        _coeff = params[4]
        assert _coeff.strip() in ['1', '2']

        return self.ls.msg(f'CRVHDR {_curve_num},{_name},{_sn},{_format},{_limit},{_coeff}')
        
    def get_name(self):
        """Get the curve name with the CRVHDR? command.

        :returns: The curve name
        :rtype: str
        """
        self.get_header()
        return self.name

    def set_name(self, name):
        """Set the curve name with the CRVHDR command.

        :param name: The curve name, limit of 15 characters, longer names get truncated
        :type name: str

        :returns: the response from the CRVHDR command
        :rtype: str
        """
        resp = self.get_header()
        resp[0] = name.upper()
        self.name = resp[0]
        return self._set_header(resp)

    def get_serial_number(self):
        """Get the curve serial number with the CRVHDR? command."

        :returns: The curve serial number
        :rtype: str
        """
        self.get_header()
        return self.serial_number

    def set_serial_number(self, serial_number):
        """Set the curve serial number with the CRVHDR command.

        :param serial_number: The curve serial number, limit of 10 characters,
                              longer serials get truncated
        :type name: str

        :returns: the response from the CRVHDR command
        :rtype: str
        """
        resp = self.get_header()
        resp[1] = serial_number
        self.serial_number = resp[1]
        return self._set_header(resp)

    def get_format(self):
        """Get the curve data format with the CRVHDR? command."

        :returns: The curve data format
        :rtype: str
        """
        self.get_header()
        return self.format

    def set_format(self, _format):
        """Set the curve format with the CRVHDR command.

        :param _format: The curve format, valid formats are:
                          "Ohm/K (linear)"
                          "log Ohm/K (linear)"
                          "Ohm/K (cubic spline)"
        :type name: str

        :returns: the response from the CRVHDR command
        :rtype: str
        """
        resp = self.get_header()

        assert _format in format_lock.keys(), "Please select a valid format"

        resp[2] = format_lock[_format]
        self.format = _format
        return self._set_header(resp)

    def get_limit(self):
        """Get the curve temperature limit with the CRVHDR? command.

        :returns: The curve temperature limit
        :rtype: str
        """
        self.get_header()
        return self.limit

    def set_limit(self, limit):
        """Set the curve temperature limit with the CRVHDR command.

        :param limit: The curve temperature limit
        :type limit: float

        :returns: the response from the CRVHDR command
        :rtype: str
        """
        resp = self.get_header()
        resp[3] = str(limit)
        self.limit = limit
        return self._set_header(resp)

    def get_coefficient(self):
        """Get the curve temperature coefficient with the CRVHDR? command.

        :returns: The curve temperature coefficient
        :rtype: str
        """
        self.get_header()
        return self.coefficient

    def set_coefficient(self, coefficient):
        """Set the curve temperature coefficient with the CRVHDR command.

        :param coefficient: The curve temperature coefficient, either 'positive' or 'negative'
        :type limit: str

        :returns: the response from the CRVHDR command
        :rtype: str
        """
        assert coefficient in ['positive', 'negative']

        resp = self.get_header()
        resp[4] = tempco_lock[coefficient]
        self.tempco = coefficient
        return self._set_header(resp)

    def get_data_point(self, index):
        """Get a single data point from a curve, given the index, using the
        CRVPT? command.

        The format for the return value, a 2-tuple of floats, is chosen to work
        with how the get_curve() method later stores the entire curve in a
        numpy structured array.

        :param index: index of breakpoint to query
        :type index: int

        :returns: (units, temperature) values for the given breakpoint
        :rtype: 3-tuple of floats
        """
        resp = self.ls.msg(f"CRVPT? {self.curve_num},{index}").split(',')
        _units = float(resp[0])
        _temp = float(resp[1])
        return (_units, _temp)

    def _set_data_point(self, index, units, kelvin, curvature=None):
        """Set a single data point with the CRVPT command.

        :param index: data point index
        :type index: int
        :param units: value of the sensor units to 6 digits
        :type units: float
        :param kelvin: value of the corresponding temp in Kelvin to 6 digits
        :type kelvin: float

        :returns: response from the CRVPT command
        :rtype: str
        """
        resp = self.ls.msg(f"CRVPT {self.curve_num}, {index}, {units}, {kelvin}")
        return resp

    # Public API Elements
    def get_curve(self, _file=None):
        """Get a calibration curve from the LS370.

        If _file is not None, save to file location.

        :param _file: the file to load the calibration curve from
        :type _file: str
        """
        breakpoints = []
        for i in range(1, 201):
            x = self.get_data_point(i)
            if x[0] == 0:
                break
            breakpoints.append(x)

        struct_array = np.array(breakpoints, dtype=[('units', 'f8'),
                                                    ('temperature', 'f8')])

        self.breakpoints = struct_array

        if _file is not None:
            with open(_file, 'w') as f:
                f.write('Sensor Model:\t' + self.name + '\r\n')
                f.write('Serial Number:\t' + self.serial_number + '\r\n')
                f.write('Data Format:\t' + format_lock[self.format] + f'\t({self.format})\r\n')

                # TODO: shouldn't this be the curve_header limit?
                # above is done ZA 20200405
                f.write('SetPoint Limit:\t%s\t(Kelvin)\r\n' % '%0.4f' % self.limit)
                f.write('Temperature coefficient:\t' + tempco_lock[self.coefficient] + f' ({self.coefficient})\r\n')
                f.write('Number of Breakpoints:\t%s\r\n' % len(self.breakpoints))
                f.write('\r\n')
                f.write('No.\tUnits\tTemperature (K)\r\n')
                f.write('\r\n')
                for idx, point in enumerate(self.breakpoints):
                    f.write('%s\t%s %s\r\n' % (idx + 1, '%0.4f' % point['units'], '%0.4f' % point['temperature']))

        return self.breakpoints

    def set_curve(self, _file):
        """Set a calibration curve, loading it from the file.

        :param _file: the file to load the calibration curve from
        :type _file: str

        :returns: return the new curve header, refreshing the attributes
        :rtype: list of str
        """
        with open(_file) as f:
            content = f.readlines()

        header = []
        for i in range(0, 6):
            if i < 2 or i > 4:
                header.append(content[i].strip().split(":", 1)[1].strip())
            else:
                header.append(content[i].strip().split(":", 1)[1].strip().split("(", 1)[0].strip())

        # Skip to the R and T values in the file and strip them of tabs, newlines, etc
        values = []
        for i in range(9, len(content)):
            values.append(content[i].strip().split())

        self.delete_curve()  # remove old curve first, so old breakpoints don't remain

        self._set_header(header[:-1])  # ignore num of breakpoints

        for point in values:
            print("uploading", point)
            self._set_data_point(point[0], point[1], point[2])

        # refresh curve attributes
        self.get_header()

    def delete_curve(self):
        """Delete the curve using the CRVDEL command.

        :returns: the response from the CRVDEL command
        :rtype: str
        """
        resp = self.ls.msg(f"CRVDEL {self.curve_num}")
        self.get_header()
        return resp

    def __str__(self):
        string = "-" * 50 + "\n"
        string += "Curve %d: %s\n" % (self.curve_num, self.name)
        string += "-" * 50 + "\n"
        string += "  %-30s\t%r\n" % ("Serial Number:", self.serial_number)
        string += "  %-30s\t%s (%s)\n" % ("Format :", format_lock[self.format], self.format)
        string += "  %-30s\t%s\n" % ("Temperature Limit:", self.limit)
        string += "  %-30s\t%s\n" % ("Temperature Coefficient:", self.coefficient)

        return string

    def get_temperature(self, channel: str) -> float:
        """Read temperature in Kelvin from channel A or B."""
        resp = self.ls.msg(f"KRDG? {channel.upper()}")
        return float(resp.strip())

    def set_heater_range(self, rng: int) -> None:
        """Set heater output range: 0=off, 1=low, 2=med, 3=high."""
        self.ls.msg(f"RANGE {rng}")

    def set_heater_power(self, percent: float) -> None:
        """Set heater output power manually as a percentage."""
        self.ls.msg(f"MOUT 1,{percent}")

    def set_pid(self, p: float, i: float, d: float) -> None:
        """Set PID parameters for control loop 1."""
        self.ls.msg(f"PID 1,{p},{i},{d}")

    def set_setpoint(self, temp: float) -> None:
        """Set the desired control loop setpoint (in Kelvin)."""
        self.ls.msg(f"SETP 1,{temp}")

    def enable_control_loop(self) -> None:
        """
        Configure loop 1:
        - Input A
        - Units: Kelvin (1)
        - Enabled at power-up
        - Power-based control
        """
        self.ls.msg("CSET 1,A,1,1,2")
