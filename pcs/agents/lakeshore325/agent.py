import argparse
import threading
import txaio
import os
from contextlib import contextmanager

from ocs import ocs_agent, site_config
from ocs.ocs_twisted import Pacemaker, TimeoutLock
from twisted.internet import reactor

from pcs.drivers.lakeshore325 import LS325

class LS325_Agent:
    """Agent to connect to a single Bluefors Temperature Controller device.
    
    """
    
    def __init__(self, agent, name, port):

        self.name = name
        self.port = port
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
        """
        self.agent.register_feed('temperatures',
                                 record=True,
                                 agg_params=agg_params,
                                 buffer_time=1)
        """
    
    def init_ls325(self, session, params=None):
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
            self.log.info("LS325 already initialized. Returning...")
            return True, "Already initialized"
            
        session.set_status('running')
        
        try:
            self.module = LS325(self.port)
        except ConnectionError:
            self.log.error("Could not connect to the LS325. Exiting.")
            reactor.callFromThread(reactor.stop)
            return False, 'LS325 initialization failed'
        except Exception as e:
            self.log.error(f"Unhandled exception encountered: {e}")
            reactor.callFromThread(reactor.stop)
            return False, 'LS325 initialization failed'

        print("Initialized LS325 module: {!s}".format(self.module))
        session.add_message("LS325 initilized with ID: %s" % self.module.id)

        #self.thermometers = [channel.name for channel in self.module.channels]
        
        self.initialized = True


        return True, 'LS325 initialized.'
        
    @ocs_agent.param('value',type=str)    
    def set_heater_units(self, session, params=None):
        self.module.heater1.set_units(params['value'])
        return True, 'Heater 1 units set'
        
    @ocs_agent.param('_')    
    def get_heater_units(self, session, params=None):
        resp = self.module.heater1.get_units()
        return True, resp
        
def make_parser(parser=None):
    """Build the argument parser for the Agent. Allows sphinx to automatically
    build documentation based on this function.

    """
    if parser is None:
        parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--port')
    pgroup.add_argument('--serial-number')

    return parser


def main(args=None):
    # For logging
    txaio.use_twisted()
    txaio.make_logger()

    # Start logging
    txaio.start_logging(level=os.environ.get("LOGLEVEL", "info"))

    parser = make_parser()
    args = site_config.parse_args(agent_class='LS325Agent',
                                  parser=parser,
                                  args=args)

    

    # Interpret options in the context of site_config.
    print('I am in charge of device with serial number: %s' % args.serial_number)

    agent, runner = ocs_agent.init_site_agent(args)

    ls325_agent = LS325_Agent(agent, args.serial_number, args.port)

    agent.register_task('init_ls325', ls325_agent.init_ls325)
    agent.register_task('get_heater_units', ls325_agent.get_heater_units)
    agent.register_task('set_heater_units', ls325_agent.set_heater_units)
    # And many more to come...

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()
    
