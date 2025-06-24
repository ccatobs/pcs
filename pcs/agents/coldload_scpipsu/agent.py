import argparse
import time
import numpy as np
import os
from collections import deque

import txaio
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock
from socs.agents.scpi_psu.agent import ScpiPsuAgent


class ColdloadAgent_ScpiPsu(ScpiPsuAgent):
    def __init__(self, agent, ip_address, gpib_slot=None, port=None, ls240=None, channel=None, max_current=None):
        super().__init__(agent, ip_address, gpib_slot=gpib_slot, port=port) # Initialize ScipPsuAgent
        self.max_current = max_current

        self.channel = channel
        self.ls240_channel = ls240[1]
        try:
            self.ls240 = OCSClient(ls240[0]) # Create LS240 client for grabbing coldload temperature data
        except Exception as e:
            self.log.error(f'Could not connect to LS240 for temperature monitoring: {e}')
        
        self.temp_control = False
        self.agent.register_feed('pid_output',
                                 record=True,
                                 agg_params={'frame_length':10*60},
                                 buffer_time=5)


    #==================#
    # Coldload Methods #
    #==================#

    def get_temp(self, session, params):
        '''get_temp()

        **Task** - Get the current coldload temperature.

        '''
        with self.lock.acquire_timeout(timeout=5, job='get_temp') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, 'Could not acquire lock.'
            acq_status = self.ls240.acq.status().session
            if acq_status['op_code'] == 3:
                try:
                    temp = acq_status['data']['fields'][self.ls240_channel]['T']
                except KeyError as e:
                    self.log.error(f'Specified LS240 channel is not valid: {e}')
                    return False, f'Failed to get coldload temperature.'

                session.data['temperature'] = temp
            else:
                self.log.error('LS240 temperature monitoring is not running.')
                return False, f'Failed to get coldload temperature.'
        return True, temp

    @ocs_agent.param('temp', type=float, check=lambda x: 60 <= x <= 120)
    @ocs_agent.param('sample_interval', type=float, default = 0.2)
    @ocs_agent.param('average_interval', type=float, default = 60)
    @ocs_agent.param('lock_interval', type=float, default=0.1)
    @ocs_agent.param('timeout', type=float, default=120)
    @ocs_agent.param('max_current', type=float, default=None)
    @ocs_agent.param('prop', type=float, default = 0)
    @ocs_agent.param('int', type=float, default=0)
    @ocs_agent.param('der', type=float, default=0)
    def set_temp(self, session, params):
        '''
        Set the coldload temperature using a PID controller.

        '''
        target = params['temp']
        max_current = self.max_current if params['max_current'] is None else params['max_current']
        p = params['prop']
        i = params['int']
        d = params['der']

        num_points = int(params['average_interval']/params['sample_interval'])
        errs = deque(num_points*[0])
        dts = deque((num_points-1)*[1])
        integral = 0.0
        der = 0.0

        # Calcualte I^2 so that the control variable is proportional to power (which is roughly linear with temperature)
        power = self.psu.get_curr(self.channel)**2

        start_time = time.time()
        last_release = time.time()
        last_sample = time.time()
        self.temp_control = True

        while self.temp_control and not (time.time() - start_time > params['timeout']*60):
            dt = time.time() - last_sample
            if dt > params['sample_interval']:
                success, temp = self.get_temp(session, params = None)
                last_sample = time.time()

                with self.lock.acquire_timeout(timeout=1, job='set_temp') as acquired:
                    if not acquired:
                        self.log.error(f"Lock could not be acquired because it is held by {self.lock.job}.")
                        return False
                
                    err = float(temp) - target
                    errs.append(err)
                    errs.popleft()

                    dts.append(dt)
                    dts.popleft()

                    integral += err * dt
                    if power == 0: integral = 0.0
                    
                    der = np.mean([diff/t for diff, t in zip(np.diff(errs), dts)])

                    power += p*errs[-1] + i*integral + d*der
                    current = np.sqrt(current)
                    current = max(min(current, max_current), 0)
                    self.psu.set_curr(self.channel, current)

                    pids = {'target_temp': float(target),'current': float(current), 'error': float(err), 'integral': float(integral), 'derivative': float(der)}

                    data = {'timestamp': time.time(),
                            'block_name': 'pid',
                            'data': pids}
                    
                    self.agent.publish_to_feed('pid_output', data)
                    session.data = data

                    # Release and reacquire the lock every ~0.1 second
                    if time.time() - last_release > params['lock_interval']:
                        last_release = time.time()
                        if not self.lock.release_and_acquire(timeout=120):
                            self.log.error(f'Could not re-acquire lock now held by {self.lock.job}.')
                            return False, 'Could not re-acquire lock.'

        return True, 'set_temp executed successfully.'

    def stop_set_temp(self, session, params):
        if self.temp_control:
            self.temp_control = False
            return True,  'Stopping setting coldload temperature...'
        else:
            return False, 'Not currently setting coldload temperature.'
    
    #===============================#
    # Overload ScpiPsuAgent Methods #
    #===============================#

    def get_voltage(self, session, params):
        params['channel'] = self.channel
        return super().get_voltage(session, params=params)

    def get_current(self, session, params):
        params['channel'] = self.channel
        return super().get_current(session, params=params)

    @ocs_agent.param('volts', type=float, check=lambda x: 0 <= x <= 30)
    def set_voltage(self, session, params):
        params['channel'] = self.channel
        return super().set_voltage(session, params=params)

    @ocs_agent.param('current', type=float)
    def set_current(self, session, params):
        params['channel'] = self.channel
        params['current'] = max(min(params['current'], self.max_current), 0)
        return super().set_current(session, params=params)

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--ip-address')
    pgroup.add_argument('--gpib-slot')
    pgroup.add_argument('--port')
    pgroup.add_argument('--channel', type=int, help='The power supply channel connected to the coldload.')
    pgroup.add_argument('--ls240', nargs=2, type=str, help='Instance ID of LS240 agent and thermometer channel of the coldload.', metavar = ('LS240 Agent Instance ID', 'Coldload Channel'))
    pgroup.add_argument('--mode', type=str, default='acq',
                        choices=['init', 'acq'])
    pgroup.add_argument('--max_current', type=float, default=0.6, help='Maximum current limit in amps.')

    return parser

def main(args=None):
    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='ColdloadAgent',
                                  parser=parser,
                                  args=args)
    init_params = False
    if args.mode == 'acq':
        init_params = {'auto_acquire': True}
    agent, runner = ocs_agent.init_site_agent(args)

    c = ColdloadAgent(agent, args.ip_address, gpib_slot=args.gpib_slot, port=args.port, channel = args.channel, ls240 = args.ls240, max_current=args.max_current)

    agent.register_task('init', c.init, startup=init_params)
    agent.register_task('set_voltage', c.set_voltage)
    agent.register_task('set_current', c.set_current)
    agent.register_task('set_output', c.set_output)

    agent.register_task('get_voltage', c.get_voltage)
    agent.register_task('get_current', c.get_current)
    agent.register_task('get_temp', c.get_temp)
    agent.register_task('get_output', c.get_output)

    agent.register_process('monitor_output', c.monitor_output, c.stop_monitoring)
    agent.register_process('set_temp', c.set_temp, c.stop_set_temp)

    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()
