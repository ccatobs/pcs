# OpsDB data registration agent

import argparse
import os
import time
import queue
from pathlib import Path

from ocs import ocs_agent, site_config
import fystplan.lib.interact_opsdb as db
from fystplan.lib.interact_opsdb import register_raw_fits_files as register_files

class OpsDBRegistrationAgent():

    def __init__(self, agent, detector_data_path, book_path):

        '''
        detector_data_path: relative path to detector data
        book_path: relative path to L0 books
        '''

        self.agent = agent
        self.log = agent.log

        self.detector_data_path = detector_data_path
        self.book_path = book_path

        self.obs_unit = None
        self.executed_obs_unit_id = None
        self.api_client = None

        self.source_location_id = None
        self.filelist = None

        self.job_queue = queue.Queue()

    @ocs_agent.param('sess_id', default='', type=str)
    def check_for_book(self, session, params):
        """
        Check that the L0 book for this observing session exists

        L0 book names follow the convention level0_{rfsoc_sess_id}.h5
        """

        # get rfsoc sess id passed in to function
        sess_id = params['sess_id']

        # define book title that this function will look for
        book_title = "level0_" + sess_id + ".h5"

        # define directory to check for book
        directory = Path(self.book_path)

        # check book_path until book exists
        while True:

            match = list(directory.glob(book_title))

            if match = True:
                continue

            else:
                time.sleep(1)

        return True, "Book found."


    @ocs_agent.param('placeholder', default='', type=str)
    def add_to_queue(self, session, params):
                """
        Add current observation for data registration queue.
        Data registration will only occur AFTER the L0 book is created.
        """
        job_packet = dict(params)
        self.job_queue.put(job_packet)
        return True, f"Job session {job_packet['placeholder']} successfully queued"

    @ocs_agent.param('obs_unit', default='', type=str)
    @ocs_agent.param('executed_obs_unit_id', default='', type=str)
    @ocs_agent.param('api_client', default='', type=str)
    @ocs_agent.param('source_location_id', default='', type=int)
    @ocs_agent.param('filelist', default='', type=list)
    def check_and_register(self, session, params):
        """
        Register the RFSoC data with the associated executed_obs_unit in OpsDB.

        filelist should be a list of RFSoC and bookbound files including relative paths.

        bulk_data follows the RawDataFileBulkCreate schema as defined in the OpsDB API:

        bulk_data = {
            executed_obs_unit_id: str (uuid),
            source_location_id: int,
            files: list
        }

        where executed_obs_unit_id is the executed obs unit, source_location_id is an interger
        that defines the module computer the data files are located on, and files is a list
        of dictionaries that define each file to be registered, with the schema

        file = {
            name: str,
            relative_path: str,
            file_type: str,
            size: int,
            checksum: str,
            instrument_module_configuration_id: int,
            description: str,
            data_class: str
        }

        where instrument_module_configuration_id is an integer that defines the instrument module 
        and is retrieved from the obs unit.

        """

        log = db.get_logger()

        # get params
        self.obs_unit = params['obs_unit']
        self.executed_obs_unit_id = params['executed_obs_unit_id']
        self.api_client = params['api_client']
        self.source_location_id = params['source_location_id']
        self.filelist = params['filelist']

        # empty array for data files
        file_data_list = []

        # loop to retrieve information for all fields for each file
        for f in filelist:
            
            # retrieve instrument module config ids
            instr_module_configurations = obs_unit.instrument_module_configurations
            config_ids = [config.id for config in instr_module_configurations]

            # retrieve info for each file
            name = Path(f).name
            path = Path(f).parent
            file_type = Path(f).suffix.lstrip(".")
            size = Path(f).stat().st_size

            # build individual file dictionary according to schema
            file_data = {
                "name": name,
                "relative_path": path,  
                "file_type": file_type, # or could be HDF5 at this point... 
                "size" : size,
                "instrument_module_configuration_id": config_id,
            }

            # append to file list
            file_data_list.append(file_data)

        # build bulk_data dictionary to be passed to register_files
        bulk_data = {
            "executed_obs_unit_id": executed_obs_unit_id,
            "source_location_id": source_location_id,
            "files": file_data_list,
        }

        # use register_files to register files with OpsDB
        register_files(bulk_data, api_client=api_client)
        log.info(f"Scan {scannum}:{subscannum} fits files were registered to OpsDB")

    def build_filelist(self, session, params):

        # grab raw RFSoC data files

        # grab bookbound files

        return filelist

    @ocs_agent.param('sess_id', default='', type=str)
    def full_data_process(self, session, params):
        '''
        Run check_for_book, then build_filelist, then check_and_register for each queue item
        '''

        # check that bound L0 book exists

        log.info(f"Checking for book with session id {sess_id}...")

        check_for_book(sess_id)

        log.info(f"Found book with session id {sess_id}.")

        # build file list with L0 book h5 files and RFSoC data g3 files

        log.info(f"Building file list to be registered...")

        all_files = build_filelist()

        log.info(f"File list built.")

        # register the files with OpsDB

        log.info("Registering with OpsDB...")

        check_and_register(obs_unit, executed_obs_unit_id, api_client, source_location_id, filelist = all_files)




    def run_register_queue(self, session, params):
        '''
        Run the full_data_process on each item in the queue
        '''

        session.set_status('running')
        self.log.info('Data registration started')
        #
        while session.status == 'running':
            try:
                job = self.job_queue.get(timeout=1.0)
                print(time.time(), job)
            except queue.Empty:
                continue

            status, message = self.full_data_process()

            
def make_parser(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser()

    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--mode', type=str, choices=['idle', 'queue'],
                        help="Starting action for the agent.")

    return parser

def main(args=None):

    parser = make_parser()

    args = site_config.parse_args(agent_class='OpsDBRegistrationAgent', parser=parser, args=args)

    init_params = False
    # if args.mode == 'idle':
    #     init_params = {'start_queue': False}
    # elif args.mode == 'queue':
    #     init_params = {'start_queue': True}

    agent, runner = ocs_agent.init_site_agent(args)
    opsdb_registration = OpsDBRegistrationAgent(agent, init_params)

    # agent.register_task('check_and_register', opsdb_registration.check_and_register, startup=init_params)

    runner.run(agent, auto_reconnect=True)


if __name__ == '__main__':
    main()