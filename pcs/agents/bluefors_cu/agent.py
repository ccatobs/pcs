# Bluefors TC agent
import time
import argparse
import threading
import txaio
import os

from contextlib import contextmanager

from ocs import ocs_agent, site_config
from ocs.ocs_twisted import Pacemaker, TimeoutLock
from twisted.internet import reactor

from pcs.drivers.bluefors_cu import BFCU

class YieldingLock:
    """
    Borrowed from SOCS Lakeshore372 Agent on 11/9/2023 - could just import
    it but want to ensure it doesn't get changed in a future SOCS release
    without us knowing it.
    
    A lock protected by a lock.  This braided arrangement guarantees
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

class Bluefors_CU_Agent:
    """Agent to connect to a single Bluefors Temperature Controller device.
    
    """
    
    def __init__(self, agent, ip, key):

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

        self.ip = ip
        self.api_key = key
        self.module = None
        self.log = agent.log
        self.initialized = False
        self.take_data = False

        self.agent = agent
        # Registers temperature feeds
        agg_params = {
            'frame_length': 10 * 60  # [sec]
        }
        self.agent.register_feed('pressures',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)
        self.agent.register_feed('flow',
                                  record=True,
                                  agg_params=agg_params,
                                  buffer_time=1)
        

    @ocs_agent.param('auto_acquire', default=False, type=bool)
    @ocs_agent.param('acq_params', type=dict, default=None)
    def init_bfcu(self, session, params=None):
        """init_bfcu(auto_acquire=False, acq_params=None)
        
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
            self.log.info("BF CU already initialized. Returning...")
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
                self.module = BFCU(self.ip, self.api_key)
            except ConnectionError:
                self.log.error("Could not connect to the BF CU. Exiting.")
                reactor.callFromThread(reactor.stop)
                return False, 'BF CU initialization failed'
            except Exception as e:
                self.log.error(f"Unhandled exception encountered: {e}")
                reactor.callFromThread(reactor.stop)
                return False, 'BF CU initialization failed'

            print("Initialized BF CU module: {!s}".format(self.module))
            session.add_message("BF CU initilized")

            
            
            self.initialized = True

        # Start data acquisition if requested
        if params['auto_acquire']:
            self.agent.start('acq')


        return True, 'BF CU initialized.'
        
    @ocs_agent.param('_')
    def acq(self, session, params=None):
   
        pm = Pacemaker(2, quantize=True) # OCS pacemaker set to check for new data at 2 Hz

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
                pm.sleep()

                # Relinquish sampling lock occasionally.
                if time.time() - last_release > 1.:
                    last_release = time.time()
                    if not self._lock.release_and_acquire(timeout=10):
                        self.log.warn(f"Failed to re-acquire sampling lock, "
                                      f"currently held by {self._lock.job}.")
                        continue
                
                for i in range(1,7):
                    pressure, timestamp = self.module.get_pressure(i)
                    channel_str = 'p' + str(i)
                    data = {
                        'timestamp': timestamp,
                        'block_name': channel_str,
                        'data': {}
                    }
                    data['data'][channel_str] = pressure
                    session.app.publish_to_feed('pressures', data)
                    self.log.debug("{data}", data=session.data)
                    field_dict = {channel_str: {"pressure": pressure,
                                                "timestamp": timestamp}}
                    session.data['fields'].update(field_dict)
                    time.sleep(2)
              
                  
                flow, timestamp = self.module.get_flow()
                channel_str = 'flow_rate'
                data = {
                   'timestamp': timestamp,
                    'block_name': channel_str,
                    'data': {}
                    }
                data['data'][channel_str] = flow
                session.app.publish_to_feed('flow', data)
                self.log.debug("{data}", data=session.data)
                    
                # For session.data
                field_dict = {channel_str: {"value": flow,
                                                "timestamp": timestamp}}
                session.data['fields'].update(field_dict)                
         
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
            
def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--ip-address')
    pgroup.add_argument('--key')
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
    args = site_config.parse_args(agent_class='Bluefors_CU_Agent',
                                  parser=parser,
                                  args=args)

   # Automatically acquire data if requested (default)
    init_params = False
    if args.auto_acquire:
        init_params = {'auto_acquire': True}

    # Interpret options in the context of site_config.
    #print('I am in charge of device with serial number: %s' % args.serial_number)

    agent, runner = ocs_agent.init_site_agent(args)

    bfcu_agent = Bluefors_CU_Agent(agent, args.ip_address, args.key)

    agent.register_task('init_bfcu', bfcu_agent.init_bfcu,
                        startup=init_params)
                        
    agent.register_process('acq', bfcu_agent.acq, bfcu_agent._stop_acq)
    # And many more to come...

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()        
