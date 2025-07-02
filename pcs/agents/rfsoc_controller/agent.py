import os
import sys
import time

import argparse
from pathlib import Path
from functools import wraps
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock
import txaio

# Import Twisted Modules for ccatkidlib python scripts
from autobahn.twisted.util import sleep as dsleep
from twisted.internet import protocol, reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.python.failure import Failure
from typing import Optional

# ccatkidlib Imports
from ccatkidlib.rfsoc.rfsoc_daq import R
import ccatkidlib.rfsoc_io as rfsoc_io

class CCATKIDlibScriptProtocol(protocol.ProcessProtocol):
    def __init__(self, script, log=None):
        self.script = Path(script)
        self.log = log
        self.end_status: Optional[Failure] = None

    def connectionMade(self):
        """Called when process is started"""
        self.transport.closeStdin()

    def outReceived(self, data):
        """Called whenever data is received through stdout"""
        if self.log: self.log.info(f"{self.script.name} | {data.strip().decode('utf-8')}")

    def errReceived(self, data):
        """Called whenever data is received through stderr"""
        self.log.error(data)

    def processExited(self, status: Failure):
        """Called when process has exited."""

        exit_code = status.value.exitCode
        if self.log: self.log.info(f"{self.script.name} | Process exited code {exit_code}.")
        
        self.deferred.callback(exit_code)

