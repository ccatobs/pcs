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

import time
import argparse
import struct
from autobahn.twisted.util import sleep as dsleep

from ocs import ocs_agent, site_config
from pcs.agents.acu_interface import status_keys
from pcs.agents.acu_interface import aculib
from pcs.agents.acu_interface import drivers as drv
from ocs.ocs_twisted import TimeoutLock
from twisted.internet.defer import inlineCallbacks
from twisted.internet import protocol, reactor, threads
import numpy as np

# FYST typed scans: ocs-free dispatch cores + the completion
# latch and constants (so the Process poll loop and the unit-testable decision
# logic share one source of truth). See trajectory.py.
from fyst_trajectories import get_fyst_site
from pcs.agents.acu_interface.trajectory import (
    MAX_REFLOOR_DRIFT_SEC,
    SCAN_DISPATCH_BUFFER_SEC,
    TCS_NOTHING_TO_ABORT_MESSAGE,
    TCS_PROGRAM_TRACK_DRAINED,
    TCS_RELATIVE_TIME_MAX,
    TCS_SPEED_TOL,
    TCS_START_TOO_SOON_MESSAGE,
    ScanCompletionLatch,
    az_scan_duration_sec,
    az_scan_sun_breach,
    build_constant_el_payload,
    build_daisy_payload,
    build_pong_payload,
    build_source_payload,
    earth_orientation_note,
    mode_is_preset,
    mode_is_program_track,
    parse_az_scan_params,
    refloor_drift_seconds,
    refloor_payload_start_time,
    tcs_response_reason,
    tcs_response_status,
    unknown_scan_param_keys,
)

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
    ('ACU_sun_avoidance', 'sun_avoidance', None, 1.),
    ('ACU_corrections', 'corrections', None, 10.),
    ('ACU_shutter', 'shutter', None, 1.),
    ('ACU_hvac', 'hvac', None, 60.),
    ('ACU_faults', 'faults', None, None),
]

#: Maximum update time (in s) for "monitor" process data, even with no changes
MONITOR_MAX_TIME_DELTA = 2.

#: Minimum time (in s) between two "broadcast" reports of datagrams in another record layout
BROADCAST_LAYOUT_LOG_INTERVAL = 60.

# ---------------------------------------------------------------------------
# FYST typed-scan and command tunables (dispatch, slew gate, completion, abort).
# Soft constants, adjust against commissioning experience.
# ---------------------------------------------------------------------------
SCAN_COMPLETION_POLL_SEC = 1.0   # cadence of the post-POST completion poll
SCAN_SETTLE_SEC = 10.0           # time backstop past the computed scan end
                                 # (SO uses a 20 s graceful-stop window; 10 s is
                                 # a lighter backstop since the stack-drained
                                 # signal normally fires first)
#: The Go TCS checks each point as it uploads it, after its HTTP 200, and a refused
#: point ends the upload; the batches already on the stack still play. A stack
#: drained more than this before the scheduled end fails the scan as truncated; no
#: points on the stack this long after the start fails it as never uploaded.
SCAN_EARLY_DRAIN_TOL_SEC = 30.0

#: Consecutive reads of points left with no axis in ProgramTrack that fail a scan
#: as interrupted; a single read can fall within a mode change.
TRACK_LEFT_CONFIRM_READS = 2

#: How long (s) the abort path waits for the ACU to leave ProgramTrack after the
#: move-to that stops a held track. The Go TCS starts a move-to as soon as its loop
#: takes it, and the ACU changes mode within a tick of its own.
HELD_TRACK_STOP_WAIT_SEC = 3.0
HELD_TRACK_STOP_POLL_SEC = 0.5

SLEW_ARRIVAL_TOL_DEG = 0.05  # looser than positionTol (TCS b7bbbf9); the speed test and busy retry cover it
SLEW_STATUS_POLL_SEC = 1.0   # each poll is a status read through the OCS API
SLEW_TIMEOUT_SEC = 180.0     # > the Go TCS's own move-to timeout, ~133 s (estimateMoveTime at TCS b7bbbf9)
#: Largest raw-minus-sky difference (deg) on either axis that the slew gate takes
#: as the Go TCS's own correction of the move-to target (Sky2Raw at TCS b7bbbf9:
#: refraction plus the pointing-model offsets), so a commanded position farther
#: from the target is a stale preset. No modulo: a commanded azimuth one wrap
#: away is a different encoder position and must not pass.
POINTING_CORRECTION_MAX_DEG = 0.5

#: The relative ``az_scan`` start_time sent unfloored. On Go TCS ``b5666ae`` and
#: later (``moveThenStartPattern``, on master ``b7bbbf9``) a 0 starts the pattern
#: about 10 s after the TCS's own move to the scan's start; earlier builds refuse
#: it with 400 ("program track starts too soon").
AZ_SCAN_START_AFTER_MOVE = 0.0

# Retry of a move-to or /path POST refused as busy (tcs_response_status None).
# The Go TCS runs one command at a time and notices a finished one only at its
# 1 Hz status tick, so a command sent just after the previous one ends can still
# find it running. With the 500 ms queue wait, attempts start about 1.25 s apart.
TCS_BUSY_RETRY_MAX = 5  # attempts, the first included
TCS_BUSY_RETRY_DELAY_SEC = 0.75

