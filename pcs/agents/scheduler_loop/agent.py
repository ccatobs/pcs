import argparse
import os
import time

from ocs import ocs_agent, site_config
import fystplan.scheduling.scheduler_loop as scheduler_loop

class SchedulerLoopAgent():

    def __init__(self, agent, params):

        self.agent = agent
        self.log = agent.log

        auto_loop = params.get('auto_loop', False)

        # Start data acquisition if requested
        if auto_loop:
            self.agent.start('run_scheduler_loop')

        # return True, 'Scheduler Loop Agent initialized.'

    def run_scheduler_loop(self, session, params):
        """run_scheduler_loop()

        **Task** - Run the scheduler loop.

        """
        if params is None:
            params = {}

        self.log.info("Starting scheduler loop")
        scheduler_loop.main()

        return True, 'Scheduler loop exited cleanly.'

def make_parser(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--mode', type=str, choices=['idle', 'loop'],
                        help="Starting action for the agent.")

    return parser

def main(args=None):

    parser = make_parser()

    args = site_config.parse_args(agent_class='Scheduler_Loop_Agent', parser=parser, args=args)

    init_params = False
    if args.mode == 'idle':
        init_params = {'auto_loop': False}
    elif args.mode == 'loop':
        init_params = {'auto_loop': True}

    agent, runner = ocs_agent.init_site_agent(args)
    scheduler_loop = SchedulerLoopAgent(agent, init_params)

    agent.register_process('run_scheduler_loop', scheduler_loop.run_scheduler_loop, startup=init_params)

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()