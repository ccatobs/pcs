import argparse
import time
import os

import txaio
from ocs.ocs_twisted import TimeoutLock
from ocs import ocs_agent, site_config

from pcs.drivers.bias_crate import BiasCrate

class PrimecamBiasAgent():
    def __init__(self, agent, 
                       host = None, 
                       port = None, 
                       pub_chan = None, 
                       sub_chan = None,
                       timeout = None,
                       max_current = None,
                       max_voltage = None):
        self.agent = agent
        self.log = agent.log
        self.lock = TimeoutLock()

        self.host, self.port = host, port
        self.pub_chan, self.sub_chan = pub_chan, sub_chan
        self.timeout = timeout
        self.max_current, self.max_voltage = max_current, max_voltage

        self.agent.register_feed('bias_crate_output',
                                 record=True,
                                 agg_params={'frame_length':10*60},
                                 buffer_time=5)

    @ocs_agent.param('auto_acquire', default=False, type=bool)
    @ocs_agent.param('poll_int', type=float, default=10)
    @ocs_agent.param('lock_int', type=float, default=0.1)
    def init_crate(self, session, params):
        with self.lock.acquire_timeout(timeout=10, job='init_crate') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            self.bias_crate = BiasCrate(self.host, self.port, self.pub_chan, self.sub_chan, timeout = self.timeout)
            self.bias_cards = self.bias_crate.get_bias_cards()['cards']
            self.log.info(f'Created Prime-Cam bias crate control object. Available bias cards are: {self.bias_cards}')

        if params.pop('auto_acquire'):
            self.agent.start('monitor_channel', params)
        return True, f'Active Bias Cards: {self.bias_cards}'
    
    # Bias crate monitoring
    # =====================
    @ocs_agent.param('poll_int', type=float, default=10)
    @ocs_agent.param('lock_int', type=float, default=0.1)
    @ocs_agent.param('com_to', default=None)
    def monitor_channel(self, session, params):
        """monitor_channel(wait=1, com_to=['1.1']

        **Process** - Continuously monitor bias crate output current and voltage of the specified channel(s)

        Parameters:
            wait (float, optional): Time to wait between measurements [seconds].
        """
        with self.lock.acquire_timeout(timeout=10, job='monitor_channel') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): 
                com_tos = [com_tos]
            elif com_tos is None:
                com_tos = [f'{card}.{i+1}' for card in self.bias_cards for i in range(8)]
            
            last_poll = time.time()
            last_release = last_poll
            poll_int, lock_int = params['poll_int'], params['lock_int']
            self.monitor = True
            while self.monitor:
                if time.time() - last_poll > poll_int: 
                    last_poll = time.time()

                    resps = {}
                    for com_to in com_tos:
                        card, channel = map(int, com_to.split('.'))
                        resp = self.bias_crate.get_status(card, channel)
                        if resp['status'] == 'success':
                            status = resp
                            # Automatically disable output if current or voltage exceed max values specified
                            if status['current'] > self.max_current or status['vbus'] > self.max_voltage:
                                self.agent.start('disable_output', {'com_to': com_to})
                                self.log.warn(f'Disabling output of Card {card} Channel {channel} since it exceeds specified voltage and/or current limit!')
                        else:
                            self.log.error(f"Failed to monitor status for Card {card} Channel {channel} with Exception: {resp['msg']}")
                            status = {'card': card, 'channel': channel, 'vbus': None, 'vshunt': None, 'current': None, 'outputEnabled': None, 'wiper': None}
                        resps[com_to] = status

                        # Convert outputEnabled boolean to integer for easier visualization in Grafana
                        if (curr_output := status['outputEnabled']) is not None: status['outputEnabled'] = int(curr_output)
                        status = {f'{k}_{card}_{channel}': v for k, v in status.items()}
                        data = {'timestamp': time.time(),
                                'block_name': f'channel_{card}_{channel}', 
                                'data': status}
                        self.agent.publish_to_feed('bias_crate_output', data)

                    data = {'timestamp': time.time(),
                            'block_name': 'bias_crate', 
                            'data': resps}
                    session.data = data

                if time.time() - last_release > lock_int:
                    last_release = time.time()
                    if not self.lock.release_and_acquire(timeout=30*60):
                        self.log.error(f'Could not re-acquire lock; now held by {self.lock.job}.')
                        return False, 'Could not re-acquire lock.'
                time.sleep(lock_int)
        return True, "Finished monitoring bias channels"

    def stop_monitoring(self, session, params):
        self.monitor = False
        return True, "Stopped bias crate monitoring."

    # Bias Crate commands
    # ===================
    def get_bias_cards(self, session, params):
        '''get_bias_cards()

        **Task** - Get the available bias cards installed in the Prime-Cam bias crate.
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='get_bias_cards') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            resp = self.bias_crate.get_bias_cards()
            if (success := resp['status'] == 'success'):
                self.bias_cards = resp['cards']
                data = {'timestamp': time.time(),
                        'block_name': 'bias_crate', 
                        'data': {'cards': self.bias_cards}}
                session.data = data
            else:
                self.log.error(f"Failed to get bias cards with Exception: {resp['msg']}")
        return success, f'Active Bias Cards: {self.bias_cards}'
    
    @ocs_agent.param('com_to', default=None)
    def enable_output(self, session, params):
        '''enable_output(com_to)

        **Task** - Enable output of the specified bias card channel
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='enable_output') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]

            resps = {}
            for com_to in com_tos:
                card, channel = map(int, com_to.split('.'))
                resp = self.bias_crate.enable_output(card, channel)
                if (success := resp['status'] == 'success'):
                    output_enabled = resp['outputEnabled']
                else:
                    self.log.error(f"Failed to enable output for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    output_enabled = None
                resps[com_to] = {'outputEnabled': output_enabled}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Enabled output for channels: {com_tos}'
    
    @ocs_agent.param('com_to', default=None)
    def disable_output(self, session, params):
        '''disable_output(com_to)

        **Task** - Disable output of the specified bias card channel
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='disable_output') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]

            resps = {}
            for com_to in com_tos:
                card, channel = map(int, com_to.split('.'))
                resp = self.bias_crate.disable_output(card, channel)
                if (success := resp['status'] == 'success'):
                    output_enabled = resp['outputEnabled']
                else:
                    self.log.error(f"Failed to disable output for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    output_enabled = None
                resps[com_to] = {'outputEnabled': output_enabled}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Disabled output for channels: {com_tos}'

    @ocs_agent.param('com_to', default=None)
    def get_status(self, session, params):
        '''get_status(com_to)

        **Task** - Get status of the specified bias card channel
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='get_status') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]

            resps = {}
            for com_to in com_tos:
                card, channel = map(int, com_to.split('.'))
                resp = self.bias_crate.get_status(card, channel)
                if (success := resp['status'] == 'success'):
                    status = resp
                else:
                    self.log.error(f"Failed to get status for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    status = {'card': card, 'channel': channel, 'vbus': None, 'vshunt': None, 'current': None, 'outputEnabled': None, 'wiper': None}
                resps[com_to] = status

            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Got status for channels: {com_tos}'
    
    @ocs_agent.param('com_to', default=None)
    @ocs_agent.param('voltage')
    def seek_voltage(self, session, params):
        '''seek_voltage(com_to, voltage)

        **Task** - Seek voltage for the specified bias card channel
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='seek_voltage') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]
            if not isinstance((voltages := params['voltage']), list): voltages = [voltages]*len(com_tos)

            if not len(voltages) == len(com_tos):
                err = 'Voltages specified do not match the number of channels specified.'
                self.log.error(err)
                return False, err

            resps = {}
            for com_to, voltage in zip(com_tos, voltages):
                card, channel = map(int, com_to.split('.'))
                if voltage > self.max_voltage:
                    self.log.warn(f'Cannot set voltage to {voltage} V as it exceeds the maximum voltage {self.max_voltage} V specified; setting to {self.max_voltage} V instead.')
                    voltage = self.max_voltage

                resp = self.bias_crate.seek_voltage(card, channel, voltage)
                if (success := resp['status'] == 'success'):
                    chan_voltage = resp['vbus']
                else:
                    self.log.error(f"Failed to seek voltage for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    chan_voltage = None
                resps[com_to] = {'vbus': chan_voltage}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Set voltage for channels: {com_tos}'

    @ocs_agent.param('com_to', default=None)
    @ocs_agent.param('current')
    def seek_current(self, session, params):
        '''seek_current(com_to, current)

        **Task** - Seek current(s) for the specified bias card channel(s)
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='seek_current') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]
            if not isinstance((currents := params['current']), list): currents = [currents]*len(com_tos)

            if not len(currents) == len(com_tos):
                err = 'Currents specified do not match the number of channels specified.'
                self.log.error(err)
                return False, err

            resps = {}
            for com_to, current in zip(com_tos, currents):
                card, channel = map(int, com_to.split('.'))
                if current > self.max_current:
                    self.log.warn(f'Cannot set current to {current} A as it exceeds the maximum current {self.max_current} A specified; setting to {self.max_current} A instead.')
                    current = self.max_current

                resp = self.bias_crate.seek_current(card, channel, current)
                if (success := resp['status'] == 'success'):
                    chan_current = resp['current']
                else:
                    self.log.error(f"Failed to seek current for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    chan_current = None
                resps[com_to] = {'current': chan_current}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Set current for channels: {com_tos}'
    
    @ocs_agent.param('com_to', default=None)
    def enable_testload(self, session, params):
        '''enable_testload(com_to)

        **Task** - Enable test load of the specified bias card channel
        
        '''
        with self.lock.acquire_timeout(timeout=10, job='enable_testload') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]

            resps = {}
            for com_to in com_tos:
                card, channel = map(int, com_to.split('.'))
                resp = self.bias_crate.enable_testload(card, channel)
                if (success := resp['status'] == 'success'):
                    chan_vshunt = resp['vshunt']
                else:
                    self.log.error(f"Failed to enable testload for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    chan_vshunt = None
                resps[com_to] = {'vshunt': chan_vshunt}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Enabled testload for channels: {com_tos}'

    @ocs_agent.param('com_to', default=None)
    def disable_testload(self, session, params):
        '''disable_testload(com_to)

        **Task** - Disable test load of the specified bias card channel
        
        '''
        card, channel = map(int, params['com_to'].split('.'))

        with self.lock.acquire_timeout(timeout=10, job='disable_testload') as acquired:
            if not acquired:
                err = f'Lock could not be acquired because it is held by {self.lock.job}.'
                self.log.error(err)
                return False, err

            if isinstance((com_tos := params['com_to']), str): com_tos = [com_tos]

            resps = {}
            for com_to in com_tos:
                card, channel = map(int, com_to.split('.'))
                resp = self.bias_crate.disable_testload(card, channel)
                if (success := resp['status'] == 'success'):
                    chan_vshunt = resp['vshunt']
                else:
                    self.log.error(f"Failed to disable testload for Card {card} Channel {channel} with Exception: {resp['msg']}")
                    chan_vshunt = None
                resps[com_to] = {'vshunt': chan_vshunt}
            data = {'timestamp': time.time(),
                    'block_name': 'bias_crate', 
                    'data': resps}
            session.data = data
        return success, f'Disabled testload for channels: {com_tos}'

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows Sphinx to automatically
    build documentation based on this function.

    """
    if parser is None: parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--redis-host', type=str, help='Host name of the Redis server communicating with the Prime-Cam bias crate.')
    pgroup.add_argument('--redis-port', type=int, help='Port that Redis server communicating with the Prime-Cam bias crate is running on.')
    pgroup.add_argument('--redis-pub-chan', type=str, help='Redis channel to publish commands to.')
    pgroup.add_argument('--redis-sub-chan', type=str, help='Redis channel to listen to for responses to commands.')
    pgroup.add_argument('--redis-timeout', type=float, help='Time in seconds to wait for a response from a command sent to the Prime-Cam bias crate.')
    pgroup.add_argument('--mode', type=str, default='acq', choices=['init', 'acq'])
    pgroup.add_argument('--poll-interval', type=float, default=10, help='Time in seconds to poll bias card channel status.')
    pgroup.add_argument('--lock-interval', type=float, default=0.1, help='Time in seconds to release channel monitor lock.')
    pgroup.add_argument('--max-current', type=float, default=0.01, help='Maximum current limit in Amperes.')
    pgroup.add_argument('--max-voltage', type=float, default=2, help='Maximum voltage limit in Volts.')
    return parser

def main(args=None):
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='PrimecamBiasAgent',
                                  parser=parser,
                                  args=args)             
    init_params = {'auto_acquire': args.mode == 'acq', 
                   'poll_int': args.poll_interval, 
                   'lock_int': args.lock_interval}
    agent, runner = ocs_agent.init_site_agent(args)

    b = PrimecamBiasAgent(agent, host = args.redis_host, 
                                 port = args.redis_port, 
                                 pub_chan = args.redis_pub_chan, 
                                 sub_chan = args.redis_sub_chan,
                                 timeout = args.redis_timeout,
                                 max_current = args.max_current,
                                 max_voltage = args.max_voltage,)

    agent.register_task('init_crate', b.init_crate, startup=init_params)
    agent.register_task('get_bias_cards', b.get_bias_cards)
    agent.register_task('enable_output', b.enable_output)
    agent.register_task('disable_output', b.disable_output)
    agent.register_task('get_status', b.get_status)
    agent.register_task('seek_voltage', b.seek_voltage)
    agent.register_task('seek_current', b.seek_current)
    agent.register_task('enable_testload', b.enable_testload)
    agent.register_task('disable_testload', b.disable_testload)

    agent.register_process('monitor_channel', b.monitor_channel, b.stop_monitoring)
    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()