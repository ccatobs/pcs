import serial
import txaio
import io

from typing import Callable, Literal
from functools import wraps
txaio.use_twisted()

def check_state(method: Callable):
    @wraps(method)
    def _wrapper(self, *args, **kwargs):
        if self.ser.is_open:
            return method(self, *args, **kwargs)
        else:
            self.log.error('No serial connection currently open.')
            return None
    return _wrapper

class Lakeshore460:
    def __init__(self, port, baudrate = 9600, timeout = 60):
        self.log = txaio.make_logger()
        self.ser, self.port, self.id = None, port, None
        self.open(port, baudrate, timeout)

    # =============== #
    # Open/Close Port #
    # =============== # 

    def open(self, port = None, baudrate=9600, timeout=60):
        if self.ser is None or not self.ser.is_open:
            try:
                port = port if port is not None else self.port
                self.ser = serial.Serial(
                    port,
                    baudrate=baudrate,
                    bytesize=serial.SEVENBITS,
                    parity=serial.PARITY_ODD,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=timeout,
                    xonxoff=False,
                    rtscts=False,
                    write_timeout=None,
                    dsrdtr=False,
                    inter_byte_timeout=None
                )
                self.id = self.get_device_id()
                if not self.id: raise TimeoutError
                self.log.info(f"Established communication with device {' '.join(self.id.split(','))} over port {port}.")
            except Exception as e:
                self.log.error(f'Failed to open connection to port {port} with Exception {e}.')
        else:
            self.log.error(f'Serial connection already open. Please close before opening a new connection.')

    @check_state
    def close(self):
        self.ser.flush()
        self.ser.close()
    
    # ================== #
    # General Read/Write #
    # ================== #

    @check_state
    def write(self, msg):
        msg += "\r\n"
        num_chars = self.ser.write(msg.encode('utf-8'))
        if num_chars == len(msg):
            self.log.debug(f'Successfully wrote message {msg}.')
        else:
            self.log.error(f'Failed to write message. {num_chars} written.')
        return num_chars

    @check_state
    def read(self):
        return self.ser.readline().decode('utf-8').strip()

    @check_state
    def read_write(self, msg):
        num_chars = self.write(msg)
        if num_chars > 0:
            output = self.read()
        return output
    
    def channel_read_write(self, msg, channel: Literal['X', 'Y', 'Z']):
        # Verify valid channel specified
        if not channel in ['X', 'Y', 'Z']:
            self.log.error("Invalid channel specified, must be one of 'X', 'Y', 'Z'.")
            return None

        # Set channel
        self.read_write(f'CHNL {channel}')
        set_channel = self.read_write('CHNL?')
        
        # Check that channel was successfully set
        if not set_channel == channel:
            self.log.error(f"Failed to set channel to {channel}.")
            return None

        return self.read_write(msg)

    # ===================== #
    # Lakeshore460 Commands #
    # ===================== #

    def get_device_id(self):
        return self.read_write('*IDN?')

    def fast_mode(self, state: bool):
        state = 1 if state else 0
        self.read_write(f"FAST {state}")
        return self.read_write("FAST?")

    def range(self, state: Literal[0, 1, 2, 3], channel: Literal['X', 'Y', 'Z']):
        self.channel_read_write(f"RANGE {state}", channel)
        return self.channel_read_write("RANGE?", channel)

    def autorange(self, state: bool, channel: Literal['X', 'Y', 'Z']):
        state = 1 if state else 0
        self.channel_read_write(f"AUTO {state}", channel)
        return self.channel_read_write('AUTO?', channel)

    def field(self, channel: Literal['X', 'Y', 'Z']):
        field = self.channel_read_write('FIELD?', channel=channel)
        mult = self.channel_read_write('FIELDM?', channel=channel)
        unit = self.channel_read_write('UNIT?', channel=channel)

        mult = float(mult) if mult else 1

        return float(field)*mult, unit

    def zero(self, channel: Literal['X', 'Y', 'Z']):
        return self.channel_read_write('ZCAL', channel=channel)