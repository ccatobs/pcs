from serial import Serial #, EIGHTBITS, STOPBITS_ONE, PARITY_NONE
import serial
import time
#import math
#import usb.core
#import usb.util
#from twisted.internet import threads, reactor

class Module:
    """
        Allows communication to Dymo Module.
        Contains list of inputs which can be read from.
    """
    def __init__(self, port="/dev/ADAM"):
        self.port = port
        self.device = None

    def connect(self):
        """Call this via deferToThread, not in __init__."""
        self.device = Serial(self.port)

    def read_weight(self):
        """Returns a Deferred — safe to call from the reactor thread."""
 #       return threads.deferToThread(self._blocking_read)

#    def _blocking_read(self):
        self.device.write(b'P\r\n')
        # time.sleep() is OK here because we're in a thread, not the reactor
        #import time; time.sleep(0.1)
        #read = self.device.readline().decode().strip()
        #parts = read.split()
        try:
            read = self.device.readline().decode().strip()
            parts = read.split()
            return float(parts[1])
        except (ValueError, IndexError):
            return -99

    #def __init__(self, port="/dev/ADAM"):
        """
            Establish Serial communication.
        """
     #   self.port = port
      #  self.device = Serial(self.port)
        #print(self.device)

        # was it found?
       # if self.device is None:
        #    return None

        # use the first/default configuration
        #try:
         #   device.set_configuration()
        #except Exception:
         #   return None

        #return self.device


#    def read_weight(self):
        """
            Sends command to read weight from scale interface.
        """
 #       self.device.write(b'P\r\n')
        #time.sleep(0.1)
  #      read = self.device.readline().decode().strip()
   #     value = float(read.split()[1])
    #    unit = read.split()[2]
        #print(value, unit)
     #   try:
      #      return value
       # except ValueError:
        #    print(value)
         #   return(-99)
