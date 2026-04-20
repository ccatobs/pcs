#!/bin/python

'''
This agent is the interface between PrimeCam detector and the telescope control
system, with the objectives of sending commands to the antenna control unit
within the OCS framework for the telescope movements and simulataneously 
capturing 200Hz UDP stream containing position broadcast and store them into
PrimeCam HK system with g3 files.
The agent also captures lower freq. influx db stream for use in grafana
dashboard and quick look mapmaking

Majority of the codes is adapted from SO's ACU agent.
'''

import urllib.request, json, requests
import os, sys
import time, datetime
import queue
import argparse
import txaio
import random
from os import environ
import socket, struct
from autobahn.twisted.util import sleep as dsleep

from ocs import ocs_agent, site_config
#from aculib import status_keys
from pcs.agents.acu_interface import status_keys
from pcs.agents.acu_interface import aculib
from pcs.agents.acu_interface import drivers as sh
from ocs.ocs_twisted import TimeoutLock
from twisted.internet.defer import DeferredList, inlineCallbacks
from twisted.internet import protocol, reactor, threads

from astropy.coordinates import SkyCoord,EarthLocation,Angle
from astropy import units as u
from astropy.time import Time
import numpy as np

#
from threading import Thread

#import acu modules
from pcs.agents.acu_interface import aculib
from pcs.agents.acu_interface import drivers as drv

INIT_DEFAULT_SCAN_PARAMS = {
    'latp': {
        'az_speed': 2,
        'az_accel': 1,
        'el_freq': .15,
        'turnaround_method': 'standard',
        'el_mode': None,
    },
}

MONITOR_STRUCTURE = [
    ('ACU_summary_output', 'summary', 'tick', None),
    ('ACU_axis_faults', 'axis_faults_errors_overages', None, None),
    ('ACU_position_errors', 'position_errors', None, None),
    ('ACU_axis_limits', 'axis_limits', None, None),
    ('ACU_axis_warnings', 'axis_warnings', None, None),
    ('ACU_axis_failures', 'axis_failures', None, None),
    ('ACU_axis_state', 'axis_state', None, None),
    ('ACU_oscillation_alarm', 'osc_alarms', None, None),
    ('ACU_command_status', 'commands', None, None),
    ('ACU_general_errors', 'ACU_failures_errors', None, None),
    ('ACU_platform_status', 'platform_status', None, None),
    ('ACU_emergency', 'ACU_emergency', None, None),
    # ('ACU_corotator', 'corotator', None, None),
    # ('ACU_shutter', 'shutter', None, None),
    ('ACU_tilt', 'tilt_slow', 'changed', 0.5),
    (None, 'tilt_fast', None, None),
    ('ACU_sun_avoidance', 'sun_avoidance', None, 1.),
    ('ACU_corrections', 'corrections', None, 10.),
    # ('ACU_hvac_data', 'hvac_data', None, 10.),
    # ('ACU_hvac_ctrl', 'hvac_ctrl', None, None),
    # ('ACU_hvac_faults', 'hvac_faults', None, None),
]

#: Maximum update time (in s) for "monitor" process data, even with no changes
MONITOR_MAX_TIME_DELTA = 2.

