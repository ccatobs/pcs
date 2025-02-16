# bluefors_cu.py

import json
import requests

class BFCU:
    """
        Bluefors Control Unit (BFCU) class.

        Attributes:
            ip (str) - IP address of the windows computer running bluefors control software
            key (str) - API key for access to server address.
            port (str) - Port number at which the HTTPS commands are
            timeout (int) - number of seconds to wait for HTTP requests to go through
            https_root (str) - beginning of the path for any HTTP command for this device

    """

    def __init__(self,ip,key,port='49098',timeout=10):
        self.ip = ip
        self.api_key = key
        self.timeout = timeout
        self.https_root = 'https://' + str(self.ip) + ':' + str(port)
        self.id = self.get_id()

    def msg(self,path):
        """
            Send message to the BF control unit over HTTPS via the requests package.

            Parameters:
            path (str) - the location in the BF control unit API of the command you want
                        Ex.: "/heater" accesses commands for a heater
            message (dict) - a JSON dictionary containing the relevant keys for 
                             executing the desired command within the BF TC API.
                             If message is an empty dictionary, this function
                             will send a GET request. It sends POST requests for
                             a dictionary with keys.
        """
        url = self.https_root + path + '/?key=' + str(self.api_key)
        req = requests.get(url, timeout=self.timeout, verify='server-cert.pem')
        resp = req.json()
        
        return resp
    
    def get_id(self):
        return self.msg('/values/driver/bftc/data/system/device_id')['data']['driver.bftc.data.system.device_id']['content']['latest_value']['value']
        
    def get_pressure(self, value):
        """Returns pressure at a location in the DR system with units of bars
            
            Parameters:
                value (int) - Possible values are 1-6
        """
        response = self.msg('/values/mapper/bf/pressures/p' + str(value))
        latest_value = response['data']['mapper.bf.pressures.p' + str(value)]['content']['latest_value']
        pressure = float(latest_value['value']) * 1000
        time = (latest_value['date']) / 1000
         
        return pressure, time
        
    def get_flow(self):
        """Returns flow rate in mmol/s"""
        
        response = self.msg('/values/mapper/bf/flow')
        latest_value = response['data']['mapper.bf.flow']['content']['latest_value']
        flow = float(latest_value['value'])
        time = (latest_value['date']) / 1000
        
        return flow, time
        
    def get_still_heater_power(self):
        """Returns the power of the still heater in watts"""
        
        response = self.msg('/values/mapper/temperature_control/heaters/still/power')
        latest_value = response['data']['mapper.temperature_control.heaters.still.power']['content']['latest_value']
        power = latest_value['value']
        time = latest_value['date']
        
        return power, time
        
    def get_sample_heater_power(self):
        """Returns the power of the sample heater in watts"""
        return self.msg('/values/mapper/temperature_control/heaters/sample/power')['data']['mapper.temperature_control.heaters.sample.power']['content']['latest_value']['value']
        
    def get_sample_heater_state(self):
        """Returns boolean value of sample heater state
            0 = OFF
            1 = ON
        """
        return self.msg('/values/mapper/temperature_control/heaters/sample/enabled')['data']['mapper.temperature_control.heaters.sample.enabled']['content']['latest_value']['value']
        
    def get_still_heater_state(self):
        """Returns boolean value of still heater state
            0 = OFF
            1 = ON
        """
        return self.msg('/values/mapper/temperature_control/heaters/still/enabled')['data']['mapper.temperature_control.heaters.still.enabled']['content']['latest_value']['value']
