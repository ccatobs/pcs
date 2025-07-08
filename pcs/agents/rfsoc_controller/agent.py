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
    PCS Agent for controlling Radio Frequency Systems on a Chip (RFSoCs) through
    ccatkidlib scripts and methods. 
    
    Modelled after SOCS PysmurfController with modifications.
    '''

    def __init__(self, agent, config: str = None, module: str = None):
        '''
        Constructor for RfsocController. 
        Initializes agent and starts new measurement session.

        Parameters:
            agent (ocs.ocs_agent): OCS agent instance
            config (str): Path to rfsoc-controller config relative to OCS_CONFIG_DIR
            module (str): Which instrument module to control with the rfsoc-controller
        Notes:
            Arguments for constructor passed through OCS config file (e.g. default.yaml in OCS_CONFIG_DIR)
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
        '''new_session(init_boards=False)

        **Task** - Start a new measurement session

        Parameters:
            init_boards (bool, optional): Whether to reinitialize the RFSoC boards
        '''
        RC = self._new_session(init_boards=params['init_boards'])
        return True, f'Succesfully created new session: {self.session}'
        
    def _new_session(self, init_boards):
        '''
        Internal method for starting a new measurement session.

        Parameters:
            init_boards (bool): Whether to reinitialize the RFSoC boards
        '''
        RC = R(cfg_path = self.sys_cfg_path, init_boards = init_boards, init_drones = True) # Instantiate RFSoC control object with full board and drone setup
        self._update_control(RC)
        self.log.info(f'Succesfully created new session: {self.session}')
        return RC
    
    @staticmethod
    def _get_control(func):
        '''
        Decorator for use with OCS tasks/processes of ccatkidlib methods.
        Creates the RFSoC control object with correct system state and passes it to decorated task/process.
        Updates system state after task/process finishes execution.

        Parameters:
            func (func): OCS task/process of ccatkidlib method to decorate
        '''
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
        Internal method for updating the system state based on the state of the given RFSoC control object.

        Parameters:
            RC (ccatkidlib.rfsoc.rfsoc_daq.R): RFSoC control object
        '''

        # Create/update attributes to save system state of control object across recreations
        # ----------------------------------------------------------------------------------
        # Get the session ID, name, and description of measurement
        self.session = RC.sess_id
        self.curr_date = RC.curr_date
        self.measurement_name = RC.measurement_name
        self.measurement_desc = RC.measurement_desc
        
        # Get the current NCLOs and attenuations
        self.NCLOs = RC.NCLOs
        self.drive_attens = RC.drive_attens
        self.sense_attens = RC.sense_attens

    #===============#
    # Setup Methods #
    #===============#

    @_get_control
    @ocs_agent.param('com_to', type=(str, list[str]), default=None)
    @ocs_agent.param('drive', type=(int, list[int]), default=None)
    @ocs_agent.param('sense', type=(int, list[int]), default=None)
    def set_atten(self, session, params):
        return
    
    @_get_control
    @ocs_agent.param('com_to', type=list, default=[])
    def set_NCLO(self, session, params):
        return

    #================#
    # Script Methods #
    #================#

    @inlineCallbacks
    def _run_script(self, session, script, args):
        """
        Internal method for running a ccatkidlib RFSoC control script using the Twisted reactor.
        Modelled after _run_script method of SOCS PysmurfController

        Parameters:
            session (ocs.ocs_agent.OpSession): OpSession object of run task
            script (str): Path of ccatkidlib python script to run
            args (list[str], optional): Additional arguments to pass to script
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
        '''run(script, args=None)
        
        **Task** - Run a ccatkidlib RFSoC control script

        Parameters:
            script (str): Path of ccatkidlib python script to run
            args (list[str], optional): Additional arguments to pass to script 

        Examples:
            Example for running a test script with a client::
                client.run(script='/app/pcs/ccatkidlib/scripts/controller/test.py', args=[])
        Notes:
            Script path must be that within the docker container. 
            For example, if ccatkidlib is mounted to /app/pcs/ccatkidlib within the container,
            the path to run a script in the scripts directory would be /app/pcs/ccatkidlib/scripts/<script_name>.py

        '''
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
    @ocs_agent.param('com_to', type=list, default=None)
    @ocs_agent.param('time', type=float)
    def take_timestream(self, session, params):
        return
    
    #===============#
    # Sweep Methods #
    #===============#
    
    @_get_control
    @ocs_agent.param('R',               type=R)
    @ocs_agent.param('com_to',          type=list, default=None)
    @ocs_agent.param('write_comb',      type=bool, default=True)
    @ocs_agent.param('sweep_steps',     type=int,  default=None, check=lambda x: x > 0)
    @ocs_agent.param('parallel_boards', type=int,  default=None)
    @ocs_agent.param('parallel_drones', type=int,  default=None, check=lambda x: 4 >= x >= 1)
    def take_vna_sweep(self, session, params):
        '''take_vna_sweep(com_to=None, write_comb=True, sweep_steps=None, parallel_boards=None, parallel_drones=None)
        
        **Task** - Take a VNA sweep
        
        Parameters:
            com_to (list[str], optional): List of drones to take VNA sweep
            write_comb (bool, optional): Whether to write a new VNA comb (default: True)
            sweep_steps (int, optional): Number of points each tone should sweep (default: sweep_steps in drone_config)
            parallel_boards (int, optional): Number of boards to run in parallel (default: parallel_boards in system_config)
            parallel_drones (int, optional): Number of drones to run in parallel (default: parallel_drones in system_config)
        
        Examples:
            Take VNA sweep with all drones of board 1 and drone 1 of board 2 in parallel::
                client.take_vna_sweep(com_to=['1', '2.1'], sweep_steps=500, parallel_boards=2, parallel_drones=4)
        
        Notes:
            Example session data:
                >>> response.session['data']
                PUT EXAMPLE HERE  
        '''
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
        '''
        Internal function for filtering out keys with None value from params dictionary 
        so that ccatkidlib defaults are used

        Parameters:
            params (dict[any]): params dictionary to filter
        '''
        return {k:v for k, v in params.items() if v is not None}

    def _publish_data(self, data, RC, params, session):
        '''
        Internal method for publishing data returned by ccatkidlib OCS task/process
        to the OCS OpSession.data dictionary.

        Parameters:
            data (any): Data returned by ccatkidlib method that was run
            RC (ccatkidlib.rfsoc.rfsoc_daq.R): RFSoC control object used to run ccatkidlib method
            params (dict[any]): Parameters used to run ccatkidlib method
            session (ocs.ocs_agent.OpSession): OpSession of ccatkidlib OCS task/process
        '''

        # Check if ccatkidlib method was run with a different set of drones than in system_config file
        com_to = params['com_to'] if 'com_to' in params else RC.drone_list

        # Create data dictionary with returned data, drones used, and measurement info
        data_dict = {'name': RC.measurement_name,
                     'date': RC.curr_date,
                     'session': RC.sess_id,
                     'timestamp': RC.timestamp,
                     'com_to': com_to,
                     'data': data}

        # Pass data dictionary to OpSession.data
        session.data = data_dict
        
def make_parser(parser=None):
    '''
    Build ArgumentParser for passing arguments through OCS config file (e.g. default.yaml in OCS_CONFIG_DIR)
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
    # Parse arguments passed in OCS config file
    # -----------------------------------------
    parser = make_parser()
    args = site_config.parse_args(agent_class='RfsocController',
                                  parser = parser,
                                  args = args)
    
    # Create RFSoController agent
    # ---------------------------
    agent, runner = ocs_agent.init_site_agent(args)
    rfsoc_controller = RFSoController(agent, config = args.config, module = args.module)


    # Register agent tasks and processes
    # ----------------------------------
    agent.register_task('run', rfsoc_controller.run, blocking=False)
    agent.register_task('abort', rfsoc_controller.abort, blocking=False)
    agent.register_task('take_vna_sweep', rfsoc_controller.take_vna_sweep)

    # Run agent
    # ---------
    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()