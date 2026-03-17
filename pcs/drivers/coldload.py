from ocs.ocs_client import OCSClient
import time
import numpy as np
import txaio 
txaio.use_twisted()

class Coldload:

    def __init__(self, lakeshore, ls_channel):
        self.ls_channel = ls_channel
        self.log = txaio.make_logger()
        
        # Create Lakeshore client for grabbing coldload temperature data
        try:
            self.lakeshore = OCSClient(lakeshore, args=[]) 
        except Exception as e:
            self.log.error(f'Could not connect to Lakeshore agent \033[3m{lakeshore}\033[0m for temperature monitoring: {e}')

    def get_temp(self):
        """
        Get the current temperature of the coldload.
        """
        # Fetch most data from Lakeshore OCS feed
        acq_status = self.lakeshore.acq.status().session

        # Check to see if lakeshore is actively acquiring data
        if acq_status['op_code'] == 3:
            # Get coldload temperature data using specified channel
            try:
                temp = acq_status['data']['fields'][self.ls_channel]['T']
            except KeyError as e:
                self.log.error(f'Specified Lakeshore channel {self.ls_channel} is not valid: {e}')
                temp = None
        else:
            self.log.error('Lakeshore data acquisition is not running.')
            temp = None
        return temp

    def set_temp(self, temp: float, get_current, set_current, *args, **kwargs):
        """
        Set the temperature of the coldload using a proportional integral derivative (PID) controller.
        The PID controller uses the coldload temperature as the process variable and the current squared as the control variable. 

        Parameters:
            temp (float): Temperature to set coldload to
            get_current: Function for getting current. Abstracted so that set_temp is compatible with different power supplies
            set_current: Function for setting current. Should have "curr" argument as a keyword argument or as the last positional argument. Abstracted so that set_temp is compatible with different power supplies
            args: Arguments for get_current and set_current functions

            kwargs:
                sample_int (float): Interval at which to sample coldload temperature
                avg_int    (float): Interval over which to average coldload temperatures (averaged temperature used as PID process variable). Also sets timescale for PID control
                thresholds  (List(float)): Error thresholds at which to modify avg_int. avg_int will be used for errors greater than the largest threshold and then multiplied by 2 for each threshold passed.
                timeout (float): Time in minutes after which to exit PID loop (0 for indefinite)
                yield_dict (bool): Whether to yield error values and coldload current after each PID control loop
                max_current (float): Maximum current limit
                pid (List[float]): Proportional, integral, and derivative control coefficients 
                int_threshold (float): Error threshold hold after which the integral term will start contributing to the PID control.
        """

        sample_int = 0.5
        default_avg_int = 7.5
        timeout = 180
        yield_dict = False

        reset_current = False
        max_current = 0.6
        init_pid = [5e-4, 1e-7, 9e-2]
        stable_pid = [5e-4, 1e-7, 9e-2]
        ID_threshold = 0.125
        thresholds = [1.13e-3, 2.5e-7, 0.35]

        err_p = temp - self.get_temp()
        err_i = 0.0
        err_d = 0.0
        errs = []

        for k, v in kwargs.items():
            if k == 'sample_int':
                sample_int = v
            elif k == 'avg_int':
                default_avg_int = v
            elif k == 'thresholds':
                thresholds = v
            elif k == 'timeout':
                timeout = v
            elif k == 'yield_dict':
                yield_dict = v
            elif k == 'max_current':
                max_current = v
            elif k == 'init_pid': 
                init_pid = v
            elif k == 'stable_pid':
                stable_pid = v
            elif k == 'err_i':
                err_i = v
            elif k == 'ID_threshold':
                ID_threshold = v
            elif k == 'reset_current':
                reset_current = v
        avg_int = default_avg_int
        timeout *= 60 # Convert timeout to seconds

        # Get the current coldload current and use current squared as the control variable so that it is proportional to power (which is roughly linear with temperature)
        curr_sq = get_current(*args)**2
        pid = init_pid

        start_time  = time.time()
        last_sample = start_time
        last_pid    = start_time
        while timeout == 0 or time.time() - start_time < timeout:
            curr_time = time.time()
            if curr_time - last_sample > sample_int:
                last_sample = curr_time
                errs.append(temp - self.get_temp())
                
                delta_t = curr_time - last_pid
                if delta_t > avg_int:
                    last_pid = curr_time

                    avg_err = np.mean(errs) # Average the error to reduce noise
                    errs = [] # Reset list of errors

                    # Calculate the PID error values
                    if np.abs(avg_err) <= ID_threshold: 
                        if curr_sq == 0: 
                            pid = stable_pid
                            ID_threshold *= 10
                        err_i += avg_err * delta_t
                        err_d = (avg_err - err_p)/delta_t
                    err_p = avg_err

                    # Vary avg_int depending on how small error is to reduce noise in derivative at small errors 
                    avg_int = default_avg_int * (2 ** sum(np.abs(err_p) < threshold for threshold in thresholds))
                    

                    # Vary the current squared as specified by the PID controller
                    curr_sq += pid[0]*err_p + pid[1] * err_i + pid[2]*err_d
                    
                    # Convert to current and limit it to be between 0 and max_current
                    curr_sq = max(curr_sq, 0.0)
                    curr = round(min(np.sqrt(curr_sq), max_current), 3) # Round down to mA precision

                    # Set the new current
                    set_current(*args, curr = curr)

                    if yield_dict: 
                        pids = {'target_temperature': float(temp),'current': float(curr), 'err_p': float(err_p), 'err_i': float(err_i), 'err_d': float(err_d)}
                        yield pids
            time.sleep(0.1) # Wait to prevent wasting CPU resources
            if yield_dict: yield None # Yield None on non-PID loops to prevent the method from blocking for avg_int seconds 
        if reset_current: set_current(*args, curr=0.0)