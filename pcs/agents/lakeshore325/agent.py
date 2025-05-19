import argparse
import threading
import txaio
import os
import time
from contextlib import contextmanager
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import Pacemaker, TimeoutLock
from twisted.internet import reactor

from pcs.drivers.lakeshore325 import LS325

class YieldingLock:
    """A lock protected by a lock.  This braided arrangement guarantees
    that a thread waiting on the lock will get priority over a thread
    that has just released the lock and wants to reacquire it.

    The typical use case is a Process that wants to hold the lock as
    much as possible, but occasionally release the lock (without
    sleeping for long) so another thread can access a resource.  The
    method release_and_acquire() is provided to make this a one-liner.

    """

    def __init__(self, default_timeout=None):
        self.job = None
        self._next = threading.Lock()
        self._active = threading.Lock()
        self._default_timeout = default_timeout

    def acquire(self, timeout=None, job=None):
        if timeout is None:
            timeout = self._default_timeout
        if timeout is None or timeout == 0.:
            kw = {'blocking': False}
        else:
            kw = {'blocking': True, 'timeout': timeout}
        result = False
        if self._next.acquire(**kw):
            if self._active.acquire(**kw):
                self.job = job
                result = True
            self._next.release()
        return result

    def release(self):
        self.job = None
        return self._active.release()

    def release_and_acquire(self, timeout=None):
        job = self.job
        self.release()
        return self.acquire(timeout=timeout, job=job)

    @contextmanager
    def acquire_timeout(self, timeout=None, job='unnamed'):
        result = self.acquire(timeout=timeout, job=job)
        if result:
            try:
                yield result
            finally:
                self.release()
        else:
            yield result

