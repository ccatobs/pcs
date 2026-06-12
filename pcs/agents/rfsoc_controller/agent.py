import os
import sys

import time
import argparse
from pathlib import Path
from functools import wraps
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock

# Import Twisted Modules for ccatkidlib python scripts
from autobahn.twisted.util import sleep as dsleep
from twisted.internet import protocol, reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.python.failure import Failure
from typing import Optional

# ccatkidlib Imports
from ccatkidlib.rfsoc.rfsoc_daq import R
import ccatkidlib.io as io

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
        if self.log:
            self.log.info(f"{self.script.name} | {data.strip().decode('utf-8')}")

    def errReceived(self, data):
        """Called whenever data is received through stderr"""
        self.log.error(data)

    def processExited(self, status: Failure):
        """Called when process has exited."""

        exit_code = status.value.exitCode
        if self.log:
            self.log.info(f"{self.script.name} | Process exited code {exit_code}.")

        self.deferred.callback(exit_code)

class RFSoController:
    """
    PCS Agent for controlling Radio Frequency Systems on a Chip (RFSoCs) through
    ccatkidlib scripts and methods.

    Modelled after SOCS PysmurfController with modifications.
    """

    def __init__(
        self,
        agent,
        module: str = None,
        session_timeout: float = 60 * 60,
        monitor_interval: float = 60,
        log_level="INFO",
    ):
        """
        Constructor for RfsocController.
        Initializes agent and starts new measurement session.

        Parameters:
            agent (ocs.ocs_agent): OCS agent instance
            config (str): Path to rfsoc-controller config relative to OCS_CONFIG_DIR
            module (str): Which instrument module to control with the rfsoc-controller
        Notes:
            Arguments for constructor passed through OCS config file (e.g. default.yaml in OCS_CONFIG_DIR)
        """

        # Create OCS agent and get log
        self.agent = agent
        self.ocs_session = None
        self.log = agent.log
        self.module = module

        self.lock = TimeoutLock()  # Create lock
        self._new_session(init_boards=True)  # Start new ccatkidlib session

        self.last_call = time.time()
        self.monitor_session, self.session_stale = True, False
        self.session_timeout, self.monitor_interval = session_timeout, monitor_interval

        self.prot = None

    # =================#
    # Control Methods #
    # =================#

    @staticmethod
    def _get_control(func):
        """
        Decorator for use with OCS tasks/processes of ccatkidlib methods.
        Creates the RFSoC control object with correct system state and passes it to decorated task/process.
        Updates system state after task/process finishes execution.

        Parameters:
            func (func): OCS task/process of ccatkidlib method to decorate
        """

        @wraps(func)
        def _wrapper(self, session, params):
            RC = (
                R(
                    init_boards=False,
                    init_drones=False,
                    sess_id=self.session,
                    measurement_name=self.measurement_name,
                    measurement_desc=self.measurement_desc,
                    curr_date=self.curr_date,
                )
                if not self.session_stale
                else self._new_session(init_boards=False)
            )
            self.last_call, self.session_stale = time.time(), False

            RC.NCLOs = self.NCLOs
            RC.drive_attens = self.drive_attens
            RC.sense_attens = self.sense_attens

            RC.set_NCLO(setup=False)
            RC.set_atten(setup=False)

            params["R"] = RC
            rtn = func(self, session, params)

            self._update_control(RC)

            return rtn

        return _wrapper

    @ocs_agent.param("init_boards", type=bool, default=False)
    def new_session(self, session, params):
        """new_session(init_boards=False)

        **Task** - Start a new measurement session

        Parameters:
            init_boards (bool, optional): Whether to reinitialize the RFSoC boards
        """
        with self.lock.acquire_timeout(5, job="new_session") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )

            RC = self._new_session(init_boards=params["init_boards"])
        return True, f"Succesfully created new session: {self.session}"

    def _new_session(self, init_boards):
        """
        Internal method for starting a new measurement session.

        Parameters:
            init_boards (bool): Whether to reinitialize the RFSoC boards
        """
        RC = R(
            init_boards=init_boards, init_drones=True
        )  # Instantiate RFSoC control object with full board and drone setup
        self._update_control(RC)
        self.log.info(f"Succesfully created new session: {self.session}")
        return RC

    @_get_control
    def get_session(self, session, params):
        """get_session()

        **Task** - Get the ccatkidlib sess_id of the current measurement session.

        """
        with self.lock.acquire_timeout(5, job="get_session") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )

            RC = params.pop("R")
            data = {"curr_time": time.time()}
            self._publish_data(data, RC, params, session)
        return True, self.session

    def _update_control(self, RC):
        """
        Internal method for updating the system state based on the state of the given RFSoC control object.

        Parameters:
            RC (ccatkidlib.rfsoc.rfsoc_daq.R): RFSoC control object
        """

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

    # ===============#
    # Setup Methods #
    # ===============#

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("drive", type=(int, list), default=None)
    @ocs_agent.param("sense", type=(int, list), default=None)
    def set_atten(self, session, params):
        """ """
        with self.lock.acquire_timeout(5, job="atten") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            drive, sense = RC.set_atten(**params)
            data = {"drive": drive, "sense": sense}
            self._publish_data(data, RC, params, session)
        return True, "Successfully set drive/sense attenuations."

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("NCLO", type=(int, list), default=None)
    def set_NCLO(self, session, params):
        """ """
        with self.lock.acquire_timeout(5, job="NCLO") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            NCLO = RC.set_NCLO(**params)
            data = {"NCLO": NCLO}
            self._publish_data(data, RC, params, session)
        return True, "Successfully set NCLO frequencies."

    # ================#
    # Script Methods #
    # ================#

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
                self.log.error(
                    f"The requested script cannot be run because the lock is held by {self.lock.job}"
                )
                return (
                    False,
                    f"The requested script cannot be run because lock is held by {self.lock.job}",
                )
            self.ocs_session = session
            try:
                self.prot = CCATKIDlibScriptProtocol(script, log=self.log)
                self.prot.deferred = Deferred()
                python_exec = sys.executable

                cmd = [python_exec, "-u", script] + list(map(str, args))

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
    @ocs_agent.param("script", type=str)
    @ocs_agent.param("args", type=list, default=[])
    @ocs_agent.param("new_session", type=bool, default=True)
    def run(self, session, params):
        """run(script, args=None)

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

        """
        status, msg = yield self._run_script(
            session, params["script"], params.get("args", [])
        )
        # Set stored NCLO and attenuations to None since their state may have changed during script execution
        self.NCLOs = None
        self.drive_attens = None
        self.sense_attens = None

        if params["new_session"]:
            self._new_session(init_boards=False)

        return status, msg

    def abort(self, session, params=None):
        """abort()

        **Task** - Aborts the actively running script.

        """
        self.prot.transport.signalProcess("KILL")
        return True, "Aborting process"

    # ==================#
    # Main DAQ Methods #
    # ==================#

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("time", type=float, check=lambda x: x > 0)
    @ocs_agent.param("write_comb", type=bool, default=None)
    @ocs_agent.param("tone_freqs", type=list, default=None)
    @ocs_agent.param("tone_powers", type=list, default=None)
    @ocs_agent.param("tone_phis", type=list, default=None)
    def take_timestream(self, session, params):
        with self.lock.acquire_timeout(5, job="timestream") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            t_sec = params.pop("time")
            stream_files = RC.take_timestream(t_sec, **params)
            data = {"data_files": list(map(str, stream_files))}
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished taking timestream."

    # ===============#
    # Sweep Methods #
    # ===============#

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("write_comb", type=bool, default=True)
    @ocs_agent.param("sweep_steps", type=(float, list), default=None)
    def take_vna_sweep(self, session, params):
        """take_vna_sweep(com_to=None, write_comb=True, sweep_steps=None, parallel_boards=None, parallel_drones=None)

        **Task** - Take a VNA sweep

        Parameters:
            com_to (list[str], optional): List of drones to take VNA sweep
            write_comb (bool, optional): Whether to write a new VNA comb (default: True)
            sweep_steps (int | list[int], optional): Number of points each tone should sweep (default: sweep_steps in drone_config)
            parallel_boards (int, optional): Number of boards to run in parallel (default: parallel_boards in system_config)
            parallel_drones (int, optional): Number of drones to run in parallel (default: parallel_drones in system_config)

        Examples:
            Take VNA sweep with all drones of board 1 and drone 1 of board 2 in parallel::
                client.take_vna_sweep(com_to=['1', '2.1'], sweep_steps=500, parallel_boards=2, parallel_drones=4)

        Notes:
            Example session data:
                >>> response.session['data']
                PUT EXAMPLE HERE
        """
        with self.lock.acquire_timeout(5, job="vna_sweep") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            vna_files = RC.take_vna_sweep(**params)
            data = {"data_files": list(map(str, vna_files))}
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished taking VNA sweep."

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("chan_bw", type=(float, list), default=None)
    @ocs_agent.param("sweep_steps", type=(int, list), default=None)
    @ocs_agent.param("write_comb", type=bool, default=None)
    @ocs_agent.param("tone_freqs", type=list, default=None)
    @ocs_agent.param("tone_powers", type=list, default=None)
    @ocs_agent.param("tone_phis", type=list, default=None)
    def take_target_sweep(self, session, params):
        """take_target_sweep(com_to=None, write_comb=True, sweep_steps=None, parallel_boards=None, parallel_drones=None)

        **Task** - Take a target sweep

        Parameters:
            com_to (list[str], optional): List of drones to take VNA sweep
            write_comb (bool, optional): Whether to write a new VNA comb (default: True)
            sweep_steps (int | list[int], optional): Number of points each tone should sweep (default: sweep_steps in drone_config)
            parallel_boards (int, optional): Number of boards to run in parallel (default: parallel_boards in system_config)
            parallel_drones (int, optional): Number of drones to run in parallel (default: parallel_drones in system_config)

        Examples:
            Take target sweep with all drones of board 1 and drone 1 of board 2 in parallel::
                client.take_target_sweep(com_to=['1', '2.1'], sweep_steps=500, parallel_boards=2, parallel_drones=4)

        Notes:
            Example session data:
                >>> response.session['data']
                PUT EXAMPLE HERE
        """
        with self.lock.acquire_timeout(5, job="target_sweep") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            targ_files = RC.take_target_sweep(**params)
            data = {"data_files": list(map(str, targ_files))}
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished taking target sweep."

    @_get_control
    @ocs_agent.param("com_to", type=list, default=None)
    @ocs_agent.param("write_comb", type=bool, default=None)
    @ocs_agent.param("new_sweep", type=bool, default=True)
    @ocs_agent.param("sweep_steps", type=(float, list[float]), default=None)
    def find_detectors(self, session, params):
        with self.lock.acquire_timeout(5, job="find_detectors") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            found_nums, vna_files = RC.find_detectors(**params)
            vna_files = list(map(str, vna_files))
            data = {"found_nums": found_nums, "data_files": vna_files}
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished finding detectors from VNA sweep."

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("method", type=str, default="grad")
    @ocs_agent.param("new_sweep", type=bool, default=True)
    @ocs_agent.param("chan_bw", type=(float, list), default=None)
    @ocs_agent.param("sweep_steps", type=(float, list), default=None)
    @ocs_agent.param("write_comb", type=bool, default=None)
    @ocs_agent.param("tone_freqs", type=list, default=None)
    @ocs_agent.param("tone_powers", type=list, default=None)
    @ocs_agent.param("tone_phis", type=list, default=None)
    def tune_tone_placement(self, session, params):
        with self.lock.acquire_timeout(5, job="tune_tone_placement") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            targ_files = RC.tune_tone_placement(**params)
            data = {"data_files": list(map(str, targ_files))}
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished tuning detector tone frequencies."

    @_get_control
    @ocs_agent.param("com_to", type=(str, list), default=None)
    @ocs_agent.param("method", type=str, default="stream")
    @ocs_agent.param(
        "atten_bounds",
        type=list,
        default=None,
        check=lambda x: all(len(xx) == 2 for xx in x),
    )
    @ocs_agent.param("num_atten", type=int, default=None, check=lambda x: x > 1)
    @ocs_agent.param("chan_bw", type=(float, list), default=None)
    @ocs_agent.param("sweep_steps", type=(float, list), default=None)
    @ocs_agent.param("write_comb", type=bool, default=None)
    @ocs_agent.param("tone_freqs", type=list, default=None)
    @ocs_agent.param("tone_powers", type=list, default=None)
    @ocs_agent.param("tone_phis", type=list, default=None)
    @ocs_agent.param("stream_time", type=float, default=0, check=lambda x: x >= 0)
    def tune_tone_power(self, session, params):
        """ """
        with self.lock.acquire_timeout(5, job="tune_tone_power") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )
            params = self._filter_params(params)
            RC = params.pop("R")
            targ_files, stream_files = RC.tune_tone_power(**params)
            data = {
                "data_files": {
                    "targ": list(map(str, targ_files)),
                    "stream": list(map(str, stream_files)),
                }
            }
            self._publish_data(data, RC, params, session)
        return True, "Successfully finished tuning detector tone powers."

    # ===========#
    # Processses #
    # ========== #
    def monitor_session(self, session, params):
        """ """

        with self.lock.acquire_timeout(5, job="monitor_session") as acquired:
            if not acquired:
                self.log.error(
                    f"Could not acquire lock because it is held by {self.lock.job}."
                )
                return (
                    False,
                    f"Could not acquire lock because it is held by {self.lock.job}.",
                )

            while self.monitor_session:
                if time.time() - self.last_call >= self.session_timeout:
                    self.session_stale = True  # Mark that the current session has become stale and a new session should be created upon next command call

                if not self.lock.release_and_acquire(timeout=0):
                    self.log.error(
                        f"Could not re-acquire lock now held by {self.lock.job}."
                    )
                    return False, "Could not re-acquire lock."
                time.sleep(self.monitor_interval)
        return True, "Session monitoring exceuted successfully."

    def stop_monitoring(self, session, params):
        """ """
        if self.monitor_session:
            self.monitor_session = False
            return True, "Stopping session monitoring..."
        else:
            return False, "Not currently monitoring session."

    # ================#
    # Helper Methods #
    # ================#
    def _filter_params(self, params):
        """
        Internal function for filtering out keys with None value from params dictionary
        so that ccatkidlib defaults are used

        Parameters:
            params (dict[any]): params dictionary to filter
        """
        return {k: v for k, v in params.items() if v is not None}

    def _publish_data(self, data, RC, params, session):
        """
        Internal method for publishing data returned by ccatkidlib OCS task/process
        to the OCS OpSession.data dictionary.

        Parameters:
            data (any): Data returned by ccatkidlib method that was run
            RC (ccatkidlib.rfsoc.rfsoc_daq.R): RFSoC control object used to run ccatkidlib method
            params (dict[any]): Parameters used to run ccatkidlib method
            session (ocs.ocs_agent.OpSession): OpSession of ccatkidlib OCS task/process
        """

        # Check if ccatkidlib method was run with a different set of drones than in system_config file
        com_to = params["com_to"] if "com_to" in params else RC.drone_list

        # Create data dictionary with returned data, drones used, and measurement info
        data_dict = {
            "module": self.module,
            "name": RC.measurement_name,
            "date": RC.curr_date,
            "session": RC.sess_id,
            "timestamp": RC.timestamp,
            "com_to": com_to,
            "data": data,
        }

        # Pass data dictionary to OpSession.data
        session.data = data_dict


