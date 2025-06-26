import argparse
import time
import numpy as np
import os
from collections import deque

import txaio
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock
from socs.agents.scpi_psu.agent import ScpiPsuAgent

from pcs.drivers.coldload import Coldload


class ColdloadAgent_ScpiPsu(ScpiPsuAgent):
    def __init__(self, agent, ip_address, gpib_slot=None, port=None, lakeshore=None, psu_channel=None, max_current=None):
        # Initialize ScpiPsuAgent
        super().__init__(agent, ip_address, gpib_slot=gpib_slot, port=port) 

        # Define additional attributes
        self.psu_channel = psu_channel
        self.temp_control = False
        self.max_current = max_current

        # Create coldload object
        self.cl = Coldload(lakeshore[0], lakeshore[1], ext_log=self.log)
        self.err_i = 0.0 # Store integral error of set_temp PID in case control loop is interrupted 

        # Register OCS feed to log PID temperature control parameters
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

            temp = self.cl.get_temp()
            data = {'timestamp': time.time(),
                    'block_name': 'coldload', 
                    'data': {'temp': temp}}
            session.data = data
            
        return temp is not None, temp

    @ocs_agent.param('temp', type=float, check=lambda x: 60 <= x <= 120)
    @ocs_agent.param('sample_int', type=float, default = 0.5)
    @ocs_agent.param('avg_int', type=float, default = 7.5)
    @ocs_agent.param('lock_int', type=float, default=0.1)
    @ocs_agent.param('timeout', type=float, default=180)
    @ocs_agent.param('max_current', type=float, default=None)
    @ocs_agent.param('pid', type=list, default=[2.25e-3, 5.1e-7, 0.71])
    def set_temp(self, session, params):
        """
        **Process** - Set the temperature of the coldload using a proportional integral derivative (PID) controller.
        The PID controller uses the coldload temperature as the process variable and the current squared as the control variable. 

        Parameters:
            temp (float): Temperature to set coldload to
            sample_int  (float): Interval at which to sample coldload temperature
            avg_int     (float): Interval over which to average coldload temperatures (averaged temperature used as PID process variable). Also sets timescale for PID control
            lock_int    (float): Interval at which to release lock
            timeout     (float): Time in minutes after which to exit PID loop (0 for indefinite)
            max_current (float): Maximum current limit
            pid         (List[float]): Proportional, integral, and derivative control coefficients 
        """

        temp = params.pop('temp')

        lock_int = params.pop('lock_int')
        if params['max_current'] is None: params['max_current'] = self.max_current 

        with self.lock.acquire_timeout(timeout=1, job='set_temp') as acquired:
            if not acquired:
                self.log.error(f"Lock could not be acquired because it is held by {self.lock.job}.")
                return False
            

            last_release = time.time()
            curr_args = [self.psu_channel]
            params['yield_dict'] = True
            params['err_i'] = self.err_i
            pid_control = self.cl.set_temp(temp, self.psu.get_curr, self.psu.set_curr, *curr_args, **params)
            self.temp_control = True
            while self.temp_control:
                # Perform PID control loop and get PID error values
                try:
                    pids = next(pid_control)
                    # Create data dictionary and publish to pid_output feed and session.data
                    if pids is not None:
                        data = {'timestamp': time.time(),
                                'block_name': 'coldload',
                                'data': pids}
                        self.agent.publish_to_feed('pid_output', data)
                        session.data = data
                        self.err_i = pids['err_i']

                    # Release and reacquire the lock
                    if time.time() - last_release > lock_int:
                        last_release = time.time()
                        if not self.lock.release_and_acquire(timeout=120):
                            self.log.error(f'Could not re-acquire lock now held by {self.lock.job}.')
                            return False, 'Could not re-acquire lock.'
                # Catch exception raised if set_temp timeout is reached
                except StopIteration:
                    self.temp_control = False  
        return True, 'set_temp executed successfully.'

    def stop_set_temp(self, session, params):
        """stop_set_temp()

        **Process** - Stop the process setting the coldload temperature. Called when running set_temp.stop()

        """
        if self.temp_control:
            self.temp_control = False
            return True,  'Stopping setting coldload temperature...'
        else:
            return False, 'Not currently setting coldload temperature.'
    
    #===============================#
    # Overload ScpiPsuAgent Methods #
    #===============================#

    def get_output(self, session, params):
        """get_output()

        **Task** - Get whether the channel connected to the coldload is on or off.
        
        """
        params['channel'] = self.psu_channel
        return super().get_output(session, params=params)

    def get_voltage(self, session, params):
        """get_voltage()

        **Task** - Get the voltage of the coldload. 
        
        """
        params['channel'] = self.psu_channel
        return super().get_voltage(session, params=params)

    def get_current(self, session, params):
        """get_current()

        **Task** - Get the current of the coldload. 

        """
        params['channel'] = self.psu_channel
        return super().get_current(session, params=params)

    @ocs_agent.param('state', type=bool)
    def set_output(self, session, params):
        """set_output(state)

        **Task** - Turn the channel connected to the coldload on or off.

        Parameters:
            state (bool): True for on, False for off.
        """

        params['channel'] = self.psu_channel
        return super().set_output(session, params=params)

    @ocs_agent.param('volts', type=float, check=lambda x: 0 <= x <= 30)
    def set_voltage(self, session, params):
        """set_voltage(volts)

        **Task** - Set the voltage of the coldload. 

        Parameters:
            volts (float): Voltage to set.
        """
        
        params['channel'] = self.psu_channel
        return super().set_voltage(session, params=params)

    @ocs_agent.param('current', type=float)
    def set_current(self, session, params):
        """set_current(current)

        **Task** - Set the current of the coldload. 

        Parameters:
            current (float): Current to set.
        """

        # Override set_current method to use power supply channel connected to coldload and to limit the max current.
        params['channel'] = self.psu_channel
        params['current'] = max(min(params['current'], self.max_current), 0)
        return super().set_current(session, params=params)

    #============================================#
    # Read/Write Serial Commands to Power Supply #
    #============================================#

    def read(self, session, params):
        """read()

        **Task** - Read message from power supply

        """

        with self.lock.acquire_timeout(timeout=5, job='read') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, 'Could not acquire lock.'
        
            resp = self.psu.read()
            data = {'timestamp': time.time(),
                    'block_name': 'power_supply',
                    'data': {'read': resp}}
            session.data = data
            return True, resp
    
    @ocs_agent.param('msg', type=str, default='')
    def write(self, session, params):
        """write(msg)

        **Task** - Write serial command to power supply.

        Parameters:
            msg (str): Serial command
        """
        with self.lock.acquire_timeout(timeout=5, job='write') as acquired:
            if not acquired:
                self.log.error(f'Lock could not be acquired because it is held by {self.lock.job}.')
                return False, 'Could not acquire lock.'
            
            msg = params['msg']
            if not msg:
                return False, f"Invalid message: {msg}"
            else:
                self.psu.write(msg)
            return True, f"Wrote message to power supply."

