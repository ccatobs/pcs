from serial import Serial, EIGHTBITS, STOPBITS_ONE, PARITY_NONE
import serial
import time
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock
import argparse

class Teledyne:
    '''
    Class to control and retrieve data form the teledyne HVG-2020B

    Args:
        port (fixed for this device)

    Attributes:
        read_pressure reads current pressure from gauge
        close closes the connection between computer and arduino

    '''
    def __init__(self, port, baud = 19200, timeout = 0.1):
        
        self.baud = baud
        self.timeout = timeout
        self.port = port
        self.connection = Serial(self.port, baudrate=self.baud, bytesize=EIGHTBITS, parity=PARITY_NONE, stopbits=STOPBITS_ONE, timeout=timeout,xonxoff=False, rtscts=False)
        '''
        try:
            self.port = port
            self.connection = Serial(self.port, baudrate=self.baud, bytesize=EIGHTBITS, parity=PARITY_NONE, stopbits=STOPBITS_ONE, timeout=None,xonxoff=False, rtscts=False)
        
        except OSError:
            print(f'port {port} does not have teledyne connected. Procceding to scan all ports for teledyne device')
            
            ports = serial.tools.list_ports.comports()
            for port in ports:
                try:
                    self.connection = Serial(port= port.device, baudrate=19200, bytesize=EIGHTBITS, parity=PARITY_NONE, stopbits=STOPBITS_ONE, timeout=1,xonxoff=False, rtscts=False)
                    self.port = port.device
                except serial.SerialException:
                    continue

                self.connection.write('s1\r\n'.encode('utf-8'))
                time.sleep(0.1)
                
                lines = self.connection.readline()
                if lines.replace(b'\r>',b'').decode() == 'HVG-2020 I/O V2.0':
                    print(f'teledyne found on port {port}')
                    return
        '''
            
        
    def read_pressure(self):
        """
        Send command to pressure gauge and returns a float with units of mbar 
        """
        self.connection.write('p\r\n'.encode("utf-8"))
        time.sleep(0.1)
        read = self.connection.readline().replace(b'\r>',b'').decode()
        try:
            return float(read)
        except ValueError:
            print(read)
            return(-99)
        '''There are abnormal data sometimes, therefore this is to make sure the data is normal else will return -99'''  
    
    def check_connection(self):

        if not self.connection.is_open:
            try:
                self.connection.open()
            except IOError as err:
                print(err)
                return False

        for i in range(3):
            print(f'Connection Check {i}')
            try:
                self.connection.write('s1\r\n'.encode('utf-8'))

                result = self.connection.readline().replace(b'\r>',b'').decode()
                print(f'recieved \"{result}\"')
                if (result[:8] == 'HVG-2020'):
                    if (i > 0): print(f'connection check passed on {i}')
                    return True

            except IOError as err:
                
                print(err)
                if self.connection.is_open:
                    print('port open but read write connection error')
                    if i < 2: continue

                    self.connection.close()
                    print('read write error 3 times, closing port for now')
                    return True
                else:
                    print('connection lost')
                    return False      
        return False    
    
    def close(self):
        """
        Closes connection with Teledyne Pressure gauge.
        """
        self.connection.close()


class Teledyne_Agent:

    def __init__(self, agent, port, f_sample=2.5):
        self.active = True
        self.agent: ocs_agent.OCSAgent = agent
        self.log = agent.log
        self.lock = TimeoutLock()
        self.port = port
        self.f_sample = f_sample
        self.take_data = False
        self.gauge = Teledyne(port)
        agg_params = {'frame_length': 60, }
        self.agent.register_feed('pressure',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)
    
    #Enables client to acquire pressure data from Teledyne pressure gauge
    @ocs_agent.param('sampling_frequency', type=float, default = 2.5)
    @ocs_agent.param('test_mode', type = bool, default = False)
    def acq(self, session, params=None):
        #Determining how many times per second to sample data, defaults to 2.5 times per second
        if params is None:
            params = {}
        f_sample = params['sampling_frequency']
        if f_sample is None:
            f_sample = self.f_sample

        sleep_time = 1. / f_sample - 0.01
        
        #Ensures that multiple clients do not try to use function at same time
        with self.lock.acquire_timeout(timeout=0, job='init') as acquired:
            if not acquired:
                self.log.warn("Could not start init because {} is already running".format(self.lock.job))
                return False, "Could not acquire lock."

            session.set_status('running')

            self.take_data = True
            
            session.data = {'fields': {}}
           
            
            x = self.gauge.check_connection()
            if not x:
                print("Could not connect with pressure gauge. Check that proper port name of pressure gauge was given.")
                
                return False, 'ACQ not properly done'
            print("Looking good!")
                
            #Creates data object, sampling pressure and related timestamp that can be used for automation script and Grafana display
            while self.take_data:
                current_time = time.time()
                data = {
                    'timestamp': current_time,
                    'block_name':  'pressure',
                    'data': {}
                }

                try:
                	pressure_line = self.gauge.read_pressure()
                except IOError:
                    if self.gauge.check_connection():
                        print('read write io error, try again later')
                        continue
                    else:
                        return False, 'Connection Lost'

                data['data']['pressure'] = pressure_line
                
                field_dict = {'pressure': pressure_line}
                session.data['fields'].update(field_dict)
                
                self.agent.publish_to_feed('pressure', data)
                
                session.data['fields'].update({'timestamp': current_time})
                time.sleep(sleep_time)
                
                #print('data taken successfully')
                #print(pressure_line)
                
                if params['test_mode']:
                    break
            
            self.agent.feeds['pressure'].flush_buffer()
        return True, 'Acquisition exited cleanly'


    def stop_acq(self, session, params=None):
        if self.take_data:
            self.take_data = False
            self.gauge.close()
            print(f'port is now {str(self.gauge.connection.is_open)}')
            return True, 'Requested to stop taking data.'
        else:
            return False, 'Acq is not currently running.'

    
def make_parser(parser=None):
    """
    Makes an understandable accumulation of arguments for agent with site_config
    """
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port', type=str, help="Path to USB for the Teledyne Pressure Gauge")
    pgroup.add_argument('--baud', type=int, default =19200)
    pgroup.add_argument('--sampling_frequency', type=float, help='Sampling frequency for data acquisition', default = 2.5)
    pgroup.add_argument("--mode", type=str, default='acq', choices=['acq', 'test'])

    return parser
    
def main(args = None):
        parser = make_parser()
        args = site_config.parse_args(agent_class='TeledyneAgent',
                                      parser=parser,
                                      args=args)
        
        init_params = True
        if args.mode == 'test':
            init_params = {'test_mode': True}
        
        agent, runner = ocs_agent.init_site_agent(args)
        teledyne_agent = Teledyne_Agent(agent, args.port, args.sampling_frequency)
        agent.register_process('acq', teledyne_agent.acq,
                             teledyne_agent.stop_acq,
                             startup= init_params)
        agent.register_task('close', teledyne_agent.stop_acq)
        runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()
