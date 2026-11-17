import argparse
import time
import os

import txaio
from ocs.ocs_twisted import TimeoutLock
from ocs import ocs_agent, site_config

import pcs.drivers.lakeshore460 as ls460

class Lakeshore460_Agent():
    def __init__(self, agent, port, baudrate, timeout):
        self.agent = agent
        self.lock = TimeoutLock()
        self.log = self.agent.log
        
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        # Register OCS feed to log magentic field
        self.agent.register_feed('ls460_field',
                            record=True,
                            agg_params={'frame_length':10*60},
                            buffer_time=5)

    @ocs_agent.param('auto_acquire', default=False, type=bool)
    @ocs_agent.param('poll_int', type=float, default=10)
    @ocs_agent.param('lock_int', type=float, default=0.1)
    @ocs_agent.param('channels', type=list, default=['X', 'Y', 'Z'])
    def init(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='init') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err
            self.ls = ls460.Lakeshore460(self.port, baudrate=self.baudrate, timeout=self.timeout)
        if params.pop('auto_acquire'):
            self.agent.start('monitor_output', params)
        return True, f"Communicating with Lakeshore 460: {self.ls.id}"
    
    # Lakeshore 460 monitoring
    # ========================
    @ocs_agent.param('poll_int', type=float, default=10, check=lambda x: x >= 1/18)
    @ocs_agent.param('lock_int', type=float, default=0.1)
    @ocs_agent.param('channels', type=list, default=['X', 'Y', 'Z'])
    def monitor_output(self, session, params):
        """monitor_output

        **Process** - Continuously monitor magnetic field measured by Lakeshore 460

        Parameters:
            poll_int (float, optional): Interval at which to monitor magnetic field [seconds]. Defaults to 10 s.
            lock_int (float, optional): Interval at which to release lock [seconds]. Defaults to 0.1 s.
        """
        with self.lock.acquire_timeout(timeout=10, job='monitor_output') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            last_poll = time.time()
            last_release = last_poll
            poll_int, lock_int, channels = params['poll_int'], params['lock_int'], params['channels']
            self.monitor = True
            while self.monitor:
                if time.time() - last_poll > poll_int: 
                    last_poll = time.time()

                    resps = {}
                    for channel in channels:
                        resp = self.ls.field(channel)
                        if not len(resp) == 2:
                            self.log.error(f"Failed to retrieve magnetic field for channel {channel}.")
                            resp = (None, '')
                        resp_dict = {'field': resp[0], 'unit': resp[1]}

                        resps[channel] = resp_dict
                        data = {'timestamp': time.time(),
                                'block_name': f'channel_{channel}', 
                                'data': {f'field_{channel}': resp_dict['field'], f'unit_{channel}': resp_dict['unit']}}
                        self.agent.publish_to_feed('ls460_field', data)

                    data = {'timestamp': time.time(),
                            'block_name': 'ls460',
                            'data': resps}
                    session.data = data

                if time.time() - last_release > lock_int:
                    last_release = time.time()
                    if not self.lock.release_and_acquire(timeout=5*60):
                        self.log.error(f'Could not re-acquire lock; now held by {self.lock.job}.')
                        return False, 'Could not re-acquire lock.'
                time.sleep(lock_int)
        return True, "Finished monitoring Lakeshore 460 output."

    def stop_monitoring(self, session, params):
        self.monitor = False
        return True, "Stopped Lakeshore 460 monitoring."

    @ocs_agent.param('range', type=int)
    @ocs_agent.param('channel', type=str)
    def range(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='range') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            chan_range, channel = params['range'], params['channel']
            if not channel in ['X', 'Y', 'Z']:
                self.log.error( f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'.")
                return False, f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'."
            elif not chan_range in [0, 1, 2, 3]:
                self.log.error( f"Invalid range: {chan_range}. Must be one of 0, 1, 2, 3.")
                return False, f"Invalid range: {chan_range}. Must be one of 0, 1, 2, 3."
            else:
                resp = self.ls.range(chan_range, channel)
                data = {'timestamp': time.time(),
                        'block_name': 'ls460',
                        'data': {channel: {'range': resp}}}
                return True, resp

    @ocs_agent.param('state', type=bool)
    @ocs_agent.param('channel', type=str)
    def autorange(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='autorange') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            state, channel = params['state'], params['channel']
            if not channel in ['X', 'Y', 'Z']:
                self.log.error( f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'.")
                return False, f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'."
            else:
                resp = self.ls.autorange(state, channel)
                data = {'timestamp': time.time(),
                        'block_name': 'ls460',
                        'data': {channel: {'autorange': resp}}}
                return True, resp
    
    @ocs_agent.param('state', type=bool)
    def fast_mode(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='fast_mode') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            state = params['state']
            resp = self.ls.fast_mode(state)
            data = {'timestamp': time.time(),
                    'block_name': 'ls460',
                    'data': {'fast_mode': resp}}
            return True, resp

    @ocs_agent.param('channel', type=str)
    def zero(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='zero') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            channel = params['channel']
            if not channel in ['X', 'Y', 'Z']:
                self.log.error( f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'.")
                return False, f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'."
            else:
                resp = self.ls.zero(channel)
                return True, resp

    @ocs_agent.param('channel', type=str)
    def field(self, session, params):
        with self.lock.acquire_timeout(timeout=5, job='field') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            channel = params['channel']
            if not channel in ['X', 'Y', 'Z']:
                self.log.error( f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'.")
                return False, f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'."
            else:
                resp = self.ls.field(channel)

                if not len(resp) == 2:
                    self.log.error(f"Failed to retrieve magnetic field for channel {channel}.")
                    return False, f"Failed to retrieve magnetic field for channel {channel}."

                data = {'timestamp': time.time(),
                        'block_name': 'ls460',
                        'data': {channel: {'field': resp[0], 
                                           'unit': resp[1]}}}
                return True, resp

    #=========================================#
    # Read/Write Serial Commands to Lakeshore #
    #=========================================#

    @ocs_agent.param('msg', type=str, default='')
    def read_write(self, session, params):
        """read_write()

        **Task** - Write and read serial command to Lakeshore 460

        """

        with self.lock.acquire_timeout(timeout=5, job='read_write') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."

            msg = params['msg']
            if not msg:
                return False, f"Invalid message: {msg}"
            else:
                resp = self.ls.read_write(msg)
                data = {'timestamp': time.time(),
                        'block_name': 'ls460',
                        'data': {'read': resp}}
                session.data = data
                return True, resp
    
    @ocs_agent.param('msg', type=str, default='')
    @ocs_agent.param('channel', type=str)
    def channel_read_write(self, session, params):
        """channel_read_write(msg)

        **Task** - Write and read serial command to a specific Lakeshore 460 probe channel ('X', 'Y', or 'Z')

        Parameters:
            msg (str): Serial command
            channel (Literal['X', 'Y', 'Z']): Lakeshore 460 channel. Must be one of 'X', 'Y', or 'Z'
        """
        with self.lock.acquire_timeout(timeout=5, job='channel_read_write') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, f"Lock could not be acquired because it is held by {self.lock.job}."
            
            msg, channel = params['msg'], params['channel']
            if not msg:
                return False, f"Invalid message: {msg}"
            elif not channel in ['X', 'Y', 'Z']:
                self.log.error( f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'.")
                return False, f"Invalid channel: {channel}. Must be one of 'X', 'Y', or 'Z'."
            else:
                resp = self.ls.channel_read_write(msg=msg, channel=channel)
                data = {'timestamp': time.time(),
                        'block_name': 'ls460',
                        'data': {channel: {'channel_read': resp}}}
                session.data = data
            return True, resp

#===========#
# Functions #
#===========#

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """

    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port', type=str, help='USB port connected to Lakeshore 460.')
    pgroup.add_argument('--baudrate', type=int, default=9600, help='Baudrate for serial data transfer.')
    pgroup.add_argument('--timeout', type=float, default=60, help='Timeout for serial read/write commands.')
    pgroup.add_argument('--poll-interval', type=float, default=5, help='Time in seconds to poll magnetic field status.')
    pgroup.add_argument('--lock-interval', type=float, default=0.1, help='Time in seconds to release output monitor lock.')
    pgroup.add_argument('--channels', nargs='+', type=str, default=['X', 'Y', 'Z'], help='Channels to monitor.')
    pgroup.add_argument('--mode', type=str, default='init', choices=['init', 'acq'])
    return parser

def main(args=None):
    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='Lakeshore460Agent',
                                  parser=parser,
                                  args=args)
                                  
    init_params = {'auto_acquire': args.mode == 'acq',
                   'poll_int': args.poll_interval, 
                   'lock_int': args.lock_interval,
                   'channels': args.channels}
                   
    agent, runner = ocs_agent.init_site_agent(args)

    ls = Lakeshore460_Agent(agent, args.port, baudrate=args.baudrate, timeout=args.timeout)

    agent.register_task('init', ls.init, startup=init_params)
    agent.register_task('range', ls.range)
    agent.register_task('autorange', ls.autorange)
    agent.register_task('fast_mode', ls.fast_mode)
    agent.register_task('zero', ls.zero)
    agent.register_task('field', ls.field)

    agent.register_task('read_write', ls.read_write)
    agent.register_task('channel_read_write', ls.channel_read_write)

    agent.register_process('monitor_output', ls.monitor_output, ls.stop_monitoring)

    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()
