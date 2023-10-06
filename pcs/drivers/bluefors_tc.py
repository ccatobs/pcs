# bluefors_tc.py

import json
import requests

import numpy as np


class BFTC:
    """
        Bluefors Temperature Controller (BFTC) class.

        Attributes:
            channels - list of channels, index corresponds to channel number
    """

    def __init__(self,ip,timeout=10,num_channels=8):
        self.ip = ip
        self.num_channels = num_channels


        #Still need to initialize channels - automatic way to do this?
        #Still need to initialize heaters - automatic way to do this?


    def msg(self,message):
        """
            Send message to the BF TC over HTTP via the requests package.


        """
        pass


class Channel:
    pass

class Heater:
    pass