class ACUAgent:
    """Interface agent to send pointing commands to ACU and
    acquire UDP data streams.

    Parameters:
        config (str):
            The configuration file for the ACU containing settings parameters.
        device (str):
            Name of the ACU device, default is 'acu_sim' for ACU simulator.
        startup (bool):
            If True, immediately start the main monitoring processes
            for status and UDP data.
    """
    def __init__(self, agent, config, device='acu_sim', startup=False):
        self.agent = agent
        #logging
        self.log = agent.log
        #get the config settings
        self.config = aculib.load_config(config)
        self.acu_conf = self.config['devices'][device]
        #self.platform_type = self.config['devices']['platform']
        self.platform_type = self.acu_conf['platform']
        self.udp = self.acu_conf['streams']['main']
        self.udp_schema = aculib.get_stream_schema(self.udp['schema'])

        ###########################################################################
        # Tried to add self.acu_read with the observatory_control_system class in pcs aculib
        # Since it seems to have the same function as AcuControl

        self.acu_read = aculib.observatory_control_system(url=self.acu_conf['base_url'], 
                                                          log=self.log, 
                                                          server_cert=self.acu_conf['certs']['server_cert'],
                                                          client_cert=self.acu_conf['certs']['client_cert'],
                                                          client_key=self.acu_conf['certs']['client_key'],
                                                          verify_cert=False
                                                          )
        
        ###########################################################################

        #placeholder for data received from monitors
        # 'status' is populated by the monitor operation
        # 'broadcast' is populated by the udp stream
        self.data = {'status':{},
                     'broadcast':{}}

        for b, k, _, _ in MONITOR_STRUCTURE:
            if b is None:
                continue
            self.data['status'][k] = {}

        #_dsets = self.acu_config['_datasets']
        _dsets = self.config['datasets']
        '''
        datasets:
        ccat:
        'default_dataset': 'ccat'
        'datasets':
            - ['ccat',       'DataSets.StatusCCatDetailed8100']
            - ['general',    'DataSets.StatusGeneral8100']
            - ['extra',      'DataSets.StatusExtra8100']
            - ['third',      'DataSets.Status3rdAxis']
            - ['faults',     'DataSets.StatusDetailedFaults']
            - ['pointing',   'DataSets.CmdPointingCorrection']
            - ['spem',       'DataSets.CmdSPEMParameter']
            - ['weather',    'DataSets.CmdWeatherStation']
            - ['azimuth',    'Antenna.SkyAxes.Azimuth']
            - ['elevation',  'Antenna.SkyAxes.Elevation']
        '''
        self.datasets = {
            'status': _dsets.get('default_dataset'), # this grabs 'DataSets.StatusDetailed'
            # 'pointing': _dsets.get('pointing_dataset'),
        }
        for k, v in self.datasets.items():
            if v is not None:
                self.datasets[k] = dict(_dsets['datasets'])[v]
                self.log.info(f'self.datasets[k]:{self.datasets[k]}')

        #exclusive locks for telescope movements
        self.azel_lock = TimeoutLock()

        '''#########################################'''
        # Create a map from each status key (read through the
        # self.datasets) to the output block and field name.
        self.status_field_map = {}
        for group, group_fields in \
                status_keys.status_fields[self.platform_type]['status_fields'].items():
            for block_name, block_group, _, _ in MONITOR_STRUCTURE:
                if block_group == group:
                    break
            else:
                raise ValueError(f"status_key block '{group}' not found in MONITOR_STRUCTURE.")
            for acu_key, block_key in group_fields.items():
                self.status_field_map[acu_key] = (group, block_name, block_key)

        # Motion limits (az / el / third ranges).
        # self.motion_limits = self.acu_config['motion_limits']
        # if min_el:
        #     self.log.warn(f'Override: min_el={min_el}')
        #     self.motion_limits['elevation']['lower'] = min_el
        # if max_el:
        #     self.log.warn(f'Override: max_el={max_el}')
        #     self.motion_limits['elevation']['upper'] = max_el
        '''#########################################'''

        # Scan params (default vel / accel / el freq).
        self.default_scan_params = \
            dict(INIT_DEFAULT_SCAN_PARAMS[self.platform_type])
        for _k in self.default_scan_params.keys():
            #_v = self.acu_config.get('scan_params', {}).get(_k)
            _v = self.config.get('scan_params', {}).get(_k)
            if _v is not None:
                self.default_scan_params[_k] = _v
        agent.log.info('On startup, default scan_params={scan_params}',
                       scan_params=self.default_scan_params)
        self.scan_params = dict(self.default_scan_params)

        #register processes
        agent.register_process('broadcast',
                               self.broadcast,
                               self._simple_process_stop,
                               blocking=False,
                               startup=startup)

        agent.register_process('execute_scan',
                               self.execute_scan,
                               self._simple_process_stop,
                               blocking=False,
                               startup=False)

        agent.register_process('monitor',
                        self.monitor,
                        self._simple_process_stop,
                        blocking=False,
                        startup=startup)

        #register tasks
        agent.register_task('go_to',
                            self.go_to,
                            blocking=True,
                            aborter=self._simple_task_abort)
        agent.register_task('az_scan',
                            self.az_scan,
                            blocking=False,
                            aborter=self._simple_task_abort)
        agent.register_task('fromfile_scan',
                            self.fromfile_scan,
                            blocking=False,
                            aborter=self._simple_task_abort)
        #agg. params
        basic_agg_params = {'frame_length': 60}
        fullstatus_agg_params = {'frame_length': 60,
                                 'exclude_influx': True,
                                 'exclude_aggregator': False}

        influx_agg_params = {'frame_length': 60,
                             'exclude_influx': False,
                             'exclude_aggregator': True}

        #register data feeds
        # this is what the Monitor function talks to
        agent.register_feed('acu_status',
                            record=True,
                            agg_params=fullstatus_agg_params,
                            buffer_time=1)

        agent.register_feed('acu_udp_stream',
                            record=True,
                            agg_params=fullstatus_agg_params,
                            buffer_time=1)
        agent.register_feed('acu_error',
                            record=True,
                            agg_params=basic_agg_params,
                            buffer_time=1)
    
    #@inlineCallbacks
    def _simple_task_abort(self, session, params):
        # Trigger a task abort by updating state to "stopping"
        #yield session.set_status('stopping')
        if session.status == 'running':
            session.set_status('stopping')

    @inlineCallbacks
    def _simple_process_stop(self, session, params):
        # Trigger a process stop by updating state to "stopping"
        yield session.set_status('stopping')

    @inlineCallbacks
    def monitor(self, session, params):
        """monitor()

        **Process** - Refresh the cache of SATP ACU status information and
        report it on the 'acu_status' and 'acu_status_influx' HK feeds.

        Summary parameters are ACU-provided time code, Azimuth mode,
        Azimuth position, Azimuth velocity, Elevation mode, Elevation position,
        Elevation velocity, Boresight mode, and Boresight position.

        The session.data of this process is a nested dictionary.
        Here's an example::

            {
            "StatusDetailed": {
                "Time": 81.661170959322,
                "Year": 2023,
                "Azimuth mode": "Stop",
                "Azimuth commanded position": -20.0012,
                "Azimuth current position": -20.0012,
                "Azimuth current velocity": 0.0002,
                "Azimuth average position error": 0,
                "Azimuth peak position error": 0,
                "Azimuth computer disabled": false,
                ...
            },

            "StatusResponseRate": 19.237531827325963,
            "PlatformType": "satp",
            "IgnoredAxes": [],
            "NamedPositions": {
                "home": [
                180,
                40
                ]
            },
            "DefaultScanParams": {
                "az_speed": 2.0,
                "az_accel": 1.0,
            },
            "connected": True,
            }

        """

        # Note that session.data will get scanned, to assign data to
        # feed blocks.  We make an explicit list of items to ignore
        # during that scan (not_data_keys).
        session.data = {'PlatformType': self.acu_conf['platform'],
                        'DefaultScanParams': self.scan_params,
                        'StatusResponseRate': 0.,
                        # 'IgnoredAxes': self.ignore_axes,
                        #'NamedPositions': self.named_positions,
                        'connected': False}
        not_data_keys = list(session.data.keys())


        last_complaint = 0
        ########################################################################################
        # I'm commenting this out for now since it doesn't seem to affect anything else

        # while True:
        #     try:
        #         version = yield self.acu_read.http.Version()
        #         break
        #     except Exception as e:
        #         if time.time() - last_complaint > 3600:
        #             errormsg = {'aculib_error_message': str(e)}
        #             self.log.error(str(e))
        #             self.log.error('monitor process failed to query version! Will keep trying.')
        #             last_complaint = time.time()
        #         yield dsleep(10)

        # self.log.info(version)

        ###########################################################################################
        session.data['connected'] = True

        # Numbering as per ICD.
        mode_key = {
            'Stop': 0,
            'Preset': 1,
            'ProgramTrack': 2,
            'Rate': 3,
            'SectorScan': 4,
            'SearchSpiral': 5,
            'SurvivalMode': 6,
            'StepTrack': 7,
            'GeoSync': 8,
            'OPT': 9,
            'TLE': 10,
            'Stow': 11,
            'StarTrack': 12,
            'SunTrack': 13,
            'MoonTrack': 14,
            'I11P': 15,
            'AutoTrack/Preset': 16,
            'AutoTrack/PositionMemory': 17,
            'AutoTrack/PT': 18,
            'AutoTrack/OPT': 19,
            'AutoTrack/PT/Search': 20,
            'AutoTrack/TLE': 21,
            'AutoTrack/TLE/Search': 22,

            # Currently we do not have ICD values for these, but they
            # are included in the output of Meta.  ElSync, at least,
            # is a known third axis mode for the LAT.
            'ElSync': 100,
            'UnStow': 101,
            'MaintenanceStow': 102,
        }

        # fault_key digital values taken from ICD (correspond to byte-encoding)
        fault_key = {
            'No Fault': 0,
            'Warning': 1,
            'Fault': 2,
            'Critical': 3,
            'No Data': 4,
            'Latched Fault': 5,
            'Latched Critical Fault': 6,
        }
        pin_key = {
            # Capitalization matches strings in ACU binary, not ICD.
            # Are these needed for the SAT still?
            'Any Moving': 0,
            'All Inserted': 1,
            'All Retracted': 2,
            'Failure': 3,
        }
        lat_pin_key = {
            # From "meta" output.
            'Moving': 0,
            'Inserted': 1,
            'Retracted': 2,
            'Error': 3,
        }
        tfn_key = {'None': float('nan'),
                    'False': 0,
                    'True': 1,
                    }
        report_t = time.time()
        report_period = 20
        n_ok = 0
        min_query_period = 0.05   # Seconds
        query_t = 0

        # Assist monitoring and logging changes in certain fields.
        checkdata = [
            ('summary', 'ctime'),
            ('platform_status', 'Remote_mode'),
            ('summary', 'Azimuth_mode'),
            ('summary', 'Elevation_mode'),
            # ('summary', 'Boresight_mode'),
        ]
        prev_checkdata = {k: None for g, k in checkdata}

        @inlineCallbacks
        def _get_status():
            output = {}
            for short, collection in [
                    ('status', 'StatusGeneral8100'), # this is what's in session.data already
                    #('pointing', 'CmdPointingCorrection'), #we commented this out above for some reason (see line 119)
            ]:
                #if self.datasets[short]:
                 #   output[collection] = (
                  #      yield self.acu_read.Values(self.datasets[short]))
                #else:
                 #   output[collection] = {}

                # It looks like get_status has a similar function to Values
                # since it queries https://127.0.0.1:5600/api/v1/telescope/acu/status
                # so I commented the above out and replaced it
                # not working perfectly still so should look more into this
                output[collection] = (yield self.acu_read.get_status())
                # output[collection] = yield self.acu_read.Values(self.datasets[short])
                # self.log.info(f'_get_status() dict :{output[collection]}')
            return output

        #session.data['StatusResponseRate'] = n_ok / (query_t - report_t)
        session.data.update((yield _get_status())) # update() merges two dictionaries
        #session.data.update((yield self.acu_read.get_status()))
        for key,value in session.data.items():
            #self.log.info("session.data after _get_status")
            self.log.info(f'{key}:{value}')

        ''' based on the code above, I think _get_status() gets called 
            before session.data is set, so we can just use "output" 
            but it formats the dictionary differently
        '''
        # qual_pacer = Pacemaker(.1)

        # last_resp_rate = None
        data_blocks = {}
        # influx_blocks = {}
        unknown_fields = set()

        while session.status in ['running']:

            # THIS WHOLE SECTION IS JUST A WAY TO MAKE SURE THAT THEY ARE CONSISTENTLY GETTING STATUS MESSAGES 
            # AT A SPECIFIC CADENCE. THEY ARE USING A THING CALLED THE 'PACEMAKER' (SEE ABOVE), WHICH 
            # IS PART OF THE SO OCS CODE IN ocs.ocs_twisted
            # ITS NOT CLEAR WHAT THIS IS DOING THAT time.sleep() COULDN'T ALSO ACCOMPLISH, SO 
            # I'M REMOVING IT FOR NOW
            ''' ####################################################################################
            now = time.time() 
            if now - query_t < min_query_period:
                yield dsleep(min_query_period - (now - query_t))

            query_t = time.time()
            if query_t > report_t + report_period:
                resp_rate = n_ok / (query_t - report_t)
                if last_resp_rate is None or (abs(resp_rate - last_resp_rate)
                                                > max(0.1, last_resp_rate * .01)):
                    self.log.info('Data rate for "monitor" stream is now %.3f Hz' % (resp_rate))
                    last_resp_rate = resp_rate
                report_t = query_t
                n_ok = 0
                session.data.update({'StatusResponseRate': resp_rate})

            if qual_pacer.next_sample <= time.time():
                # Publish UDP data health feed
                qual_pacer.sleep()  # should be instantaneous, just update counters
                bq = self._broadcast_qual
                bq_offset = bq['time_offset']
                if bq_offset is None:
                    bq_offset = 0.
                bq_ok = (bq['active'] and (now - bq['timestamp'] < 5)
                            and abs(bq_offset) < 1.)
                block = {
                    'timestamp': time.time(),
                    'block_name': 'qual0',
                    'data': {
                        'Broadcast_stream_ok': int(bq_ok),
                        'Broadcast_recv_offset': bq_offset,
                    }
                }
                self.agent.publish_to_feed('data_qual', block)
                ####################################################################################
                ''' 
            now = time.time() #now is used below so I pulled it out of the commented code block above

            try:
                session.data.update((yield _get_status()))
                #for key,value in session.data.items():
                    #self.log.info("session.data after _get_status")
                 #   self.log.info(f'{key}:{value}')
                #session.data.update((yield self.acu_read.get_status()))
                session.data['connected'] = True
                n_ok += 1
                last_complaint = 0
            except Exception as e:
                if now - last_complaint > 3600:
                    errormsg = {'aculib_error_message': str(e)}
                    self.log.error(str(e))
                    acu_error = {'timestamp': time.time(),
                                    'block_name': 'ACU_error',
                                    'data': errormsg
                                    }
                    self.agent.publish_to_feed('acu_error', acu_error)
                    last_complaint = time.time()
                    session.data['connected'] = False
                yield dsleep(1)
                continue

            # this section uses the status_field_map to match the key,val pairs from the MONITOR STRUCTURE 
            # and status_keys used by get_status() to parse the session.data and self.data dictionaries
            for k, v in session.data.items():
                if k in not_data_keys:
                    continue
                for (key, value) in v.items():
                    try:
                        group, block, field = self.status_field_map[key]
                    except KeyError:
                        if key not in unknown_fields:
                            self.log.warn(
                                'unknown status field (ignored hereafter): "%s"' % key)
                            unknown_fields.add(key)
                        continue
                    if block is None:
                        continue
                    # Cast value to saveable type.
                    if isinstance(value, bool):
                        value = int(value)
                    elif isinstance(value, int) or isinstance(value, float):
                        pass
                    elif value is None:
                        value = float('nan')
                    else:
                        value = str(value)
                    # Store.
                    #self.data['status'][k][v] = value
                    self.data['status'][group][field] = value
                #self.data[k][v] = value

            self.data['status']['summary']['ctime'] = \
                sh.timecode(self.data['status']['summary']['Time'])

            # Check for state changes in some key fields.
            new_checkdata = {k: self.data['status'][g].get(k)
                                for g, k in checkdata}

            if new_checkdata['Remote_mode'] != prev_checkdata['Remote_mode']:
                if new_checkdata['Remote_mode']:
                    self.log.warn('ACU now in remote mode.')
                else:
                    self.log.warn('ACU in local mode!')

            for axis_mode, v in new_checkdata.items():
                if 'mode' not in axis_mode or 'Remote' in axis_mode:
                    continue
                if v != prev_checkdata[axis_mode]:
                    self.log.info('{axis_mode} is now "{v}"',
                                    axis_mode=axis_mode, v=v)

            if new_checkdata['ctime'] == prev_checkdata['ctime']:
                self.log.warn('ACU time has not changed from previous data point!')
                continue

            prev_checkdata = new_checkdata


            '''
            ##########################################################
            I THINK THIS SECTION IS FOR INFLUX PUBLISHING WHICH WE ARE 
            ALREADY DOING THROUGH OUR 'broadcast' FUNCTION, SO I'M 
            COMMENTING IT OUT FOR NOW
            ##########################################################
            '''
            # influx_blocks are constructed based on refers to all
            # other self.data['status'] keys. Do not add more keys to
            # any self.data['status'] categories beyond this point

            # new_influx_blocks = {}
            # for category in self.data['status']:
            #     new_influx_blocks[category] = {
            #         'timestamp': self.data['status']['summary']['ctime'],
            #         'block_name': category,
            #         'data': {}}

            #     if category != 'commands':
            #         for statkey, statval in self.data['status'][category].items():
            #             if isinstance(statval, float):
            #                 influx_val = statval
            #             elif isinstance(statval, str):
            #                 for key_map in [tfn_key, mode_key, fault_key, pin_key,
            #                                 lat_pin_key]:
            #                     if statval in key_map:
            #                         influx_val = key_map[statval]
            #                         break
            #                 else:
            #                     raise ValueError('Could not convert value for %s="%s"' %
            #                                         (statkey, statval))
            #             elif isinstance(statval, int):
            #                 if statkey in ['Year', 'Free_upload_positions']:
            #                     influx_val = float(statval)
            #                 else:
            #                     influx_val = int(statval)
            #             new_influx_blocks[category]['data'][statkey + '_influx'] = influx_val
            #     else:  # i.e. category == 'commands':
            #         if str(self.data['status']['commands']['Azimuth_commanded_position']) != 'nan':
            #             acucommand_az = {'timestamp': self.data['status']['summary']['ctime'],
            #                                 'block_name': 'ACU_commanded_positions_az',
            #                                 'data': {'Azimuth_commanded_position_influx': self.data['status']['commands']['Azimuth_commanded_position']}
            #                                 }
            #             self.agent.publish_to_feed('acu_commands_influx', acucommand_az)
            #         if str(self.data['status']['commands']['Elevation_commanded_position']) != 'nan':
            #             acucommand_el = {'timestamp': self.data['status']['summary']['ctime'],
            #                                 'block_name': 'ACU_commanded_positions_el',
            #                                 'data': {'Elevation_commanded_position_influx': self.data['status']['commands']['Elevation_commanded_position']}
            #                                 }
            #             self.agent.publish_to_feed('acu_commands_influx', acucommand_el)
            #         if self.acu_config['platform'] == 'satp':
            #             if str(self.data['status']['commands']['Boresight_commanded_position']) != 'nan':
            #                 acucommand_bs = {'timestamp': self.data['status']['summary']['ctime'],
            #                                     'block_name': 'ACU_commanded_positions_boresight',
            #                                     'data': {'Boresight_commanded_position_influx': self.data['status']['commands']['Boresight_commanded_position']}
            #                                     }
            #                 self.agent.publish_to_feed('acu_commands_influx', acucommand_bs)

            # Only keep blocks that have changed or have new data.
            # block_keys = list(new_influx_blocks.keys())
            # for k in block_keys:
            #     if k not in influx_blocks:
            #         continue
            #     B, N = influx_blocks[k], new_influx_blocks[k]
            #     overdue = (N['timestamp'] - B['timestamp'] > MONITOR_MAX_TIME_DELTA)
            #     changes = any([B['data'][_k] != _v for _k, _v in N['data'].items()])
            #     if overdue or changes:
            #         continue
            #     del new_influx_blocks[k]

            # for block in new_influx_blocks.values():
            #     # Check that we have data (commands and corotator often don't)
            #     if len(block['data']) > 0:
            #         self.agent.publish_to_feed('acu_status_influx', block)
            # influx_blocks.update(new_influx_blocks)

            # Assemble data for aggregator ...
            new_blocks = {}
            for block_name, data_key, _, _ in MONITOR_STRUCTURE:
                if block_name is None:
                    continue
                new_blocks[block_name] = {
                    'timestamp': self.data['status']['summary']['ctime'],
                    'block_name': block_name,
                    'data': self.data['status'][data_key],
                }

            # Only keep blocks that have changed or have new data.
            for k, _, policy, delta in MONITOR_STRUCTURE:
                if k is None:
                    continue
                B, N = data_blocks.get(k), new_blocks[k]
                if len(N['data']) == 0:
                    del new_blocks[k]
                    continue
                if B is None:
                    continue
                if policy == 'tick':  # always store.
                    continue
                underdue = delta is not None and \
                    (N['timestamp'] - B['timestamp'] < delta)
                overdue = (N['timestamp'] - B['timestamp'] > MONITOR_MAX_TIME_DELTA) \
                    and not underdue
                changes = any([B['data'][_k] != _v for _k, _v in N['data'].items()])
                if (overdue and policy != 'changed') or changes and not underdue:
                    continue
                del new_blocks[k]

            for block in new_blocks.values():
                self.agent.publish_to_feed('acu_status', block)

            data_blocks.update(new_blocks)

        return True, 'Acquisition exited cleanly.'


    @ocs_agent.param('auto_enable', type=bool, default=True)
    @inlineCallbacks
    def broadcast(self, session, params): #this is called _udp_stream_handler in SOCS
        """broadcast(auto_enable=True)

        **Process** - Read UDP data from the port specified by
        self.acu_config, decode it, and publish to HK feeds.  Full
        resolution (200 Hz) data are written to feed "acu_udp_stream"
        while 1 Hz decimated are written to "acu_broadcast_influx".
        The 1 Hz decimated output are also stored in session.data.

        Args:
          auto_enable (bool): If True, the Process will try to
            configure and (re-)enable the UDP stream if at any point
            the stream seems to drop out.
        """
        FMT = self.udp_schema['format']
        FMT_LEN = struct.calcsize(FMT)
        udp_host = self.acu_conf['interface_ip']
        udp_port = self.udp['port']

        #data holder for queue
        udp_data = []
        fields = self.udp_schema['fields']
        session.data = {}

        #define the parsing datagram class method
        class MonitorUDP(protocol.DatagramProtocol):
            def datagramReceived(self, data, src_addr):
                now = time.time()
                host, port = src_addr
                offset = 0
                while len(data) - offset >= FMT_LEN:
                    d = struct.unpack(FMT, data[offset:offset+FMT_LEN])
                    udp_data.append((now, d))
                    offset += FMT_LEN

        #inistantiate twisted reactor data parsing
        handler = reactor.listenUDP(int(udp_port), MonitorUDP())

        #set up data holder for influx db
        influx_data = {}
        influx_data['Time_bcast_influx'] = []
        for i in range(2, len(fields)):
            influx_data[fields[i].replace(' ', '_') + '_bcast_influx'] = []

        self.log.info(f"Listening for UDP data on {udp_host}:{udp_port}")

        #some flags
        best_dt = None
        active = True
        last_packet_time = time.time()

        #start data acquisition loop
        while session.status in ['running']:
            now = time.time()
            #check if the data stream is at least 1s long (200Hz)
            if len(udp_data)>=200:
                if not active:
                    self.log.info('UDP packets are being received.')
                    active = True
                last_packet_time = now
                best_dt = None

                #start processing the stream with 1s chunks
                process_data = udp_data[:200]
                udp_data = udp_data[200:]
                for recv_time, d in process_data:
                    #convert timestamps in unix time
                    data_ctime = drv.timecode(d[0] + d[1] / drv.DAY)
                    if best_dt is None or abs(recv_time - data_ctime) < best_dt:
                        best_dt = recv_time - data_ctime

                    self.data['broadcast']['Time'] = data_ctime
                    influx_data['Time_bcast_influx'].append(data_ctime)
                    for i in range(2, len(d)):
                        self.data['broadcast'][fields[i].replace(' ', '_')] = d[i]
                        influx_data[fields[i].replace(' ', '_') + '_bcast_influx'].append(d[i])
                    #test >
                    #print (f"Timestamp: {data_ctime} --- Az: {self.data['broadcast']['Azimuth']} --- El:{self.data['broadcast']['Elevation']} ")

                    #put together the data block to be written
                    acu_udp_stream = {'timestamp': self.data['broadcast']['Time'],
                                      'block_name': 'ACU_broadcast',
                                      'data': self.data['broadcast']
                                      }
                    self.agent.publish_to_feed('acu_udp_stream', acu_udp_stream)
                influx_means = {}
                for key in influx_data.keys():
                    influx_means[key] = np.mean(influx_data[key])
                    influx_data[key] = []
                acu_broadcast_influx = {'timestamp': influx_means['Time_bcast_influx'],
                                        'block_name': 'ACU_bcast_influx',
                                        'data': influx_means,
                                        }
                #TODO: publish to influx feed and test
                sd = {}
                for ky in influx_means:
                    sd[ky.split('_bcast_influx')[0]] = influx_means[ky]
                session.data.update(sd)
            else:
                # Consider logging an outage, attempting reconfig.
                if active and now - last_packet_time > 3:
                    self.log.info('No UDP packets are being received.')
                    active = False
                    next_reconfig = time.time()
                if not active and params['auto_enable'] and next_reconfig <= time.time():
                    self.log.info('Requesting UDP stream enable.')
                    try:
                        handler = reactor.listenUDP(int(udp_port), MonitorUDP())
                    except Exception as err:
                        self.log.info('Exception while trying to enable stream: {err}', err=err)
                    next_reconfig += 60

            yield dsleep(0.01)

        handler.stopListening()
        #self.agent.feeds['acu_udp_stream'].flush_buffer()
        return True, 'Acquisition exited cleanly.'

    @ocs_agent.param('az', type=float)
    @ocs_agent.param('el', type=float)
    def go_to(self, session, params):
        """go_to(az, el)

        **Task** - Move the telescope to a particular point (azimuth,
        elevation) in Preset mode. When motion has ended and the telescope
        reaches the preset point, it returns to Stop mode and ends.

        Parameters:
            az (float): destination angle for the azimuth axis
            el (float): destination angle for the elevation axis

        """
        with self.azel_lock.acquire_timeout(0, job='go_to') as acquired:
            if not acquired:
                return False, f"Operation failed: {self.azel_lock.job} is running."

            #TODO: Add all prechecks before executing telescope motion
            self.log.info('Clearing faults to prepare for motion.')

            target_az = params['az']
            target_el = params['el']

            self.log.info(f'Requested position: az={target_az}, el={target_el}')
            
            #perform telescope move
            certs = self.acu_conf['certs']
            tcs = aculib.observatory_control_system(
                    self.acu_conf['base_url'],
                    self.log,
                    server_cert=certs['server_cert'],
                    client_cert=certs['client_cert'],
                    client_key=certs['client_key'],
                    verify_cert=certs['verify']
                    )
            self.log.info('Executing telescope movement')
            msg = tcs.move_to(target_az,target_el)
            self.log.info(f"HTTP request executed with respose code: {msg.status_code}")


        return True, msg.text

    @ocs_agent.param('scan_params', type=dict)
    #def az_scan():
    def az_scan(self, session, params):
        """az_scan(start_time, turnaround_time, elevation, 
        speed, num_scans, azimuth_range)

        **Task** - Send telescope on an azimuth scan at a constant elevation.
        It can be executed at a future time with set speed and number of scan
        cycles.

        Parameters:
            scan_params (dict): Azimuth scan parameters with the following
                fields in the dictionary:
            {start_time (float): time in future to begin scan,
                in format %Y-%m-%dT%H:%M:%SZ
             turnaround_time (float): time to change scan direction in seconds
             elevation (float): elevation of telescope in deg
             speed (float): speed of scan in degrees/second
             num_scans (int): numbers of cycles of the azimuth scan
             azimuth_range (list): list with 2 floats containing range of azimuth
             }
        """
        with self.azel_lock.acquire_timeout(0, job='az_scan') as acquired:
            if not acquired:
                return False, f"Operation failed: {self.azel_lock.job} is running."

            #TODO: Add all prechecks before executing telescope motion
            self.log.info('Clearing faults to prepare for motion.')

            #perform telescope move
            certs = self.acu_conf['certs']
            tcs = aculib.observatory_control_system(
                    self.acu_conf['base_url'],
                    self.log,
                    server_cert=certs['server_cert'],
                    client_cert=certs['client_cert'],
                    client_key=certs['client_key'],
                    verify_cert=certs['verify']
                    )
            self.log.info('Executing telescope movement')
            msg = tcs.azimuth_scan(**params['scan_params'])
            self.log.info(f"HTTP request executed with respose code: {msg.status_code}")

        return True, msg.text


    @ocs_agent.param('scan_filename', type=str)
    #def fromfile_scan():
    def fromfile_scan(self, session, params):
        """fromfile_scan(scan_filename)

        **Task** - Send scan commands for a predefined arbitrary path which
            consists of sequence of points stored in a text file. Currently,
            scan points in only 'Horizon' coordinate system is implemented.

        Parameters:
            scan_filename (str): path of the file containing pair of az,el
                points in each line that the scan patter will go through.
        """
        with self.azel_lock.acquire_timeout(0, job='fromfile_scan') as acquired:
            if not acquired:
                return False, f"Operation failed: {self.azel_lock.job} is running."

            #TODO: Add all prechecks before executing telescope motion
            self.log.info('Clearing faults to prepare for motion.')

            #perform telescope move
            certs = self.acu_conf['certs']
            tcs = aculib.observatory_control_system(
                    self.acu_conf['base_url'],
                    self.log,
                    server_cert=certs['server_cert'],
                    client_cert=certs['client_cert'],
                    client_key=certs['client_key'],
                    verify_cert=certs['verify']
                    )
            self.log.info('Executing telescope movement')
            msg = tcs.scan_pattern_from_file(params['scan_filename'])

            self.log.info(f"HTTP request executed with respose code: {msg.status_code}")

        return True, msg.text

    def execute_scan():
        #this function plans to implement the automated scans for the telescope,
        #by coordinating with schedular and other factors like sun avoidance,
        #should be self-contained operation with both telescope movement commands
        #as well as DAQ controls
        pass

def add_agent_args(parser_in=None):
    if parser_in is None:
        parser_in = argparse.ArgumentParser()
    pgroup = parser_in.add_argument_group('Agent Options')
    pgroup.add_argument("--acu-config", type=str,
                        default="/work/pcam_ocs/ocs/agents/acu_interface/acu_config.yaml")
    pgroup.add_argument("--no-processes", action='store_true',
                        default=False)
    pgroup.add_argument("--device", type=str, default="acu-sim")
    
    return parser_in

def main(args=None):
    parser = add_agent_args()
    args = site_config.parse_args(agent_class='ACUAgent',
                                  parser=parser,
                                  args=args)
    agent, runner = ocs_agent.init_site_agent(args)
    _ = ACUAgent(agent, args.acu_config,
                 device = args.device,
                 startup=not args.no_processes)

    runner.run(agent, auto_reconnect=True)


if __name__=='__main__':
    main()