#===========#
# Functions #
#===========#

def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """
    # From simonsobs/socs/socs/agents/scpi_psu/agent.py with additions

    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--ip-address')
    pgroup.add_argument('--gpib-slot')
    pgroup.add_argument('--port')
    pgroup.add_argument('--psu-channel', type=int, help='The power supply channel connected to the coldload.')
    pgroup.add_argument('--lakeshore', nargs=2, type=str, help='Instance ID of lakeshore agent and thermometer channel of the coldload.', metavar = ('Lakeshore Agent Instance ID', 'Lakeshore Thermometer Channel of Coldload'))
    pgroup.add_argument('--mode', type=str, default='init',
                        choices=['init', 'acq'])
    pgroup.add_argument('--max-current', type=float, default=1, help='Maximum current limit in Amperes.')
    return parser

def main(args=None):
    # From simonsobs/socs/socs/agents/scpi_psu/agent.py with modifications

    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='ColdloadAgent',
                                  parser=parser,
                                  args=args)
                                  
    init_params = {'auto_acquire': args.mode == 'acq'}
    agent, runner = ocs_agent.init_site_agent(args)

    c = ColdloadAgent_ScpiPsu(agent, args.ip_address, gpib_slot=args.gpib_slot, port=args.port, psu_channel = args.psu_channel, lakeshore = args.lakeshore, max_current=args.max_current)

    agent.register_task('init', c.init, startup=init_params)
    agent.register_task('set_voltage', c.set_voltage)
    agent.register_task('set_current', c.set_current)
    agent.register_task('set_output', c.set_output)

    agent.register_task('get_voltage', c.get_voltage)
    agent.register_task('get_current', c.get_current)
    agent.register_task('get_temp', c.get_temp)
    agent.register_task('get_output', c.get_output)

    agent.register_task('read', c.read)
    agent.register_task('write', c.write)

    agent.register_process('monitor_output', c.monitor_output, c.stop_monitoring)
    agent.register_process('set_temp', c.set_temp, c.stop_set_temp)

    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()
