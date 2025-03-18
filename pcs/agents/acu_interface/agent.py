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
import socket, struct, requests
from autobahn.twisted.util import sleep as dsleep

from ocs import ocs_agent, site_config
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
import aculib
import drivers as drv

# For logging
txaio.use_twisted()
LOG = txaio.make_logger()


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
        #get the config settings
        self.config = aculib.load_config(config)
        self.acu_conf = self.config['devices'][device]
        self.udp = self.acu_conf['streams']['main']
        self.udp_schema = aculib.get_stream_schema(self.udp['schema'])

        #placeholder for data received from monitors
        # 'status' is populated by the monitor operation
        # 'broadcast' is populated by the udp stream
        self.data = {'status':{},
                     'broadcast':{}}
        #logging
        self.log = agent.log

        #exclusive locks for telescope movements
        self.azel_lock = TimeoutLock()


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
        agent.register_feed('acu_status',
                            record=True,
                            agg_params=fullstatus_agg_params,
                            buffer_time=1)
        agent.register_feed('acu_udp_stream',
                            record=True,
                            agg_params=fullstatus_agg_params,
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

    @ocs_agent.param('auto_enable', type=bool, default=True)
    @inlineCallbacks
    def broadcast(self, session, params):
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
    def az_scan():
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
    def fromfile_scan():
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



