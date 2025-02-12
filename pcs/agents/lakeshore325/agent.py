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
        
        self.agent.register_feed('resistances',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)
        
    
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

            session.set_status('running')
            self.log.info("Starting data acquisition for {}".format(self.agent.agent_address))
            previous_timestamp = None
            last_release = time.time()

            session.data = {"fields": {}}

            self.take_data = True
            while self.take_data:
   
                # Relinquish sampling lock occasionally.
                if time.time() - last_release > 1.:
                    last_release = time.time()
                    if not self._lock.release_and_acquire(timeout=10):
                        self.log.warn(f"Failed to re-acquire sampling lock, "
                                      f"currently held by {self._lock.job}.")
                        continue
                
                
                res_reading = self.module.channel_A.get_resistance() 
                current_time_A = time.time()
                channel_str = 'Channel_A'
                
                # Setup feed dictionary
                data = {
                    'timestamp': current_time_A,
                    'block_name': channel_str,
                    'data': {}
                }

                data['data'][channel_str + '_R'] = res_reading
                    
                session.app.publish_to_feed('resistances', data)
                self.log.debug("{data}", data=session.data)
                
             
                time.sleep(.1)
                
                res_reading = self.module.channel_B.get_resistance()
                current_time_B = time.time()
                channel_str = 'Channel_B'
                
                # Setup feed dictionary
                data = {
                    'timestamp': current_time_B,
                    'block_name': channel_str,
                    'data': {}
                }

                data['data'][channel_str + '_R'] = res_reading
                    
                session.app.publish_to_feed('resistances', data)
                self.log.debug("{data}", data=session.data)
                
                
                time.sleep(10)
                
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
                        
    @ocs_agent.param('value',type=str)    
    def set_heater_units(self, session, params=None):
        self.module.heater1.set_units(params['value'])
        return True, 'Heater 1 units set'
        
    @ocs_agent.param('_')    
    def get_heater_units(self, session, params=None):
        resp = self.module.heater1.get_units()
        return True, resp
    
    #paul added:
    @ocs_agent.param('_')    
    def get_one_measurement_A(self, session, params=None):
    
        session.data = {"fields": {}}
        res_reading = float(self.module.channel_A.get_resistance())
        current_time_A = time.time()
        channel_str = 'Channel_A'
                
        # Setup feed dictionary
        data = {'timestamp': current_time_A,
                'block_name': channel_str,
                'data': {}
                }

        data['data'][channel_str + '_R'] = res_reading
                    
        session.app.publish_to_feed('resistances', data)
        self.log.debug("{data}", data=session.data)
                
        # For session.data
        field_dict = {channel_str: {"R": res_reading,
                                    "timestamp": current_time_A}}
        session.data['fields'].update(field_dict)
               
        return True, res_reading
   
    #paul added:
    @ocs_agent.param('_')    
    def get_one_measurement_B(self, session, params=None):
    
        session.data = {"fields": {}}
        res_reading = float(self.module.channel_B.get_resistance())
        current_time_B = time.time()
        channel_str = 'Channel_B'
                
        # Setup feed dictionary
        data = {'timestamp': current_time_B,
                'block_name': channel_str,
                'data': {}
                }

        data['data'][channel_str + '_R'] = res_reading
                    
        session.app.publish_to_feed('resistances', data)
        self.log.debug("{data}", data=session.data)
                
        # For session.data
        field_dict = {channel_str: {"R": res_reading,
                                    "timestamp": current_time_B}}
        session.data['fields'].update(field_dict)
               
        return True, res_reading
   


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

    

    # Interpret options in the context of site_config.
    print('I am in charge of device with serial number: %s' % args.serial_number)

    agent, runner = ocs_agent.init_site_agent(args)

    ls325_agent = LS325_Agent(agent, args.serial_number, args.port)
    agent.register_process('acq', ls325_agent.acq, ls325_agent._stop_acq)
    agent.register_task('init_ls325', ls325_agent.init_ls325)
    agent.register_task('get_heater_units', ls325_agent.get_heater_units)
    agent.register_task('set_heater_units', ls325_agent.set_heater_units)
    #register task with get_one_measurement
    agent.register_task('get_one_measurement_A', ls325_agent.get_one_measurement_A)
    agent.register_task('get_one_measurement_B', ls325_agent.get_one_measurement_B)
    # And many more to come...

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()
    