#: The typed scan ops that run as abortable Processes holding azel_lock. The
#: standalone ``abort`` task stops each so a running scan releases the lock (its
#: dispatch loop sees session status 'stopping' -> sends its own /abort and
#: returns). Deliberately NOT the always-on infrastructure Processes
#: (broadcast/monitor), which must keep running across an abort.
SCAN_PROCESS_OPS = ('constant_el_scan', 'source_scan', 'pong_scan', 'daisy_scan')

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
    def __init__(self, agent, config, device='acu_sim', startup=False, ignore_axes=None):
        self.agent = agent
        #logging
        self.log = agent.log
        #get the config settings
        self.config = aculib.load_config(config)
        self.acu_conf = self.config['devices'][device]
        #self.platform_type = self.config['devices']['platform']
        # The 'acu-sim' device block carries no 'platform' key, and 'latp' is the
        # only platform populated in status_keys.status_fields.
        self.platform_type = self.acu_conf.get('platform', 'latp')
        self.udp = self.acu_conf['streams']['main']
        self.udp_schema = aculib.get_stream_schema(self.udp['schema'])

        self.acu_read = aculib.observatory_control_system(url=self.acu_conf['base_url'],
                                                          log=self.log,
                                                          server_cert=self.acu_conf['certs']['server_cert'],
                                                          client_cert=self.acu_conf['certs']['client_cert'],
                                                          client_key=self.acu_conf['certs']['client_key'],
                                                          verify_cert=False,
                                                          readonly_url=self.acu_conf.get('readonly_url'),
                                                          )

        #placeholder for data received from monitors
        # 'status' is populated by the monitor operation
        # 'broadcast' is populated by the udp stream
        self.data = {'status':{},
                     'broadcast':{}}

        for b, k, _, _ in MONITOR_STRUCTURE:
            if b is None:
                continue
            self.data['status'][k] = {}

        _dsets = self.config['datasets'][self.platform_type]
        '''
        datasets:
        ccat:
        'default_dataset': 'ccat'
        'datasets':
            - ['ccat',       'DataSets.StatusCCatDetailed8100']
        '''
        self.datasets = {
            'status': _dsets.get('default_dataset'), # this grabs 'DataSets.StatusDetailed'
            'pointing': 'pointing',
            'shutter': 'shutter',
            'hvac': 'hvac',
            'faults': 'faults',
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

        # Axes to ignore.
        self.ignore_axes = self.acu_conf.get('ignore_axes', [])
        if ignore_axes is not None:
            self.ignore_axes = [x for x in ignore_axes if x != 'none']
        if len(self.ignore_axes):
            assert all([x in ['az', 'el', 'third'] for x in self.ignore_axes])
            agent.log.warn('Note ignore_axes={i}', i=self.ignore_axes)
        
        '''#########################################'''

        #register processes
        agent.register_process('broadcast',
                               self.broadcast,
                               self._simple_process_stop,
                               blocking=False,
                               startup=startup)

        agent.register_process('monitor',
                        self.monitor,
                        self._simple_process_stop,
                        blocking=False,
                        startup=startup)

        # FYST typed scan tasks. Registered as Processes (not
        # Tasks) so they are abortable mid-scan via _simple_process_stop.
        agent.register_process('constant_el_scan',
                               self.constant_el_scan,
                               self._simple_process_stop,
                               blocking=False,
                               startup=False)

        agent.register_process('source_scan',
                               self.source_scan,
                               self._simple_process_stop,
                               blocking=False,
                               startup=False)

        agent.register_process('pong_scan',
                               self.pong_scan,
                               self._simple_process_stop,
                               blocking=False,
                               startup=False)

        agent.register_process('daisy_scan',
                               self.daisy_scan,
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
                            blocking=True,
                            aborter=self._simple_task_abort)
        agent.register_task('fromfile_scan',
                            self.fromfile_scan,
                            blocking=False,
                            aborter=self._simple_task_abort)
        # FYST dedicated abort task. Standalone /abort escape
        # hatch (the shared _safe_abort path); safe to call with no scan
        # running. Mid-scan abort normally goes via stopping the scan Process.
        agent.register_task('abort',
                            self.abort,
                            blocking=False,
                            aborter=self._simple_task_abort)
        agent.register_task('stop_and_clear',
                            self.stop_and_clear,
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
        agent.register_feed('acu_status_influx',
                            record=True,
                            agg_params=influx_agg_params,
                            buffer_time=1)
        agent.register_feed('acu_udp_stream',
                            record=True,
                            agg_params=fullstatus_agg_params,
                            buffer_time=1)
        agent.register_feed('acu_broadcast_influx',
                            record=True,
                            agg_params=influx_agg_params,
                            buffer_time=1)
        agent.register_feed('acu_error',
                            record=True,
                            agg_params=basic_agg_params,
                            buffer_time=1)

        # The typed scans and az_scan's Sun check need astropy's Earth-orientation
        # data. Checked once, here, before the reactor runs (the check can block
        # while astropy tries a download), so a bad table shows at start, not at
        # the first scan; the agent starts either way.
        eop_ok, eop_note = earth_orientation_note(time.time())
        if eop_ok:
            self.log.info('{note}', note=eop_note)
        else:
            self.log.error('{note}', note=eop_note)
    
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

        **Process** - Refresh the cache of CCAT FYST ACU status information and
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
        session.data = {'connected': False}
        not_data_keys = list(session.data.keys())


        last_complaint = 0
        session.data['connected'] = True

        # Assist monitoring and logging changes in certain fields.
        checkdata = [
            ('summary', 'ctime'),
            ('platform_status', 'Remote_mode'),
            ('summary', 'Azimuth_mode'),
            ('summary', 'Elevation_mode'),
        ]
        prev_checkdata = {k: None for g, k in checkdata}

        try:
            status = yield threads.deferToThread(self.acu_read.get_status)
            session.data['StatusDetailed'] = status
        except (Exception, SystemExit) as e:
            self.log.error('monitor: initial status read failed: {e}', e=e)
            session.data['connected'] = False

        data_blocks = {}
        unknown_fields = set()

        while session.status in ['running']:
            now = time.time()

            try:
                status = yield threads.deferToThread(self.acu_read.get_status)
                session.data['StatusDetailed'] = status
                session.data['connected'] = True
                last_complaint = 0
            except (Exception, SystemExit) as e:
                if now - last_complaint > 3600:
                    errormsg = {'aculib_error_message': str(e)}
                    self.log.error('{e}', e=e)
                    acu_error = {'timestamp': time.time(),
                                    'block_name': 'ACU_error',
                                    'data': errormsg
                                    }
                    self.agent.publish_to_feed('acu_error', acu_error)
                    last_complaint = time.time()
                    session.data['connected'] = False
                yield dsleep(1)
                continue

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
                    if isinstance(value, bool):
                        value = int(value)
                    elif isinstance(value, int) or isinstance(value, float):
                        pass
                    elif value is None:
                        value = float('nan')
                    else:
                        value = str(value)
                    self.data['status'][group][field] = value

            self.data['status']['summary']['ctime'] = \
                status['timestamp'] / 1e9

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
                influx_block = {
                    'timestamp': block['timestamp'],
                    'block_name': block['block_name'],
                    'data': {k + '_influx': v for k, v in block['data'].items()},
                }
                if influx_block['data']:
                    self.agent.publish_to_feed('acu_status_influx', influx_block)

            data_blocks.update(new_blocks)
            yield dsleep(0.2) #prevents the HTTP 503 error

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
        log = self.log
        schema = self.udp['schema']
        layout_logged = float('-inf')

        #define the parsing datagram class method
        class MonitorUDP(protocol.DatagramProtocol):
            def datagramReceived(self, data, src_addr):
                nonlocal layout_logged
                now = time.time()
                host, port = src_addr
                # A datagram that is not whole records is another record layout:
                # unpacking it at this stride would shift every field.
                if len(data) % FMT_LEN:
                    if now - layout_logged >= BROADCAST_LAYOUT_LOG_INTERVAL:
                        layout_logged = now
                        log.error('broadcast: dropping {size}-byte datagrams, not a whole number of '
                                  '{record}-byte records of schema {schema}; the ACU is sending another '
                                  'record layout, and the schema is chosen by streams.main.schema of the device',
                                  size=len(data), record=FMT_LEN, schema=schema)
                    return
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
                # InfluxDB refuses a NaN, so a NaN mean stays out of the block; the ACU simulator
                # (from v0.7.0) sends its commanded position as NaN during a program track. An OCS
                # feed block keeps the fields of its first message (ocs Block.append raises on
                # another set), so the block name carries the fields left out.
                finite = {k: v for k, v in influx_means.items() if np.isfinite(v)}
                left_out = sorted(set(influx_means) - set(finite))
                acu_broadcast_influx = {'timestamp': influx_means['Time_bcast_influx'],
                                        'block_name': '-'.join(['ACU_bcast_influx'] + left_out),
                                        'data': finite,
                                        }
                self.agent.publish_to_feed('acu_broadcast_influx', acu_broadcast_influx)
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
                        tcs_reconfig = self._make_tcs()
                        yield threads.deferToThread(
                            tcs_reconfig.session.post,
                            f"{self.acu_conf['base_url']}/api/v1/telescope/acu/position-broadcast",
                            json={'destination_host': udp_host,
                                  'destination_port': int(udp_port)},
                            verify=False,
                            timeout=aculib.TCS_HTTP_TIMEOUT,
                        )
                    except Exception as err:
                        self.log.info('Exception while trying to enable stream: {err}', err=err)
                    next_reconfig += 60

            yield dsleep(0.01)

        handler.stopListening()
        #self.agent.feeds['acu_udp_stream'].flush_buffer()
        return True, 'Acquisition exited cleanly.'

    ###########################################
    #currently needed for stop_and_clear to work

    @inlineCallbacks
    def _set_modes(self, az=None, el=None, third=None):
        """Helper for changing individual axis modes.  Respects ignore_axes.

        When setting one axis it is often necessary to write others as
        well.  The current mode is first queried, and written back
        unmodified.

        """
        modes = list((yield self.acu_control.mode(size=3)))
        changes = [False, False, False]
        for i, (k, v) in enumerate([('az', az), ('el', el), ('third', third)]):
            if k not in self.ignore_axes and v is not None:
                changes[i] = True
                modes[i] = v
        if not any(changes):
            return
        if not changes[2]:
            yield self.acu_control.mode(modes[:2])
        else:
            yield self.acu_control.mode(modes)

    @inlineCallbacks
    def _stop(self, all_axes=False):
        """Helper for putting all axes in Stop.  This will normally just issue
        acu_control.stop(); but if any axes are being "ignored", and
        the user has not passed all_axes=True, then it will avoid
        changing the mode of those axes.

        """
        if all_axes or len(self.ignore_axes) == 0:
            yield self.acu_control.stop()
            return
        yield self._set_modes('Stop', 'Stop', 'Stop')

    ######################################################

    @ocs_agent.param('az', type=float)
    @ocs_agent.param('el', type=float)
    def go_to(self, session, params):
        """go_to(az, el)

        **Task** - Move the telescope to a particular point (azimuth,
        elevation) in Preset mode. The task returns once the Go TCS has queued
        the move, without waiting for it to start or finish.

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
            try:
                msg = tcs.move_to(target_az,target_el)
            except SystemExit as e:
                # aculib.post raises SystemExit on a requests error. No /abort:
                # this task holds azel_lock only for the call.
                self.log.error('go_to: move-to failed in transport: {e}', e=e)
                return False, f'go_to: move-to failed in transport ({e}); whether the dish moved is unknown.'
            code = tcs_response_status(msg)
            self.log.info(f"HTTP request executed with response code: {code}")
            if code is not None and code >= 500:
                # The OCS API failed, so the move may have reached the TCS.
                return False, (
                    f"go_to: move-to failed at the OCS API "
                    f"({tcs_response_reason(msg)}); whether the dish moved is unknown.")
            if code != 200:
                return False, (
                    f"go_to: move-to refused "
                    f"({tcs_response_reason(msg)}); not moved.")

        return True, getattr(msg, "text", "")

    @ocs_agent.param('scan_params', type=dict)
    def az_scan(self, session, params):
        """az_scan(start_time, turnaround_time, elevation, 
        speed, num_scans, azimuth_range)

        **Task** - Send telescope on an azimuth scan at a constant elevation.
        It can be executed at a future time with set speed and number of scan
        cycles.

        Parameters:
            scan_params (dict): Azimuth scan parameters with the following
                fields in the dictionary:
            {start_time (float or str): time in future to begin scan, as Unix
                seconds (below TCS_RELATIVE_TIME_MAX, 1e5: seconds from now) or
                an ISO-8601 UTC string, %Y-%m-%dT%H:%M:%SZ (no offset: UTC);
                floored to SCAN_DISPATCH_BUFFER_SEC ahead, except a relative 0
                (AZ_SCAN_START_AFTER_MOVE), sent as is. Go TCS b5666ae and later
                move the dish to the scan's start first: 0 starts the pattern
                about 10 s after that move, and any other start must leave the
                move time plus 5 s, or the scan fails after the TCS has answered.
             turnaround_time (float): time to change scan direction in seconds
             elevation (float): elevation of telescope in deg
             speed (float): speed of scan in degrees/second
             num_scans (int): numbers of cycles of the azimuth scan
             azimuth_range (list): list with 2 floats containing range of azimuth
             }

        Before sending, the whole arc, from now (the TCS first moves to the
        range start and holds there) to the pattern's end, is checked against
        the site's Sun exclusion zone; the move to the range start is not
        checked. A sweep that comes within its radius, or a scan_params without
        the six keys, with any other key, or whose pattern keys do not read as
        finite numbers (speed positive, num_scans a whole number of at least 0,
        turnaround_time at least 0), is refused and not sent.
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
            try:
                (scan_params, azimuth_range, elevation, speed, num_scans,
                 turnaround_time) = parse_az_scan_params(params['scan_params'])
            except ValueError as e:
                return False, str(e)
            start_time = scan_params['start_time']

            # Floored to SCAN_DISPATCH_BUFFER_SEC ahead in its own units (see
            # TCS_RELATIVE_TIME_MAX), except a relative 0 (AZ_SCAN_START_AFTER_MOVE).
            now = time.time()
            absolute = start_time >= TCS_RELATIVE_TIME_MAX
            if start_time == AZ_SCAN_START_AFTER_MOVE:
                # A 0 starts 10 s after the TCS's move, which it times out at its own
                # estimate, at most ~133 s (estimateMoveTime at TCS b7bbbf9); SLEW_TIMEOUT_SEC
                # covers both.
                start_unix = now + SLEW_TIMEOUT_SEC
            elif absolute:
                start_unix = max(start_time, now + SCAN_DISPATCH_BUFFER_SEC)
            else:
                scan_params['start_time'] = max(start_time, SCAN_DISPATCH_BUFFER_SEC)
                start_unix = now + scan_params['start_time']
            breach = az_scan_sun_breach(
                azimuth_range=azimuth_range,
                elevation=elevation,
                start_unix=start_unix,
                duration_sec=az_scan_duration_sec(
                    azimuth_range, speed, num_scans, turnaround_time),
                now_unix=now,
                site=get_fyst_site(),
            )
            if breach is not None:
                lo, hi = azimuth_range
                refusal = (
                    f"az_scan: the sweep from az {lo:g} to {hi:g} at el {elevation:g} comes "
                    f"within {breach['separation']:.1f} deg of the Sun at {breach['time']} "
                    f"(exclusion radius {breach['radius']:.1f} deg); not launched.")
                self.log.warn('{refusal}', refusal=refusal)
                return False, refusal
            if absolute:
                # Floored last: the Go TCS times the lead from its receipt.
                scan_params['start_time'] = max(start_time, time.time() + SCAN_DISPATCH_BUFFER_SEC)
            if scan_params['start_time'] != start_time:
                self.log.info('az_scan: floored start_time {start} -> {floored} for the Go TCS '
                              'minimum lead', start=start_time, floored=scan_params['start_time'])

            self.log.info('Executing telescope movement')
            try:
                msg = tcs.azimuth_scan(**scan_params)
            except SystemExit as e:
                # As in go_to.
                self.log.error('az_scan: azimuth-scan failed in transport: {e}', e=e)
                return False, (f'az_scan: azimuth-scan failed in transport ({e}); whether it '
                               f'launched is unknown.')
            code = tcs_response_status(msg)
            self.log.info(f"HTTP request executed with response code: {code}")
            if code is not None and code >= 500:
                # As in go_to; the scan may have reached the TCS.
                return False, (
                    f"az_scan: azimuth-scan failed at the OCS API "
                    f"({tcs_response_reason(msg)}); whether it launched is unknown.")
            if code != 200:
                hint = ''
                if (scan_params.get('start_time') == AZ_SCAN_START_AFTER_MOVE
                        and TCS_START_TOO_SOON_MESSAGE in tcs_response_reason(msg)):
                    hint = (' A start_time of 0 needs Go TCS b5666ae or later; '
                            'give a start time instead.')
                return False, (
                    f"az_scan: azimuth-scan refused "
                    f"({tcs_response_reason(msg)}); not launched.{hint}")

        return True, getattr(msg, "text", "")


    @ocs_agent.param('scan_filename', type=str)
    def fromfile_scan(self, session, params):
        """fromfile_scan(scan_filename)

        **Task** - Send scan commands for a predefined arbitrary path which
            consists of sequence of points stored in a text file. Currently,
            scan points in only 'Horizon' coordinate system is implemented.

        Parameters:
            scan_filename (str): path of the text file holding the scan's
                points, one per line as five whitespace-separated numbers
                [t, az, el, vaz, vel]: seconds after the scan start, azimuth
                and elevation (deg) and their velocities (deg/s).
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
            try:
                msg = tcs.scan_pattern_from_file(params['scan_filename'])
            except SystemExit as e:
                # As in go_to; an unreadable scan file still raises.
                self.log.error('fromfile_scan: /path POST failed in transport: {e}', e=e)
                return False, (f'fromfile_scan: /path POST failed in transport ({e}); whether it '
                               f'launched is unknown.')
            code = tcs_response_status(msg)
            self.log.info(f"HTTP request executed with response code: {code}")
            if code is not None and code >= 500:
                # As in go_to; the path may have reached the TCS.
                return False, (
                    f"fromfile_scan: /path POST failed at the OCS API "
                    f"({tcs_response_reason(msg)}); whether it launched is unknown.")
            if code != 200:
                return False, (
                    f"fromfile_scan: /path POST refused "
                    f"({tcs_response_reason(msg)}); scan not launched.")

        return True, getattr(msg, "text", "")

    def _make_tcs(self):
        """Build a FRESH per-scan Go TCS client from this device's config.

        Each typed scan Process (and the standalone ``abort`` task) builds its own
        ``aculib.observatory_control_system`` instead of sharing ``self.acu_read``:
        ``requests.Session`` is not thread-safe, and the always-on ``monitor``
        Process (reactor thread) + the standalone ``abort`` task can hit the shared
        client's one Session concurrently with a scan's thread-pool calls. A fresh
        client per scan gives it its own Session/connection-pool, matching legacy
        ``go_to`` / ``az_scan`` / ``fromfile_scan``.

        Reads ``self.acu_conf['certs']`` defensively (``.get`` + per-field
        defaults), so the cert-less device blocks resolve to ``verify_cert=False``
        with empty cert paths instead of ``KeyError``-ing like the legacy builds.
        Empty cert paths route ``start_session`` down its cert-less branch
        (``session.verify = False``), correct for the
        plain-HTTP / loopback-self-signed devices.
        """
        certs = self.acu_conf.get('certs', {})
        return aculib.observatory_control_system(
            self.acu_conf['base_url'],
            self.log,
            server_cert=certs.get('server_cert', ''),
            client_cert=certs.get('client_cert', ''),
            client_key=certs.get('client_key', ''),
            verify_cert=certs.get('verify', False),
        )

    @staticmethod
    def _publish_phase(session, data, phase, **fields):
        """Record a typed scan's dispatch phase in ``session.data``.

        ``data`` is the scan's own dict, kept across calls, so ``session.data``, a
        copy of it, carries every field set so far.
        """
        data.update(fields)
        data['phase'] = phase
        data['timestamp'] = time.time()
        session.data = dict(data)

    @staticmethod
    def _abort_outcome(sent, note):
        """Say how a failure path's /abort went, for the task's failure text."""
        detail = f' ({note})' if note else ''
        if sent:
            return f'/abort sent{detail}'
        return f'/abort failed{detail}; the dish may still be moving'

    @staticmethod
    def _launch_abort_outcome(sent, note):
        """Say how the /abort after a command the Go TCS may have queued went."""
        if sent:
            return '; scan not launched' + (f' ({note})' if note else '')
        return f', but {note}; the dish may still be moving'

    @staticmethod
    def _stop_outcome(job, sent, note, where=''):
        """Return a typed scan's result when it stopped on request, from its /abort.

        ``where`` says when the stop came, in both texts.
        """
        if not sent:
            return False, (f'{job}: stop requested{where}, but /abort failed ({note}); the '
                           f'dish may still be moving.')
        detail = f' ({note})' if note else ''
        return True, f'{job} aborted{where}; /abort sent{detail}.'

    @staticmethod
    def _program_track_state(status):
        """Read one ACU status for a program track the dish is still playing.

        Returns ``(points, playing)``: the points on the stack beyond the one a
        drained stack keeps, and whether an axis is in ProgramTrack, each ``None``
        when the read lacks it. A track plays when ``points`` is positive and
        ``playing`` is not ``False``: the Go TCS's ``/abort`` only sets Stop
        (``Telescope.Stop`` at TCS ``b7bbbf9``), so points in Stop or Preset are
        a stopped track's leftovers.
        """
        free = status.get('QtyOfFreeProgramTrackStackPositions')
        points = None if free is None else max(0, TCS_PROGRAM_TRACK_DRAINED - int(free))
        az_mode = status.get('AzimuthMode')
        el_mode = status.get('ElevationMode')
        if az_mode is None and el_mode is None:
            playing = None
        else:
            playing = (mode_is_program_track(az_mode)
                       or mode_is_program_track(el_mode))
        return points, playing

    def _wait_for_track_to_stop(self, tcs):
        """Poll the ACU for up to ``HELD_TRACK_STOP_WAIT_SEC`` for its axes to leave ProgramTrack.

        Returns ``True`` once they have, ``False`` if the track still plays at the
        end, and ``None`` (unconfirmed) if no read carried the modes. Runs
        off-reactor, inside :meth:`_safe_abort`, so it sleeps between reads.
        """
        deadline = time.time() + HELD_TRACK_STOP_WAIT_SEC
        seen = None
        while True:
            try:
                _points, playing = self._program_track_state(tcs.get_status())
            except (Exception, SystemExit) as e:
                self.log.warn('/abort: status read while waiting for the track to stop failed: {e}', e=e)
            else:
                if playing is False:
                    return True
                if playing is not None:
                    seen = False
            if time.time() >= deadline:
                return seen
            time.sleep(HELD_TRACK_STOP_POLL_SEC)

    def _stop_held_track(self, tcs, status, points, message):
        """Stop a program track the Go TCS has let go of: a move-to to where the dish is.

        The Go TCS ends a command without a Stop on a failed ACU status read or a
        refused upload batch (its command loop in main.go at TCS ``b7bbbf9``), so the
        ACU plays on while ``/abort`` answers "nothing to abort". ``MoveTo`` sets Stop
        first, on a stack of any age; ``/reset`` (TCS ``1e3497f``) stops it too and resets
        ACU failures, where the OCS API relays that route (from OCS ``0c58dec``). A 200
        means queued only (the Go TCS drops a command while ``Telescope.Ready`` fails), so
        ``sent`` is ``True`` only once the axes have left ProgramTrack. Returns
        ``(sent, note)`` as :meth:`_safe_abort`.
        """
        held = f'{message}; ACU still playing a program track ({points} points on its stack)'
        try:
            az = float(status.get('AzimuthCurrentPosition'))
            el = float(status.get('ElevationCurrentPosition'))
        except (TypeError, ValueError):
            return False, f'{held}, and its position is not in the status, so it was not stopped'
        target = f'the encoder position read (az={az:.3f}, el={el:.3f})'
        answer = None  # None: the move-to was accepted; else what became of it
        accepted = False  # a move-to was accepted on some attempt
        busy = False
        stopped = None
        for attempt in (1, 2):
            try:
                reply = tcs.move_to(az, el)
            except (Exception, SystemExit) as e:
                answer = f'the move-to to stop it failed in transport ({e})'
            else:
                code = tcs_response_status(reply)
                busy = code is None
                if code == 200:
                    answer = None
                    accepted = True
                elif code is None:
                    answer = 'the move-to to stop it met a busy Go TCS'
                elif code >= 500:
                    # An OCS API or intermediary failure: the move-to may have been taken.
                    answer = f'the move-to to stop it got no Go TCS answer ({tcs_response_reason(reply)})'
                else:
                    answer = f'the move-to to stop it was refused ({tcs_response_reason(reply)})'
            stopped = self._wait_for_track_to_stop(tcs)
            if stopped:
                # Unless the move-to was accepted, which command stopped the track is unknown.
                how = (f'via a move-to at {target}' if answer is None
                       else f'after {answer} (by that move-to if it was taken, or by a '
                            f'concurrent command)')
                self.log.warn('/abort: held program track stopped {how}', how=how)
                return True, f'{message}; held program track ({points} points) stopped {how}'
            if attempt == 1 and (answer is None or busy):
                continue  # once more: a move-to the Go TCS dropped, or one refused as busy
            break
        if stopped is None:
            what = answer
            if answer is None:
                what = f'a move-to at {target} was sent'
            elif accepted:
                what = f'a move-to at {target} was accepted, then {answer}'
            note = (f'{held}; {what} but no later status read showed the axis modes, so the '
                    f'stop is unconfirmed')
        elif answer is None:
            note = f'{held}; the move-to at {target} was accepted but the ACU still plays the track'
        else:
            note = f'{held}; {answer}'
        self.log.warn('/abort: {note}', note=note)
        return False, note

    def _safe_abort(self, tcs):
        """Send a Go TCS ``/abort`` and stop a program track it finds still playing.

        Never raises: ``aculib``'s ``abort()`` raises ``SystemExit`` on a transport
        error (from ``post()``), ``RuntimeError`` on an HTTP 503 and ``ValueError`` on
        a body that is not JSON, so ``(Exception, SystemExit)`` is trapped, not
        ``BaseException``. A ``"status": "error"`` answer is a refusal (the OCS API
        relays it with HTTP 200), except :data:`TCS_NOTHING_TO_ABORT_MESSAGE`: no
        command was running, but a held track may still play, so a status read checks
        (:meth:`_stop_held_track`). Every typed scan's stop path and the ``abort`` task
        use this. Returns ``(sent, note)``: ``sent`` only if the TCS took the
        ``/abort``, or had nothing to abort and no track plays; ``note`` says why
        (empty on a plain success).
        """
        try:
            reply = tcs.abort()
        except (Exception, SystemExit) as e:
            self.log.warn('/abort failed (continuing cleanup): {e}', e=e)
            note = 'no answer from the TCS (the abort may have taken effect); see log'
            try:
                points, playing = self._program_track_state(tcs.get_status())
            except (Exception, SystemExit):
                return False, note
            if playing is False:
                note += '; the ACU reports both axes out of program track'
            elif points:
                note += f'; the ACU still reports a program track ({points} points)'
            elif playing and points is None:
                note += '; the ACU reports program track and its stack count is missing'
            elif playing:
                note += '; the ACU is in program track with no points left'
            return False, note
        raw = reply
        reply = reply if isinstance(reply, dict) else {}
        message = str(reply.get('message') or '')
        if reply.get('status') == 'error' and message != TCS_NOTHING_TO_ABORT_MESSAGE:
            self.log.warn('/abort refused by the Go TCS: {message}', message=message)
            return False, f'refused: {message}'
        if message == TCS_NOTHING_TO_ABORT_MESSAGE:
            status = None
            for _ in range(2):  # one retry: a flaky ACU read is the state a held track arises in
                try:
                    status = tcs.get_status()
                    break
                except (Exception, SystemExit) as e:
                    self.log.warn('/abort: status read after "nothing to abort" failed: {e}', e=e)
            if status is None:
                return False, (f'{message}; the ACU status could not be read, so whether a program '
                               f'track still plays is unknown')
            try:
                points, playing = self._program_track_state(status)
            except (AttributeError, TypeError, ValueError):
                return False, (f'{message}; the ACU status is malformed, so whether a program '
                               f'track still plays is unknown')
            if points is None and playing is None:
                return False, (f'{message}; the ACU status carries neither the stack count nor '
                               f'the axis modes, so whether a program track still plays is '
                               f'unknown')
            if points is None and playing:
                return False, (f'{message}; the ACU status is missing its stack count, so whether '
                               f'a program track still plays is unknown')
            if points and playing is not False:
                self.log.warn('/abort: Go TCS had no command, but the ACU is still playing '
                              f'a program track ({points} points on its stack)')
                return self._stop_held_track(tcs, status, points, message)
            if points:
                return True, (f'{message}; axes not in program track, {points} stale '
                              f'points on the stack')
            return True, message
        if reply.get('status') != 'ok':
            self.log.warn('/abort: unexpected reply from the TCS: {reply}', reply=repr(raw))
            return False, f'unexpected reply: {raw!r}'
        return True, message

    def _current_encoder_azel(self, tcs):
        """Return the current encoder ``(az, el, source)`` in degrees, or ``None``.

        Prefers the 200 Hz position broadcast (``self.data['broadcast']``, keys
        ``'Azimuth'`` / ``'Elevation'``). If the stream is not warm yet, falls back
        to a one-shot ``tcs.get_status()`` on the per-scan client (the status field
        names ``'AzimuthCurrentPosition'`` / ``'ElevationCurrentPosition'``). Returns
        ``None`` when neither source can supply a live position, so the caller
        refuses to dispatch rather than guess (a wrong guess feeds
        ``choose_encoder_solution`` and can produce a wrong-wrap slew).

        Called off the reactor via ``deferToThread``; the broadcast fast-path is a
        benign concurrent read of ``self.data['broadcast']`` keys, which are
        set-only/monotonic and hold immutable floats. Each read is GIL-atomic; a
        cross-batch az/el skew of one broadcast interval is possible and negligible
        for wrap selection.
        """
        bcast = self.data.get('broadcast', {})
        if 'Azimuth' in bcast and 'Elevation' in bcast:
            return float(bcast['Azimuth']), float(bcast['Elevation']), 'broadcast'
        # Broadcast not warm; one-shot status read. tcs.get_status() calls
        # sys.exit(-1) on ConnectionError -> SystemExit, and cold start is exactly
        # when that is most likely; trap (Exception, SystemExit) but NOT
        # BaseException (KeyboardInterrupt/GeneratorExit keep propagating).
        try:
            status = tcs.get_status()
            az = status.get('AzimuthCurrentPosition')
            el = status.get('ElevationCurrentPosition')
            if az is not None and el is not None:
                return float(az), float(el), 'status'
        except (Exception, SystemExit) as e:
            self.log.warn('_current_encoder_azel: get_status() fallback failed: {e}', e=e)
        # Neither source live: return None so the caller refuses to dispatch
        # rather than guess (see docstring; a guess can feed a wrong-wrap slew).
        self.log.warn('no live position available; '
                      'cannot determine current encoder az/el.')
        return None

    def _axes_stopped(self, status):
        """Return ``True`` iff both axis velocities in ``status`` are stopped.

        Reads ``'AzimuthCurrentVelocity'`` / ``'ElevationCurrentVelocity'``
        from a ``get_status()`` dict; ``True`` only when both are present
        and below :data:`TCS_SPEED_TOL` (``speedTol`` at TCS ``b7bbbf9``). A missing velocity
        returns ``False`` ("not known to be stopped"), so the slew-arrival gate
        keeps waiting rather than POSTing into a still-settling mount.
        """
        vaz = status.get('AzimuthCurrentVelocity')
        vel = status.get('ElevationCurrentVelocity')
        return (vaz is not None and abs(vaz) < TCS_SPEED_TOL
                and vel is not None and abs(vel) < TCS_SPEED_TOL)

    def _preset_arrived(self, status, enc_az, enc_el):
        """Return ``(arrived, reason)``: whether a ``get_status()`` dict shows the
        move-to to ``(enc_az, enc_el)`` done, and if not, the first unmet condition.

        The test of ``moveToCmd.isDone`` (TCS ``b7bbbf9``), read from the same
        raw-frame record: both axes in Preset, each on its commanded position
        (within :data:`SLEW_ARRIVAL_TOL_DEG`) and stopped (:meth:`_axes_stopped`).
        It adds that the commanded position lies within
        :data:`POINTING_CORRECTION_MAX_DEG` of the sky target, so a preset left
        from an earlier move, or one wrap away, does not pass. A missing field is
        an unmet condition.
        """
        for axis in ('Azimuth', 'Elevation'):
            mode = status.get(f'{axis}Mode')
            if not mode_is_preset(mode):
                return False, f'{axis.lower()} mode is {mode!r}, not Preset'
        for axis, target in (('Azimuth', enc_az), ('Elevation', enc_el)):
            current = status.get(f'{axis}CurrentPosition')
            commanded = status.get(f'{axis}CommandedPosition')
            if current is None or commanded is None:
                return False, f'{axis.lower()} current or commanded position missing'
            if abs(commanded - target) > POINTING_CORRECTION_MAX_DEG:
                return False, (f'{axis.lower()} commanded {commanded:.4f} is not the '
                               f'target {target:.4f} (stale preset, or a pointing '
                               f'correction over {POINTING_CORRECTION_MAX_DEG} deg)')
            if abs(current - commanded) > SLEW_ARRIVAL_TOL_DEG:
                return False, (f'{axis.lower()} current {current:.4f} is not on its '
                               f'commanded {commanded:.4f}')
        if not self._axes_stopped(status):
            return False, 'axes not stopped'
        return True, 'arrived'

    @ocs_agent.param('scan_params', type=dict)
    @ocs_agent.param('scheduled_t0_unix', type=float, default=None)
    @inlineCallbacks
    def constant_el_scan(self, session, params):
        """constant_el_scan(scan_params, scheduled_t0_unix=None)

        **Process** - FYST constant-elevation scan. At dispatch it
        builds a full az/el trajectory with ``fyst_trajectories`` from the
        *current* encoder position (OCS-free core
        :func:`~pcs.agents.acu_interface.trajectory.build_constant_el_payload`,
        astropy ephemeris math built off-reactor), slews to the sun-safe scan
        start, and POSTs to the Go TCS ``/path`` endpoint. Registered as a Process
        so it can be aborted mid-scan (aborter -> session ``'stopping'`` -> Go TCS
        ``/abort``).

        Progress is in ``session.data``: ``scan``, ``scheduled_t0_unix``,
        ``ignored_keys`` (``scan_params`` keys the build does not read, or that
        the chosen form does not use),
        ``module`` (the key as sent, None without it), the
        ``timestamp`` of the last change, and ``phase``: ``building``; ``slewing``
        (adds ``encoder_az``, ``encoder_el``, ``start_time``, ``n_points``);
        ``holding`` (``/path`` accepted, ``start_time`` re-floored; the dish waits
        at the scan start until then, the readout-tuning window that
        ``scheduled_t0_unix`` sets); ``scanning``. A failure leaves the last phase.

        Parameters:
            scan_params (dict): Constant-elevation scan specification in one
                of two forms. Both require ``elevation`` (deg) and ``velocity``
                (azimuth-coordinate deg/s, mount frame, sent to the ACU as-is,
                NOT cos(el)-scaled) and take ``az_accel`` and ``timestep``.
                Field form (no ``az_min`` / ``az_max``), modeled on
                ``fyst_trajectories.plan_constant_el_scan``: required
                ``ra_center``, ``dec_center``, ``width``, ``height`` (deg);
                optional ``rising`` (bool), ``angle``, ``az_padding`` (default
                0.0), ``max_search_hours``, ``step_seconds``, ``lsa_window``
                (``rising`` is then ignored); ``duration`` has no effect here.
                Either, when given, is named in ``ignored_keys``.
                Window form (``az_min`` and ``az_max`` together; one alone is
                refused), modeled on the window form of
                ``fyst_trajectories.plan_constant_el_scan``: the dish
                sweeps ``az_min`` to ``az_max`` (deg) for ``duration``
                (required, seconds, at least one leg) from the start; optional
                ``az_padding``
                (default 0.0) and the four field keys, all or none.
                ``lsa_window`` is refused here, and ``rising``, ``angle``,
                ``max_search_hours`` and ``step_seconds`` are not used (named in
                ``ignored_keys`` when given).
                Both forms take ``module``: a Prime-Cam bay name such as
                'IM5', or several (a list or one comma-separated string) for
                their mean position, at which the scan is planned; the
                geometry keys stay as given and the dish is commanded to the
                boresight that puts that position on them. ``footprint`` is
                refused.
            scheduled_t0_unix (float): Scheduled scan start in Unix seconds, or
                None to start as soon as the dispatch buffer allows. In the
                field form the planner searches forward from
                ``max(scheduled_t0_unix, now + 10 s)`` for the elevation
                crossing, or with ``lsa_window`` for the window's start; the
                window form posts that floored time as its start, moved later
                only by the post-slew re-floor.
        """
        result = yield self._dispatch_scan_process(
            session, params, build_fn=build_constant_el_payload,
            job='constant_el_scan', tcs=self._make_tcs())
        return result

    @inlineCallbacks
    def _dispatch_scan_process(self, session, params, *, build_fn, job, tcs):
        """Shared dispatch-and-run body for a typed scan Process.

        One implementation for ``constant_el_scan`` / ``source_scan`` /
        ``pong_scan`` / ``daisy_scan`` instead of four copies: dispatch-buffer
        floor, position-unknown refusal, off-reactor trajectory build, sun-safe
        slew, slew-arrival gate, post-slew re-floor, busy-retried POST, completion-
        latch / abort loop.

        ``params`` must carry ``scan_params`` (dict) and ``scheduled_t0_unix``
        (float or None). ``build_fn`` is one of the ocs-free ``build_*_payload``
        cores in :mod:`pcs.agents.acu_interface.trajectory`, called off the reactor
        via ``deferToThread`` and returning ``{"encoder_az", "encoder_el",
        "payload"}``. ``job`` is the lock job name / log prefix. ``tcs`` is a fresh
        per-scan client (see :meth:`_make_tcs`), not shared with the monitor or the
        abort task, so its ``requests.Session`` cannot be hit concurrently.

        Progress goes to ``session.data`` as :meth:`constant_el_scan` documents.
        """
        with self.azel_lock.acquire_timeout(0, job=job) as acquired:
            if not acquired:
                return False, f"Operation failed: {self.azel_lock.job} is running."

            # Current encoder position (200 Hz broadcast, else one-shot status).
            # Refuse to dispatch on an unknown position; the False is retryable
            # once the broadcast warms (sub-second).
            pos = yield threads.deferToThread(self._current_encoder_azel, tcs)
            if pos is None:
                return False, (
                    f"{job}: refusing to dispatch, current telescope position "
                    "unknown (200 Hz broadcast cold and ACU status unavailable). "
                    "Start/await the 'broadcast' Process and retry.")
            current_az, current_el, src = pos
            self.log.info(
                f'{job}: current position az={current_az:.4f}, '
                f'el={current_el:.4f} (from {src})')

            # Build the trajectory + sun-safe slew target off the reactor thread.
            site = get_fyst_site()
            scan_params = dict(params['scan_params'])
            max_dispatch_delay_sec = scan_params.pop('max_dispatch_delay_sec', None)
            # Unread keys are named, not refused (see trajectory.SCAN_PARAM_KEYS).
            ignored_keys = unknown_scan_param_keys(job, scan_params)
            if ignored_keys:
                self.log.warn(
                    '{job}: ignoring scan_params keys the build does not read: '
                    '{ignored_keys}; the scan is built without them',
                    job=job, ignored_keys=ignored_keys)
            phase_data = {'scan': job, 'scheduled_t0_unix': params.get('scheduled_t0_unix'),
                          'ignored_keys': ignored_keys, 'module': scan_params.get('module')}
            self._publish_phase(session, phase_data, 'building')
            build_kwargs = dict(
                scan_params=scan_params,
                current_az=current_az,
                current_el=current_el,
                site=site,
                scheduled_t0_unix=params.get('scheduled_t0_unix'),
                now_unix=time.time(),
            )
            if max_dispatch_delay_sec is not None:
                build_kwargs['max_dispatch_delay_sec'] = max_dispatch_delay_sec
            try:
                result = yield threads.deferToThread(build_fn, **build_kwargs)
            except Exception as e:
                self.log.error('{job}: trajectory build failed: {e}', job=job, e=e)
                return False, f'Trajectory build failed: {e}'

            enc_az = result['encoder_az']
            enc_el = result['encoder_el']
            payload = result['payload']
            self.log.info(
                f'{job}: slew target az={enc_az:.4f}, el={enc_el:.4f}; '
                f'{len(payload["points"])} trajectory points, '
                f'start_time={payload["start_time"]:.3f}')

            # Honor an abort requested during the (potentially long) build.
            if session.status == 'stopping':
                return True, 'Aborted before slew.'

            # Slew to the sun-safe start (fresh-per-scan client; see _make_tcs),
            # then gate + POST the scan.
            self.log.info(f'{job}: slewing to sun-safe scan start')
            slew_msg = None
            for attempt in range(1, TCS_BUSY_RETRY_MAX + 1):
                if session.status == 'stopping':
                    return True, 'Aborted before slew.'
                try:
                    slew_msg = yield threads.deferToThread(tcs.move_to, enc_az, enc_el)
                except (Exception, SystemExit) as e:
                    # aculib.post raises SystemExit on a requests error. A read
                    # timeout (TCS_HTTP_TIMEOUT) can come after the Go TCS took the
                    # move, so /abort before releasing azel_lock.
                    self.log.error('{job}: slew move-to failed in transport: {e}', job=job, e=e)
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    outcome = self._launch_abort_outcome(sent, note)
                    return False, (
                        f'{job}: slew move-to failed in transport ({e}); the Go TCS '
                        f'may have queued the move, so /abort was sent{outcome}; '
                        f'redispatch.')
                slew_code = tcs_response_status(slew_msg)
                self.log.info(f'{job}: move_to response code {slew_code}')
                if slew_code == 200:
                    break
                if slew_code is None:
                    if attempt < TCS_BUSY_RETRY_MAX:
                        yield dsleep(TCS_BUSY_RETRY_DELAY_SEC)
                    continue
                if slew_code >= 500:
                    # The OCS API failed, so the move may have reached the TCS.
                    self.log.error('{job}: slew move-to failed at the OCS API: {reason}',
                                   job=job, reason=tcs_response_reason(slew_msg))
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    outcome = self._launch_abort_outcome(sent, note)
                    return False, (
                        f'{job}: slew move-to failed at the OCS API '
                        f'({tcs_response_reason(slew_msg)}); the Go TCS may have '
                        f'queued the move, so /abort was sent{outcome}; redispatch.')
                return False, (
                    f'{job}: move-to to scan start refused '
                    f'({tcs_response_reason(slew_msg)}); scan not launched.')
            else:
                # Every attempt was refused as busy.
                return False, (
                    f'{job}: Go TCS stayed busy for {TCS_BUSY_RETRY_MAX} move-to '
                    f'attempts ({tcs_response_reason(slew_msg)}); scan not launched.')
            self._publish_phase(
                session, phase_data, 'slewing', encoder_az=enc_az, encoder_el=enc_el,
                start_time=payload['start_time'], n_points=len(payload['points']))

            # The Go TCS refuses a /path as busy until the move-to is done, which it
            # judges from the ACU's raw current and commanded positions, never the sky
            # target (moveToCmd.isDone at TCS b7bbbf9), so the gate reads the same
            # fields (_preset_arrived). dsleep, not time.sleep: this runs on the reactor.
            slew_deadline = time.time() + SLEW_TIMEOUT_SEC
            reason, status = 'no status read', None
            while True:
                if session.status == 'stopping':
                    self.log.info(f'{job}: abort requested during slew, sending /abort')
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    return self._stop_outcome(job, sent, note, ' during slew')
                # Sleep before each read: the Go TCS answers the move-to when its
                # loop takes it, before MoveTo sets the new preset and Preset modes.
                yield dsleep(SLEW_STATUS_POLL_SEC)
                # A failed read leaves the move "not known to be done", so keep
                # polling until the timeout.
                try:
                    status = yield threads.deferToThread(tcs.get_status)
                except (Exception, SystemExit) as e:
                    reason = f'status read failed ({e})'
                    self.log.warn('{job}: status read during slew gate failed: {e}', job=job, e=e)
                else:
                    arrived, reason = self._preset_arrived(status, enc_az, enc_el)
                    if arrived:
                        break
                if time.time() > slew_deadline:
                    # The move may still be running: stop it before the lock goes.
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    outcome = self._abort_outcome(sent, note)
                    readings = {key: (status or {}).get(key) for key in (
                        'AzimuthMode', 'ElevationMode',
                        'AzimuthCurrentPosition', 'ElevationCurrentPosition',
                        'AzimuthCommandedPosition', 'ElevationCommandedPosition')}
                    return False, (
                        f'{job}: slew to scan start timed out '
                        f'(target az={enc_az:.3f}, el={enc_el:.3f}; last unmet: {reason}; '
                        f'last reading {readings}); {outcome}.')

            # Each attempt re-floors start_time (see SCAN_DISPATCH_BUFFER_SEC), as
            # waiting eats lead.
            original_start = payload["start_time"]
            scan_msg = None
            for attempt in range(1, TCS_BUSY_RETRY_MAX + 1):
                # An abort can land during the slew gate's last read or a retry wait;
                # a POST then would race the completion loop's /abort at the ACU.
                if session.status == 'stopping':
                    self.log.info(f'{job}: abort requested before /path POST, sending /abort')
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    return self._stop_outcome(job, sent, note, ' before /path POST')

                payload = refloor_payload_start_time(payload, time.time())
                drift = refloor_drift_seconds(original_start, payload)
                if drift > MAX_REFLOOR_DRIFT_SEC:
                    # The baked track would lag the sky (see MAX_REFLOOR_DRIFT_SEC).
                    return False, (
                        f'{job}: post-slew re-floor advanced start_time by '
                        f'{drift:.1f} s (> {MAX_REFLOOR_DRIFT_SEC:.0f} s); the slew '
                        f'consumed the scan lead and the trajectory geometry is '
                        f'stale. Refusing to POST; redispatch.')

                self.log.info(
                    f'{job}: posting trajectory to /path '
                    f'(attempt {attempt}/{TCS_BUSY_RETRY_MAX})')
                try:
                    scan_msg = yield threads.deferToThread(tcs.scan_pattern, payload)
                except (Exception, SystemExit) as e:
                    # As for the slew; no re-POST, since the path may have reached the TCS.
                    self.log.error('{job}: /path POST failed in transport: {e}', job=job, e=e)
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    # Read once the abort has completed: one sent before the path start may still
                    # have reached the Go TCS after it.
                    if sent and time.time() >= payload['start_time']:
                        outcome = (', but had not completed before the path start, so the scan may have run'
                                   + (f' ({note})' if note else ''))
                    else:
                        outcome = self._launch_abort_outcome(sent, note)
                    return False, (
                        f'{job}: /path POST failed in transport ({e}); the Go TCS '
                        f'may have queued the path, so /abort was sent{outcome}; '
                        f'redispatch.')
                scan_code = tcs_response_status(scan_msg)
                self.log.info(f'{job}: /path response code {scan_code}')
                if scan_code == 200:
                    break
                if scan_code is None:
                    if attempt < TCS_BUSY_RETRY_MAX:
                        yield dsleep(TCS_BUSY_RETRY_DELAY_SEC)
                    continue
                if scan_code >= 500:
                    # The OCS API failed, so the path may have reached the TCS.
                    self.log.error('{job}: /path POST failed at the OCS API: {reason}',
                                   job=job, reason=tcs_response_reason(scan_msg))
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    outcome = self._launch_abort_outcome(sent, note)
                    return False, (
                        f'{job}: /path POST failed at the OCS API '
                        f'({tcs_response_reason(scan_msg)}); the Go TCS may have '
                        f'queued the path, so /abort was sent{outcome}; redispatch.')
                return False, (
                    f'{job}: /path POST refused '
                    f'({tcs_response_reason(scan_msg)}); scan not launched.')
            else:
                # Every attempt was refused as busy.
                return False, (
                    f'{job}: Go TCS stayed busy for {TCS_BUSY_RETRY_MAX} /path '
                    f'attempts ({tcs_response_reason(scan_msg)}); '
                    f'scan not launched.')

            self._publish_phase(session, phase_data, 'holding', start_time=payload['start_time'])

            # /path is fire-and-forget: a 200 means queued. Completion is the stack-drained
            # test of startPattern's isDone (TCS b7bbbf9), delegated to ScanCompletionLatch,
            # with the scan end plus SCAN_SETTLE_SEC as a backstop so a failing status read
            # cannot hold azel_lock. start_time is absolute Unix; points[-1][0] is relative.
            end_unix = payload['start_time'] + payload['points'][-1][0]
            deadline = end_unix + SCAN_SETTLE_SEC
            latch = ScanCompletionLatch(payload['start_time'])
            stack_filled = False  # a read showed THIS track's points on the stack
            stack_read = False    # at least one status read returned a stack count
            never_uploaded = (
                f'{job}: the Go TCS accepted the path but no status read showed its points on '
                f'the ACU program-track stack (never uploaded, never started, or played while '
                f'the reads failed; see the Go TCS log)')
            left_track_reads = 0  # consecutive reads of points left with no axis in ProgramTrack
            while True:
                if session.status == 'stopping':
                    self.log.info(f'{job}: abort requested, sending /abort')
                    sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                    return self._stop_outcome(job, sent, note)
                if (phase_data['phase'] != 'scanning' and stack_filled
                        and time.time() >= payload['start_time']):
                    self._publish_phase(session, phase_data, 'scanning')
                try:
                    status = yield threads.deferToThread(tcs.get_status)
                except (Exception, SystemExit) as e:
                    # aculib.get_status() raises SystemExit on ConnectionError, which
                    # `except Exception` would NOT catch. Trap it so a transient blip
                    # cannot kill the Process; the time backstop still bounds the wait.
                    self.log.warn('{job}: status read failed during wait: {e}', job=job, e=e)
                else:
                    # StatusGeneral8100 field names, not the ACU's long aliases
                    # (telescope_status in api/app/telescope.py at OCS c515a51).
                    free = status.get('QtyOfFreeProgramTrackStackPositions')
                    points, playing = self._program_track_state(status)
                    if points is not None:
                        stack_read = True
                        stack_filled = stack_filled or (points > 0 and playing is not False)
                    now = time.time()
                    if (stack_read and not stack_filled
                            and now >= payload['start_time'] + SCAN_EARLY_DRAIN_TOL_SEC):
                        # Never on the stack by the tolerance past the start: fail now, as
                        # an empty or stale stack never trips the drained latch.
                        sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                        outcome = self._abort_outcome(sent, note)
                        self.log.error(never_uploaded)
                        return False, f'{never_uploaded}; {outcome}. Redispatch.'
                    if (stack_filled and now >= payload['start_time']
                            and points and playing is False):
                        # The ACU left the track (a fault, or a stop from elsewhere).
                        left_track_reads += 1
                        if left_track_reads >= TRACK_LEFT_CONFIRM_READS:
                            sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                            outcome = self._abort_outcome(sent, note)
                            return False, (
                                f'{job}: the ACU left program track with {points} points '
                                f'unplayed; scan interrupted ({outcome}). Redispatch.')
                    else:
                        left_track_reads = 0
                    vaz = status.get('AzimuthCurrentVelocity')
                    vel = status.get('ElevationCurrentVelocity')
                    done = latch.update(free=free, vaz=vaz, vel=vel, now_unix=now,
                                        az_mode=status.get('AzimuthMode'),
                                        el_mode=status.get('ElevationMode'))
                    if done:
                        if not stack_filled:
                            # Drained but never filled: /abort in case the Go TCS
                            # still holds the command.
                            sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                            outcome = self._abort_outcome(sent, note)
                            self.log.error(never_uploaded)
                            return False, f'{never_uploaded}; {outcome}. Redispatch.'
                        # A track cut short (see SCAN_EARLY_DRAIN_TOL_SEC): stop
                        # whatever is left before the lock goes.
                        early = end_unix - time.time()
                        if early > SCAN_EARLY_DRAIN_TOL_SEC:
                            self.log.error(
                                f'{job}: program-track stack drained {early:.0f} s '
                                f'before the scheduled end; scan truncated.')
                            sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                            outcome = self._abort_outcome(sent, note)
                            return False, (
                                f'{job}: program-track stack drained {early:.0f} s '
                                f'before the scheduled end; scan truncated (the Go TCS '
                                f'may have dropped the track or rejected points at '
                                f'upload); {outcome}. Redispatch.')
                        self.log.info(f'{job}: scan complete (stack drained).')
                        return True, 'scan complete (stack drained at scheduled end)'
                if time.time() >= deadline:
                    if stack_read and not stack_filled:
                        # Never on the stack, for a path shorter than the tolerance or
                        # reads failing after the start: as at the exits above.
                        sent, note = yield threads.deferToThread(self._safe_abort, tcs)
                        outcome = self._abort_outcome(sent, note)
                        self.log.error(never_uploaded)
                        return False, f'{never_uploaded}; {outcome}. Redispatch.'
                    # No drained signal: release azel_lock as an unconfirmed success.
                    self.log.info(f'{job}: scan end reached (time backstop).')
                    return True, 'scan end reached (time backstop; completion unconfirmed)'
                yield dsleep(SCAN_COMPLETION_POLL_SEC)

    @ocs_agent.param('scan_params', type=dict)
    @ocs_agent.param('scheduled_t0_unix', type=float, default=None)
    @inlineCallbacks
    def source_scan(self, session, params):
        """source_scan(scan_params, scheduled_t0_unix=None)

        **Process** - FYST source-tracking constant-elevation scan.
        At dispatch it drags a moving source (planet or sidereal point) across the
        *centred* PrimeCam focal plane at fixed boresight elevation with
        ``fyst_trajectories.plan_source_ces`` (OCS-free core
        :func:`~pcs.agents.acu_interface.trajectory.build_source_payload`, built
        off-reactor), slews to the sun-safe scan start, and POSTs to Go TCS
        ``/path``. Abortable mid-scan (aborter -> session ``'stopping'`` -> Go TCS
        ``/abort``).

        Progress is in ``session.data``, as for :meth:`constant_el_scan`.

        Velocity frame: the commanded az velocity is the per-leg speed
        (``az_speed`` when given, else derived by the planner) plus the solved
        drift ``v_az``, both MOUNT-frame azimuth-coordinate deg/s (same frame as
        ``constant_el_scan``, NOT the on-sky frame pong/daisy take); ``cos(el)``
        is implicit in the elevation-fixed az track and NOT re-applied.

        **Centred only.** Dispatches the on-axis, full-array case (``footprint="c"``,
        uncommanded boresight rotator). The off-centre single-module case and a
        commanded ``boresight_rot`` are gated off in ``build_source_payload``
        (the bay layout is not yet checked on the sky; no boresight rotation is
        confirmed for Prime-Cam); requesting either, or sending ``module``, the
        field scans' key, raises ``ValueError``; a ``footprint`` that names no bay
        (a removed ``i1``) raises the library's ``KeyError``.

        Parameters:
            scan_params (dict): Source-CES spec, modeled on
                ``fyst_trajectories.plan_source_ces``. Source: ``body`` (str) OR
                ``ra`` + ``dec`` (deg); ``el_bore`` (deg), or none for the planner
                to derive it, and ``mode`` unless given, from the scan's start (the
                pass starts at or shortly after it). Optional: ``mode``
                ('rising'/'setting'), ``footprint`` (must name the centre module,
                'c', 'center' or 'IM0' in any case, while the gate stands),
                ``boresight_rot`` (must be None), ``pm_ra``/``pm_dec``,
                ``ref_epoch``, ``timestep``, ``sampling_step_seconds``, ``az_accel``,
                ``az_padding``, ``az_branch``, ``allow_partial``, ``v_az``,
                ``az_speed`` (deg/s, mount frame), ``az_throw`` (deg; replaces the
                padded throw, so leave ``az_padding`` unset), ``dwell`` (s),
                ``footprint_margin`` (deg, non-negative; widens the centre module's
                footprint on every side), ``window`` (needs ``el_bore``).
            scheduled_t0_unix (float): Scheduled start in Unix seconds (the
                plan_source_ces search anchor), or None to start as soon as the
                dispatch buffer allows.
        """
        result = yield self._dispatch_scan_process(
            session, params, build_fn=build_source_payload, job='source_scan',
            tcs=self._make_tcs())
        return result

    @ocs_agent.param('scan_params', type=dict)
    @ocs_agent.param('scheduled_t0_unix', type=float, default=None)
    @inlineCallbacks
    def pong_scan(self, session, params):
        """pong_scan(scan_params, scheduled_t0_unix=None)

        **Process** - FYST Pong (curvy-box) scan over a rectangular
        RA/Dec field. At dispatch it builds the trajectory with
        ``fyst_trajectories.plan_pong_scan`` (OCS-free core
        :func:`~pcs.agents.acu_interface.trajectory.build_pong_payload`, built
        off-reactor), slews to the sun-safe scan start, and POSTs to Go TCS
        ``/path``. Abortable mid-scan (aborter -> session ``'stopping'`` -> Go TCS
        ``/abort``).

        Progress is in ``session.data``, as for :meth:`constant_el_scan`.

        Velocity frame: ``scan_params['velocity']`` is ON-SKY (tangent-plane)
        deg/s, a DIFFERENT frame from ``constant_el_scan`` / ``source_scan``. The
        planner maps it to the mount frame via the field geometry; pass the
        astronomer's on-sky scan speed directly (do NOT pre-scale by cos(el)).

        Parameters:
            scan_params (dict): Pong specification, modeled on
                ``fyst_trajectories.plan_pong_scan``. Required: ``ra_center``,
                ``dec_center``, ``width``, ``height`` (deg); ``velocity``
                (on-sky deg/s), ``spacing`` (deg), ``num_terms`` (int). Optional:
                ``angle`` (deg), ``n_cycles`` (int), ``timestep`` (s), ``module``
                (a bay name, or several for their mean position, at which the
                field is scanned; ``footprint`` is refused).
            scheduled_t0_unix (float): Scheduled start in Unix seconds, or None
                to start as soon as the dispatch buffer allows. Pong uses
                start_time literally (no forward search).
        """
        result = yield self._dispatch_scan_process(
            session, params, build_fn=build_pong_payload, job='pong_scan',
            tcs=self._make_tcs())
        return result

    @ocs_agent.param('scan_params', type=dict)
    @ocs_agent.param('scheduled_t0_unix', type=float, default=None)
    @inlineCallbacks
    def daisy_scan(self, session, params):
        """daisy_scan(scan_params, scheduled_t0_unix=None)

        **Process** - FYST Daisy (constant-velocity petal) scan
        centred on a point source. At dispatch it builds the trajectory with
        ``fyst_trajectories.plan_daisy_scan`` (OCS-free core
        :func:`~pcs.agents.acu_interface.trajectory.build_daisy_payload`, built
        off-reactor), slews to the sun-safe scan start, and POSTs to Go TCS
        ``/path``. Abortable mid-scan (aborter -> session ``'stopping'`` -> Go TCS
        ``/abort``).

        Progress is in ``session.data``, as for :meth:`constant_el_scan`.

        Velocity frame: ``scan_params['velocity']`` is ON-SKY (tangent-plane)
        deg/s, the SAME frame as Pong, DIFFERENT from ``constant_el_scan`` /
        ``source_scan``. The planner maps it to the mount frame; pass the
        astronomer's on-sky scan speed directly.

        Parameters:
            scan_params (dict): Daisy specification, modeled on
                ``fyst_trajectories.plan_daisy_scan``. Required: ``ra``, ``dec``
                (deg); ``radius`` (deg), ``velocity`` (on-sky deg/s),
                ``turn_radius`` (deg), ``avoidance_radius`` (deg), and
                ``start_acceleration`` (deg/s^2), ``duration`` (s). Optional:
                ``y_offset`` (deg), ``timestep`` (s), ``module`` (a bay name, or
                several for their mean position, at which the source is scanned;
                ``footprint`` is refused).
            scheduled_t0_unix (float): Scheduled start in Unix seconds, or None
                to start as soon as the dispatch buffer allows. Daisy uses
                start_time literally (no forward search).
        """
        result = yield self._dispatch_scan_process(
            session, params, build_fn=build_daisy_payload, job='daisy_scan',
            tcs=self._make_tcs())
        return result

    @inlineCallbacks
    def abort(self, session, params):
        """abort()

        **Task** - Send a Go TCS ``/abort`` through :meth:`_safe_abort`, on a fresh
        client (:meth:`_make_tcs`), after stopping every running typed scan Process
        (:data:`SCAN_PROCESS_OPS`). Safe with nothing running: "nothing to abort"
        succeeds unless the ACU still plays a program track, which is then stopped.
        Fails, never raises, when the ``/abort`` or that stop is not confirmed.

        The scans are stopped because the Go TCS ``/abort`` sets Stop but leaves the
        program-track stack, so the stack-drained test never passes and a scan not told
        to stop would hold ``azel_lock`` until it failed on its own. ``OCSAgent.stop``,
        which acts on the reactor thread, returns an error tuple for an op not running;
        that is logged and skipped.
        """
        for op in SCAN_PROCESS_OPS:
            try:
                status, msg, _ = self.agent.stop(op)
                self.log.info('abort: stop({op}) -> {status}: {msg}', op=op, status=status, msg=msg)
            except Exception as e:
                self.log.warn('abort: stop({op}) failed (continuing): {e}', op=op, e=e)
        sent, note = yield threads.deferToThread(self._safe_abort, self._make_tcs())
        detail = f' ({note})' if note else ''
        if sent:
            return True, (f'abort: Go TCS /abort sent{detail}; '
                          'stop requested on any running scan.')
        if note.startswith(TCS_NOTHING_TO_ABORT_MESSAGE):
            return False, (f'abort: the Go TCS had nothing to abort, but the ACU is not known '
                           f'to be stopped ({note}).')
        return False, f'abort: Go TCS /abort failed{detail}; the dish may still be moving.'


    @ocs_agent.param('all_axes', default=False, type=bool)
    @inlineCallbacks
    def stop_and_clear(self, session, params):
        """stop_and_clear(all_axes=False)

        **Task** - Change the azimuth, elevation
        to Stop; also clear the ProgramTrack stack.

        Args:
          all_axes (bool): Send Stop to all axes, even ones user has
            requested to be ignored.

        """
        def _read_modes():
            modes = [self.data['status']['summary']['Azimuth_mode'],
                     self.data['status']['summary']['Elevation_mode']]
            # if self.acu_conf['platform'] == 'satp':
            #     modes.append(self.data['status']['summary']['Boresight_mode'])
            # elif self.acu_conf['platform'] in ['ccat', 'lat']:
            #     modes.append(self.data['status']['corotator']['Corotator_mode'])
            return modes

        # for i in range(6):
        #     for short_name, mode in zip(['az', 'el', 'third'],
        #                                 _read_modes()):
        #         if (params['all_axes'] or short_name not in self.ignore_axes) and mode != 'Stop':
        #             break
        #     else:
        #         self.log.info('All axes in Stop mode')
        #         break
        #     yield self._stop(params['all_axes'])
        #     self.log.info('Stop called (iteration %i)' % (i + 1))
        #     yield dsleep(0.1)

        # else:
        #     msg = 'Failed to set all axes to Stop mode!'
        #     self.log.error(msg)
        #     return False, msg

        yield self.acu_read.abort() #aborts telescope movement that may be taking place

        # for i in range(6):
        #     free_stack = self.data['status']['summary']['Free_upload_positions']
        #     if free_stack < FULL_STACK:
        #         yield self.acu_control.http.Command('DataSets.CmdTimePositionTransfer',
        #                                             'Clear Stack')
        #         self.log.info('Clear Stack called (iteration %i)' % (i + 1))
        #         yield dsleep(0.1)
        #     else:
        #         self.log.info('Stack cleared')
        #         break
        # else:
        #     msg = 'Failed to clear the ProgramTrack stack!'
        #     self.log.warn(msg)
        #     return False, msg

        yield self.acu_read.clear() #sets az and el modes to "Stop" and clears ProgramTrack stack

        session.set_status('stopping')
        return True, 'Job completed'

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
                 device=args.device,
                 startup=not args.no_processes)

    runner.run(agent, auto_reconnect=True)


if __name__=='__main__':
    main()