def make_parser(parser=None):
    """
    Build ArgumentParser for passing arguments through OCS config file (e.g. default.yaml in OCS_CONFIG_DIR)
    """
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group("Agent Options")
    pgroup.add_argument(
        "--module",
        type=str,
        choices=["280-GHz", "350-GHz", "850-GHz", "eor-spec", "mod-cam"],
        help="Which instrument module to control with rfsoc-controller.",
    )
    pgroup.add_argument(
        "--session-timeout",
        type=float,
        default=3600,
        help="Time after which a new session should be started if no commands have been run.",
    )
    pgroup.add_argument(
        "--monitor-interval",
        type=float,
        default=60,
        help="Time interval at which to monitor session status.",
    )

    pgroup.add_argument(
        "--log-level",
        type=str,
        default='INFO',
        help="Level at which to log ccatkidlib messages.",
    )

    return parser


def main(args=None):
    # Parse arguments passed in OCS config file
    # -----------------------------------------
    parser = make_parser()
    args = site_config.parse_args(
        agent_class="RfsocController", parser=parser, args=args
    )

    # Create RFSoController agent
    # ---------------------------
    agent, runner = ocs_agent.init_site_agent(args)
    rfsoc_controller = RFSoController(
        agent,
        module=args.module,
        session_timeout=args.session_timeout,
        monitor_interval=args.monitor_interval,
        log_level=args.log_level,
    )

    # Register agent tasks and processes
    # ----------------------------------
    agent.register_task("run", rfsoc_controller.run, blocking=False)
    agent.register_task("abort", rfsoc_controller.abort, blocking=False)
    agent.register_task("get_session", rfsoc_controller.get_session)
    agent.register_task("new_session", rfsoc_controller.new_session)
    agent.register_task("set_NCLO", rfsoc_controller.set_NCLO)
    agent.register_task("set_atten", rfsoc_controller.set_atten)
    agent.register_task("take_vna_sweep", rfsoc_controller.take_vna_sweep)
    agent.register_task("take_target_sweep", rfsoc_controller.take_target_sweep)
    agent.register_task("take_timestream", rfsoc_controller.take_timestream)
    agent.register_task("find_detectors", rfsoc_controller.find_detectors)
    agent.register_task("tune_tone_placement", rfsoc_controller.tune_tone_placement)
    agent.register_task("tune_tone_power", rfsoc_controller.tune_tone_power)

    agent.register_process(
        "monitor_session",
        rfsoc_controller.monitor_session,
        rfsoc_controller.stop_monitoring,
    )
    # Run agent
    # ---------
    runner.run(agent, auto_reconnect=True)


if __name__ == "__main__":
    main()
