import time
import argparse
from serial import Serial, EIGHTBITS, STOPBITS_ONE, PARITY_NONE
import serial
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock

from pcs.drivers.adam import Module

class Adam_Agent:
    """Class to control and retrieve data from the Adam scale for the ModCam LN2 dewar

    This Agent is meant to be an example for Agent development, and provides a
    clean starting point when developing a new Agent.
    Parameters:
        agent (OCSAgent): OCSAgent object from :func:`ocs.ocs_agent.init_site_agent`.

    Attributes:
        agent (OCSAgent): OCSAgent object from :func:`ocs.ocs_agent.init_site_agent`.
    """

    def __init__(self, agent, port="/dev/ADAM", f_sample=0.5): #, timeout=1):

        self.agent = agent
        self.log = agent.log
        self.lock = TimeoutLock()

        self.port = port
        #self.timeout = timeout
        self.f_sample = f_sample

        self.initialized = False

        #register weight feed
        agg_params = {'frame_length': 60}
        self.agent.register_feed('weight',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)

    #@ocs_agent.param('auto_acquire', default=False, type=bool)
    def init_adam(self, session, params):
        """init_lakeshore(auto_acquire=False)

        **Task** - Perform first time setup of the Lakeshore 425 Module.

        Parameters:
            auto_acquire (bool, optional): Default is False. Starts data
                acquisition after initialization if True.

        """
        if params is None:
            params = {}

        auto_acquire = params.get('auto_acquire', False) #params['auto_acquire']

        if self.initialized:
            return True, "Already Initialized Module"

        with self.lock.acquire_timeout(0, job='init') as acquired:
            if not acquired:
                self.log.warn("Could not start init because "
                              "{} is already running".format(self.lock.job))
                return False, "Could not acquire lock."

            #self.dev = usb.core.find(idVendor = self.vid, idProduct = self.pid)
            self.module = Module(port = self.port)
            #print(self.module)
            self.module.connect()
            if self.module is None:
                raise ValueError('Device not found')
            #self.log.info(self.dev.get_id())
            print("Initialized Adam: {!s}".format(self.module))

        self.initialized = True

        # Start data acquisition if requested
        if auto_acquire:
            self.agent.start('acq')

        return True, 'Adam initialized.'

    @ocs_agent.param('sampling_frequency', type=float, default = 0.5) #2.5)
    @ocs_agent.param('test_mode', type = bool, default = False)
    def acq(self, session, params=None):

        if params is None:
            params = {}
        f_sample = params['sampling_frequency']
        if f_sample is None:
            f_sample = self.f_sample

        sleep_time = 1. / f_sample - 0.01

        with self.lock.acquire_timeout(0, job='acq') as acquired:
            if not acquired:
                self.log.warn("Could not start init because "
                              "{} is already running".format(self.lock.job))
                return False, "Could not acquire lock."


            session.set_status('running')

            self.take_data = True

            session.data = {'fields': {}}

            while self.take_data:
                current_time = time.time()
                data = {
                    'timestamp': current_time,
                    'block_time': 'weight',
                    'data': {}
                }

                weight = self.module.read_weight()
                print(weight)

                #weight = weight_line['weight']

                data['data']['weight'] = weight
                #print(data)
                field_dict = {'weight': weight}
                session.data['fields'].update(field_dict)
                #print(session.data)
                self.agent.publish_to_feed('weight', data)

                session.data['fields'].update({'timestamp': current_time})

                #time.sleep(sleep_time)
                #print(data['data']['weight'])

            self.agent.feeds['weight'].flush_buffer()

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
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port', type=str,
                        help="Port of Adam scale.  Defaults to /dev/ADAM if not specified.")
    pgroup.add_argument('--mode', type=str, choices=['init', 'acq'],
                        help="Starting action for the agent.")
    pgroup.add_argument('--sampling-frequency', type=float,
                        help="Sampling frequency for data acquisition")

    return parser



def main(args=None):

    parser = make_parser()

    args = site_config.parse_args(agent_class='Adam_Agent', parser=parser, args=args)

    init_params = False
    if args.mode == 'init':
        init_params = {'auto_acquire': False}
    elif args.mode == 'acq':
        init_params = {'auto_acquire': True}

    agent, runner = ocs_agent.init_site_agent(args)
    adam = Adam_Agent(agent)

    agent.register_task('init_adam', adam.init_adam, startup=init_params)
    agent.register_process('acq', adam.acq, adam._stop_acq)

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()