class LS325_Agent:
    """Agent to connect to a single Bluefors Temperature Controller device.
    
    """
    
    def __init__(self, agent, name, port):
        
        # self._acq_proc_lock is held for the duration of the acq Process.
        # Tasks that require acq to not be running, at all, should use
        # this lock.
        self._acq_proc_lock = TimeoutLock()
        
        # self._lock is held by the acq Process only when accessing
        # the hardware but released occasionally so that (short) Tasks
        # may run.  Use a YieldingLock to guarantee that a waiting
        # Task gets activated preferentially, even if the acq thread
        # immediately tries to reacquire.
        self._lock = YieldingLock(default_timeout=5)
        
        self.name = name
        self.port = port
        self.module = None

        self.log = agent.log
        self.initialized = False
        self.take_data = False

        self.agent = agent
        
        # Registers temperature feeds
        agg_params = {
            'frame_length': 10 * 60  # [sec]
        }
        
        self.agent.register_feed('temperatures',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)
        
    @ocs_agent.param('auto_acquire', default=False, type=bool)
    def init_ls325(self, session, params=None):
        """init_bftc(auto_acquire=False, acq_params=None)
        
        **Task** - Perform first time setup of the communication to the BFTC.
        
        Parameters:
            auto_acquire (bool, optional): Default is False. Starts data
                acquisition after initialization if True.
            acq_params (dict, optional): Params to pass to acq process if
                auto_acquire is True.
        """

        if params is None:
            params = {}
        if self.initialized and not params.get('force', False):
            self.log.info("LS325 already initialized. Returning...")
            return True, "Already initialized"
            
        with self._lock.acquire_timeout(job='init') as acquired1, \
                self._acq_proc_lock.acquire_timeout(timeout=0., job='init') \
                as acquired2:
            if not acquired1:
                self.log.warn(f"Could not start init because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"  
            if not acquired2:
                self.log.warn(f"Could not start init because "
                              f"{self._acq_proc_lock.job} is already running")
                return False, "Could not acquire lock"
        session.set_status('running')
        
        try:
            self.module = LS325(self.port)
        except ConnectionError:
            self.log.error("Could not connect to the LS325. Exiting.")
            reactor.callFromThread(reactor.stop)
            return False, 'LS325 initialization failed'
        except Exception as e:
            self.log.error(f"Unhandled exception encountered: {e}")
            reactor.callFromThread(reactor.stop)
            return False, 'LS325 initialization failed'

        print("Initialized LS325 module: {!s}".format(self.module))
        session.add_message("LS325 initilized with ID: %s" % self.module.id)

        self.initialized = True

         # Start data acquisition if requested
        if params['auto_acquire']:
            self.agent.start('acq')

        return True, 'LS325 initialized.'
        
    @ocs_agent.param('_') 
    def acq(self, session, params=None):
    
        with self._acq_proc_lock.acquire_timeout(timeout=0, job='acq') \
                as acq_acquired, \
                self._lock.acquire_timeout(job='acq') as acquired:
            if not acq_acquired:
                self.log.warn(f"Could not start Process because "
                              f"{self._acq_proc_lock.job} is already running")
                return False, "Could not acquire lock"
            if not acquired:
                self.log.warn(f"Could not start Process because "
                              f"{self._lock.job} is holding the lock")
                return False, "Could not acquire lock"

            session.set_status('running')
            self.log.info("Starting data acquisition for {}".format(self.agent.agent_address))
            last_release = time.time()
            last_publish = 0
            session.data = {"fields": {}}

            self.take_data = True
            
            def read_and_publish(channel, label):
                try:
                    res = float(channel.get_resistance())
                    temp = float(channel.get_kelvin_reading())
                    timestamp = time.time()

                    data = {
                        'timestamp': timestamp,
                        'block_name': label,
                        'data': {
                            f'{label}_R': res,
                            f'{label}_T': temp
                        }
                    }
                    session.app.publish_to_feed('temperatures', data)
                    session.data['fields'][label] = {
                        'R': res, 'T': temp, 'timestamp': timestamp
                    }
                except Exception as e:
                    self.log.warn(f"Failed to read/publish from {label}: {e}")
                      
            while self.take_data:
            
                # Relinquish sampling lock occasionally.
                if time.time() - last_release > 1.:
                    last_release = time.time()
                    if not self._lock.release_and_acquire(timeout=10):
                        self.log.warn(f"Failed to re-acquire sampling lock, "
                                      f"currently held by {self._lock.job}.")
                        continue
                        
                if time.time() - last_publish >= 20:      
                    read_and_publish(self.module.channel_A, 'Channel_A')
                    time.sleep(0.1)
                    read_and_publish(self.module.channel_B, 'Channel_B')
                    last_publish = time.time()
                    
                time.sleep(0.5)
                    
        return True, 'Acquisition exited cleanly.'                 
    
    def _stop_acq(self, session, params=None):
        """
        Stops acq process.
        """
        if self.take_data:
            session.set_status('stopping')
            self.take_data = False
            return True, 'requested to stop taking data.'
        else:
            return False, 'acq is not currently running'
                        
        
    @ocs_agent.param('channel', type=str)
    def get_temperature(self, session, params):
        with self._lock.acquire_timeout(job='get_temperature') as acquired:
            if not acquired:
                self.log.warn(f"Could not start Task because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"
                
        if params['channel'].upper() == 'A':
    	    temp = self.module.channel_A.get_kelvin_reading()
    	    return True, f"Channel_A: {temp}"
        if params['channel'].upper() == 'B':
    	    temp = self.module.channel_B.get_kelvin_reading()
    	    return True, f"Channel_B: {temp}"
        else:
    	    return False, "Invalid channel selected"
    
    @ocs_agent.param('loop', type=int, check=lambda x: x in (1,2))	    
    @ocs_agent.param('temp', type=float, check=lambda x: x < 20)
    @ocs_agent.param('channel', type=str, check=lambda x: x.upper() in ('A','B'))
    def servo_to_temperature(self, session, params):
        """servo_to_temperature(temperature, channel=None)

        **Task** - Servo to a given temperature using a closed loop PID on a
        fixed channel. This will automatically disable autoscan if enabled.

        Parameters:
            temperature (float): Temperature to servo to in units of Kelvin.
            channel (int, optional): Channel to servo off of.

        """
        with self._lock.acquire_timeout(job='servo_to_temperature') as acquired:
            if not acquired:
                self.log.warn(f"Could not start Task because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"

            
            if params['loop'] == 1:
                loop_obj = self.module.loop_1
            if params['loop'] == 2:
                loop_obj = self.module.loop_2
                
            # Check we're in correct control mode for servo.    
            if loop_obj.get_mode().upper() != 'MANUAL PID':
                session.add_message('Changing control to Manual PID Loop mode for servo.')
                loop_obj.set_mode("Manual PID")
            else:
                session.add_message(f'Loop_{params["loop"]} is already set to Manual PID mode.')

            # Check to see if we passed an input channel, and if so change to it
            if loop_obj.get_control_input() != params['channel'].upper():
                session.add_message(f'Changing loop input channel to {params["channel"].upper()}')
                loop_obj.set_control_input(params["channel"])
            else:
                session.add_message(f'Loop_{params["loop"]} input channel is already {params["channel"].upper()}')

            # Check we're setup to take correct units.
            if loop_obj.get_units().upper() != 'KELVIN':
                session.add_message('Setting preferred units to Kelvin on heater control.')
                loop_obj.set_units('kelvin')

            loop_obj.set_setpoint(params["temp"])
            resp = loop_obj.get_setpoint()
        return True, f'Setpoint now set to {resp} K'  
 	
    @ocs_agent.param('loop', type=int, check=lambda x: x in (1,2))	    
    @ocs_agent.param('range', type=str, check=lambda x: x.upper() in ('ON', 'OFF', 'LOW', 'HIGH'))
    @ocs_agent.param('resistance', type=int, check=lambda x: (25,50))    
    def init_heater(self, session, params):
    
        with self._lock.acquire_timeout(job='init_heater') as acquired:
            if not acquired:
                self.log.warn(f"Could not start Task because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"
            
            
            if params['loop'] == 1:
               loop_obj = self.module.loop_1
            if params['loop'] == 2:
               loop_obj = self.module.loop_2
            
            if loop_obj.get_range().upper() != params['range'].upper():
               session.add_message(f'Changing heater range to {params["range"]}')
               loop_obj.set_range(params["range"])
            else:
                session.add_message(f'Heater range is already {params["range"]}')
             
            if (loop_obj.get_resistance()) != params['resistance']:
               session.add_message(f'Changing heater resistance to {params["resistance"]} ohms')
               loop_obj.set_resistance(params['resistance'])
            else:
                session.add_message(f'Heater resistance is already set to {params["resistance"]} ohms')
               
        return True, f'Loop_{params["loop"]} heater initialized succesfully'
        
    @ocs_agent.param('loop', type=int, check=lambda x: x in (1,2)) 
    @ocs_agent.param('p', type=float, check=lambda x: x <= 1000 and x >= 0)
    @ocs_agent.param('i', type=float, check=lambda x: x <= 1000 and x >= 1)
    @ocs_agent.param('d', type=float, check=lambda x: x <= 200 and x >= 1)
    def set_pid(self, session, params):
        with self._lock.acquire_timeout(job='set_pid') as acquired:
            if not acquired:
                self.log.warn(f"Could not start Task because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"
                
            if params['loop'] == 1:
               loop_obj = self.module.loop_1
            if params['loop'] == 2:
               loop_obj = self.module.loop_2
               
            loop_obj.set_pid(params["p"], params["i"], params["d"])
            resp = loop_obj.get_pid()

        return True, f'Set PID to {resp[0]}, {resp[1]}, {resp[2]}'
    
    @ocs_agent.param('loop', type=int, check=lambda x: x in (1,2))	    
    @ocs_agent.param('temp', type=float, check=lambda x: x < 20)
    def set_setpoint(self, session, params):
        with self._lock.acquire_timeout(job='set_setpoint') as acquired:
            if not acquired:
                self.log.warn(f"Could not start Task because "
                              f"{self._lock.job} is already running")
                return False, "Could not acquire lock"
                
            if params['loop'] == 1:
               loop_obj = self.module.loop_1
            if params['loop'] == 2:
               loop_obj = self.module.loop_2
               
            # Check we're setup to take correct units.
            if loop_obj.get_units().upper() != 'KELVIN':
                session.add_message('Setting preferred units to Kelvin on heater control.')
                loop_obj.set_units('kelvin')   
            loop_obj.set_setpoint(params['temp'])
            resp = loop_obj.get_setpoint()
            
        return True, f'Setpoint now set to {resp} K'

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port')
    pgroup.add_argument('--serial-number')
    pgroup.add_argument('--auto-acquire', type=bool, default=False,
                        help='Automatically start data acquisition on startup')
    return parser


def main(args=None):
    # For logging
    txaio.use_twisted()
    txaio.make_logger()

    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='LS325Agent',
                                  parser=parser,
                                  args=args)

    # Automatically acquire data if requested (default)
    init_params = False
    if args.auto_acquire:
        init_params = {'auto_acquire': True}

    # Interpret options in the context of site_config.
    print('I am in charge of device with serial number: %s' % args.serial_number)

    agent, runner = ocs_agent.init_site_agent(args)

    ls325_agent = LS325_Agent(agent, args.serial_number, args.port)
    agent.register_process('acq', ls325_agent.acq, ls325_agent._stop_acq)
    agent.register_task('init_ls325', ls325_agent.init_ls325, startup=init_params)
    agent.register_task('get_temperature', ls325_agent.get_temperature)
    agent.register_task('init_heater', ls325_agent.init_heater)
    agent.register_task('servo_to_temperature', ls325_agent.servo_to_temperature)
    agent.register_task('set_pid', ls325_agent.set_pid)
    agent.register_task('set_setpoint', ls325_agent.set_setpoint)

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()
    