class RFSoController:
    '''
    PCS Agent for controlling RFSoCs through ccatkidlib scripts and methods.
    Modelled after SOCS PysmurfController with modifications.
    '''
    def __init__(self, agent, config: str = None, module: str = None):
        '''
        Constructor for RfsocController. 
        '''

        # Create OCS agent and get log
        self.agent = agent
        self.ocs_session = None
        self.log = agent.log
        
        self.lock = TimeoutLock() # Create lock
        
        cfg_file = Path(os.environ['OCS_CONFIG_DIR']) / config
        try:
            self.control_cfg = rfsoc_io.load_config(cfg_file)
        except AssertionError:
            self.log.error(f'Could not find rfsoc-controller config file {cfg_file}.')
            raise FileNotFoundError
    
        self.sys_cfg_path = self.control_cfg['modules'][module]['system_config']
        
        self._new_session(init_boards=True)

        self.prot = None

    #=================#
    # Control Methods #
    #=================#

    @ocs_agent.param('init_boards', type = bool, default = False)
    def new_session(self, session, params):
        RC = self._new_session(init_boards=params['init_boards'])
        return True, f'Succesfully created new session: {self.session}'
        
    def _new_session(self, init_boards):
        RC = R(cfg_path = self.sys_cfg_path, init_boards = init_boards, init_drones = True) # Instantiate RFSoC control object with full board and drone setup
        self._update_control(RC)
        self.log.info(f'Succesfully created new session: {self.session}')
        return RC
    
    @staticmethod
    def _get_control(func):
        @wraps(func)
        def _wrapper(self, session, params):
            RC = R(cfg_path = self.sys_cfg_path, initialize_boards = False, initialize_drones = False,
                sess_id = self.session, measurement_name = self.measurement_name, measurement_desc = self.measurement_desc, curr_date = self.curr_date)
            
            RC.NCLOs = self.NCLOs
            RC.drive_attens = self.drive_attens
            RC.sense_attens = self.sense_attens
            
            RC.set_NCLO(setup=False)
            RC.set_atten(setup=False)

            params['R'] = RC

            rtn = func(self, session, params)

            self._update_control(RC)

            return rtn
        return _wrapper

    def _update_control(self, RC):
        '''
        Get the current state of the control object.
        '''

        # Create attributes to save system state of control object across recreations
        # ---------------------------------------------------------------------------
        # Get the session ID, name, and description of measurement
        self.session = RC.sess_id
        self.curr_date = RC.curr_date
        self.measurement_name = RC.measurement_name
        self.measurement_desc = RC.measurement_desc
        
        # Get the current NCLOs and attenuations
        self.NCLOs = RC.NCLOs
        self.drive_attens = RC.drive_attens
        self.sense_attens = RC.sense_attens

    #================#
    # Script Methods #
    #================#

    @inlineCallbacks
    def _run_script(self, session, script, args):
        """
        Runs a ccatkidlib control script using the Twisted reactor.
        Modified _run_script method of SOCS PysmurfController

        Args:
            script (string):
                path to the script you wish to run
            args (list, optional):
                List of command line arguments to pass to the script.
                Defaults to [].
            log (string or bool, optional):
                Determines if and how the process's stdout should be logged.
                You can pass the path to a logfile, True to use the agent's log,
                or False to not log at all.
        """

        with self.lock.acquire_timeout(5, job=script) as acquired:
            if not acquired:
                self.log.error(f"The requested script cannot be run because the lock is held by {self.lock.job}")
                return False, f"The requested script cannot be run because lock is held by {self.lock.job}"
            self.ocs_session = session 
            try:
                self.prot = CCATKIDlibScriptProtocol(script, log=self.log)
                self.prot.deferred = Deferred()
                python_exec = sys.executable

                cmd = [python_exec, '-u', script] + list(map(str, args))

                self.log.info(f"Running Script: {' '.join(cmd)}")

                reactor.spawnProcess(self.prot, python_exec, cmd, env=os.environ)

                exit_code = yield self.prot.deferred

                return exit_code == 0, f"Script has finished with exit code {exit_code}"

            finally:
                # Sleep to allow any remaining messages to be put into the
                # session var
                yield dsleep(1.0)
                self.ocs_session = None

    @inlineCallbacks
    def run(self, session, params=None):
        status, msg = yield self._run_script(session, params['script'], params.get('args', []))

        # Set stored NCLO and attenuations to None since their state may have changed during script execution
        self.NCLOs = None
        self.drive_attens = None
        self.sense_attens = None

        self._new_session(init_boards=False)

        return status, msg

    def abort(self, session, params=None):
        """abort()

        **Task** - Aborts the actively running script.

        """
        self.prot.transport.signalProcess('KILL')
        return True, "Aborting process"
    
    #==================#
    # Main DAQ Methods #
    #==================#

    def tune():
        return

    @_get_control
    @ocs_agent.param('com_to', type=list, default=[])
    @ocs_agent.param('time', type=float)
    def take_timestream(self, session, params):
        return
    
    #===============#
    # Sweep Methods #
    #===============#
    
    @_get_control
    @ocs_agent.param('R',               type=R,    default=None)
    @ocs_agent.param('com_to',          type=list, default=None)
    @ocs_agent.param('write_comb',      type=bool, default=None)
    @ocs_agent.param('sweep_steps',     type=int,  default=None)
    @ocs_agent.param('parallel_boards', type=int,  default=None)
    @ocs_agent.param('parallel_drones', type=int,  default=None)
    def take_vna_sweep(self, session, params):
        with self.lock.acquire_timeout(5, job='vna_sweep') as acquired:
            if not acquired:
                self.log.error(f"Could not acquire lock because it is held by {self.lock.job}.")
                return False, f"Could not acquire lock because it is held by {self.lock.job}."
            params = self._filter_params(params)
            RC = params.pop('R')
            data = RC.take_vna_sweep(**params)
            data = list(map(str, data))
            self._publish_data(data, RC, params, session)
        return True, 'Successfully finished taking VNA sweep.'

    @_get_control
    @ocs_agent.param('com_to', type=list, default=[])
    def take_target_sweep(self, session, params):
        return 

    @_get_control
    @ocs_agent.param('com_to', type=list, default=[])
    def find_detectors(self, session, params):
        return

    @_get_control
    @ocs_agent.param('com_to', type=list, default=[])
    def find_detectors_fine(self, session, params):
        return

    #=======#
    # Other #
    #=======#

    @ocs_agent.param('threshold', type=float, default=5)
    def monitor_space(self, session, params):
        '''monitor_space.start()
        
        **Process** - Monitor storage space of RFSoC boards and clean files as necessary. 
        '''
        
        return

    #================#
    # Helper Methods #
    #================#
    def _filter_params(self, params):
        return {k:v for k, v in params.items() if v is not None}

    def _publish_data(self, data, RC, params, session):
        com_to = params['com_to'] if 'com_to' in params else RC.drone_list
        data_dict = {'name': RC.measurement_name,
                'date': RC.curr_date,
                'session': RC.sess_id,
                'com_to': com_to,
                'data': data}
        session.data['data'] = data_dict
        
def make_parser(parser=None):
    '''
    Build ArgumentParser for passing arguments through OCS_CONFIG file
    '''
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--config',  type=str, default='controller_config.yaml',
                         help='Path to rfsoc-controller config relative to OCS_CONFIG_DIR')
    pgroup.add_argument('--module',  type=str, choices=['280GHz', '350GHz', '850GHz', 'EoR_Spec'],
                        help='Which instrument module to control with rfsoc-controller.')

    return parser

def main(args = None):
    parser = make_parser()
    args = site_config.parse_args(agent_class='RfsocController',
                                  parser = parser,
                                  args = args)
    
    agent, runner = ocs_agent.init_site_agent(args)
    rfsoc_controller = RFSoController(agent, config = args.config, module = args.module)

    agent.register_task('run', rfsoc_controller.run, blocking=False)
    agent.register_task('abort', rfsoc_controller.abort, blocking=False)
    agent.register_task('take_vna_sweep', rfsoc_controller.take_vna_sweep)

    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()