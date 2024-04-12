# Bluefors TC agent

import argparse
import threading
from contextlib import contextmanager

from ocs import ocs_agent, site_config
from ocs.ocs_twisted import Pacemaker, TimeoutLock
from twisted.internet import reactor

from pcs.drivers.bluefors_tc import BFTC

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


class Bluefors_TC_Agent:
    """Agent to connect to a single Bluefors Temperature Controller device.
    
    """
    
    def __init__(self, agent, name, ip):

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
        self.ip = ip
        self.module = None
        self.thermometers = []

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
    @ocs_agent.param('acq_params', type=dict, default=None)
    def init_bftc(self, session, params=None):
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
            self.log.info("BF TC already initialized. Returning...")
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
                self.module = BFTC(self.ip)
            except ConnectionError:
                self.log.error("Could not connect to the BF TC. Exiting.")
                reactor.callFromThread(reactor.stop)
                return False, 'BF TC initialization failed'
            except Exception as e:
                self.log.error(f"Unhandled exception encountered: {e}")
                reactor.callFromThread(reactor.stop)
                return False, 'BF TC initialization failed'

            print("Initialized BF TC module: {!s}".format(self.module))
            session.add_message("BF TC initilized with ID: %s" % self.module.id)

            self.thermometers = [channel.name for channel in self.module.channels]
            
            self.initialized = True

        # Start data acquisition if requested
        if params.get('auto_acquire', False):
            self.agent.start('acq', params.get('acq_params', None))

        return True, 'BF TC initialized.'
    
    @ocs_agent.param('_')
    def acq(self, session, params=None):
        """acq()

        **Process** - Acquire data from the BF TC. Set to check for new data at
                      2 Hz - this needs to be adjusted if wait_time+meas_time
                      in the BF TC is shorter than a second (it is much longer
                      than 1 sec by default).

        Notes:
            The most recent data collected is stored in session data in the
            structure::

                >>> response.session['data']
                {"fields":
                    {"Channel_05": {"T": 293.644, "R": 33.752, "timestamp": 1601924482.722671},
                     "Channel_06": {"T": 0, "R": 1022.44, "timestamp": 1601924499.5258765},
                     "Channel_08": {"T": 0, "R": 1026.98, "timestamp": 1601924494.8172355},
                     "Channel_01": {"T": 293.41, "R": 108.093, "timestamp": 1601924450.9315426},
                     "Channel_02": {"T": 293.701, "R": 30.7398, "timestamp": 1601924466.6130798},
                    }
                }

        """
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

                current_measurement = self.module.get_latest_measurement()
                current_timestamp = current_measurement['timestamp']
                current_ch_num = current_measurement['channel_nr']
                channel_str = f'Channel_{current_ch_num:02}'

                # The BFTC reports a timestamp of the most recent measurement
                # when we query it - we only save the new data point when the
                # timestamp changes.
                if previous_timestamp != current_timestamp:
                # Potential upgrade: implement checks of channel wait_time
                # and meas_time to ensure we aren't missing data
                    
                    # Record most recent data
                    # Temperature data returns None if no curve or out of range
                    # Will set to zero to match LS372 behavior for now
                    temp_reading = current_measurement['temperature'] # in K
                    if temp_reading == None:
                        temp_reading = 0.0
                    res_reading = current_measurement['resistance'] # in Ohms
                        
                    # Update timestamp
                    previous_timestamp = current_timestamp
                        
                    current_time = time.time() # Using time.time() to be consistent with other agents
                    data = {
                        'timestamp': current_time,
                        'block_name': channel_str,
                        'data': {}
                    }
                    
                    # For data feed
                    data['data'][channel_str + '_T'] = temp_reading
                    data['data'][channel_str + '_R'] = res_reading
                    session.app.publish_to_feed('temperatures', data)
                    self.log.debug("{data}", data=session.data)
                
                    # For session.data
                    field_dict = {channel_str: {"T": temp_reading,
                                                "R": res_reading,
                                                "timestamp": current_time}}
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
    pgroup.add_argument('--serial-number')
    pgroup.add_argument('--mode', type=str, default='acq',
                        choices=['idle', 'init', 'acq'],
                        help="Starting action for the Agent.")

    return parser


def main(args=None):
    # For logging
    txaio.use_twisted()
    txaio.make_logger()

    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='BFTCAgent',
                                  parser=parser,
                                  args=args)

    # Automatically acquire data if requested (default)
    init_params = False
    if args.mode == 'init':
        init_params = {'auto_acquire': False,
                       'acq_params': {}}
    elif args.mode == 'acq':
        init_params = {'auto_acquire': True,
                       'acq_params': {}}

    # Interpret options in the context of site_config.
    print('I am in charge of device with serial number: %s' % args.serial_number)

    agent, runner = ocs_agent.init_site_agent(args)

    bftc_agent = Bluefors_TC_Agent(agent, args.serial_number, args.ip_address)

    agent.register_task('init_bftc', bftc_agent.bftc,
                        startup=init_params)
    agent.register_process('acq', bftc_agent.acq, bftc_agent._stop_acq)
    # And many more to come...

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()
    
