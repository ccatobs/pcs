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

    Differences between SATP and LAT structures:

    - The PlatformType reports "satp" for SATP and "ccat" for LAT.
    - In the case of an SATP, the Status3rdAxis is not populated;
        the Boresight info can be found in StatusDetailed.  In the
        case of the LAT, the corotator info is queried separately
        and stored under Status3rdAxis.
    - The StatusShutter and Hvac entries will be populated for the
        LAT, but empty for SATP.

    """

    # Note that session.data will get scanned, to assign data to
    # feed blocks.  We make an explicit list of items to ignore
    # during that scan (not_data_keys).
    session.data = {'PlatformType': self.acu_config['platform'],
                    'DefaultScanParams': self.scan_params,
                    'StatusResponseRate': 0.,
                    'IgnoredAxes': self.ignore_axes,
                    'NamedPositions': self.named_positions,
                    'connected': False}
    not_data_keys = list(session.data.keys())

    last_complaint = 0
    while True:
        try:
            version = yield self.acu_read.http.Version()
            break
        except Exception as e:
            if time.time() - last_complaint > 3600:
                errormsg = {'aculib_error_message': str(e)}
                self.log.error(str(e))
                self.log.error('monitor process failed to query version! Will keep trying.')
                last_complaint = time.time()
            yield dsleep(10)

    self.log.info(version)
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
        ('summary', 'Boresight_mode'),
        ('corotator', 'Corotator_mode'),
    ]
    prev_checkdata = {k: None for g, k in checkdata}

    @inlineCallbacks
    def _get_status():
        output = {}
        for short, collection in [
                ('status', 'StatusDetailed'),
                ('third', 'Status3rdAxis'),
                ('shutter', 'StatusShutter'),
                ('pointing', 'CmdPointingCorrection'),
                ('hvac', 'Hvac'),
        ]:
            if self.datasets[short]:
                output[collection] = (
                    yield self.acu_read.Values(self.datasets[short]))
            else:
                output[collection] = {}
        return output

    session.data['StatusResponseRate'] = n_ok / (query_t - report_t)
    session.data.update((yield _get_status()))
    qual_pacer = Pacemaker(.1)

    hvm = hvac.HvacManager()

    last_resp_rate = None
    data_blocks = {}
    influx_blocks = {}
    unknown_fields = set()

    while session.status in ['running']:

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

        try:
            session.data.update((yield _get_status()))
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

        for k, v in session.data.items():
            if k in not_data_keys:
                continue

            if k == 'Hvac' and len(v) > 0 and (hvm.grouped_fields is None):
                # Runs when first HVAC data are received. These
                # fields aren't listed explicitly in soaculib so
                # they're analyzed here.
                hvm.parse_fields(v)
                assert len(hvm.grouped_fields['unclassified']) == 0
                self.status_field_map.update(hvm.get_block_info())

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
                self.data['status'][group][field] = value

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

        # influx_blocks are constructed based on refers to all
        # other self.data['status'] keys. Do not add more keys to
        # any self.data['status'] categories beyond this point
        new_influx_blocks = {}
        for category in self.data['status']:
            new_influx_blocks[category] = {
                'timestamp': self.data['status']['summary']['ctime'],
                'block_name': category,
                'data': {}}

            if category != 'commands':
                for statkey, statval in self.data['status'][category].items():
                    if isinstance(statval, float):
                        influx_val = statval
                    elif isinstance(statval, str):
                        for key_map in [tfn_key, mode_key, fault_key, pin_key,
                                        lat_pin_key]:
                            if statval in key_map:
                                influx_val = key_map[statval]
                                break
                        else:
                            raise ValueError('Could not convert value for %s="%s"' %
                                                (statkey, statval))
                    elif isinstance(statval, int):
                        if statkey in ['Year', 'Free_upload_positions']:
                            influx_val = float(statval)
                        else:
                            influx_val = int(statval)
                    new_influx_blocks[category]['data'][statkey + '_influx'] = influx_val
            else:  # i.e. category == 'commands':
                if str(self.data['status']['commands']['Azimuth_commanded_position']) != 'nan':
                    acucommand_az = {'timestamp': self.data['status']['summary']['ctime'],
                                        'block_name': 'ACU_commanded_positions_az',
                                        'data': {'Azimuth_commanded_position_influx': self.data['status']['commands']['Azimuth_commanded_position']}
                                        }
                    self.agent.publish_to_feed('acu_commands_influx', acucommand_az)
                if str(self.data['status']['commands']['Elevation_commanded_position']) != 'nan':
                    acucommand_el = {'timestamp': self.data['status']['summary']['ctime'],
                                        'block_name': 'ACU_commanded_positions_el',
                                        'data': {'Elevation_commanded_position_influx': self.data['status']['commands']['Elevation_commanded_position']}
                                        }
                    self.agent.publish_to_feed('acu_commands_influx', acucommand_el)
                if self.acu_config['platform'] == 'satp':
                    if str(self.data['status']['commands']['Boresight_commanded_position']) != 'nan':
                        acucommand_bs = {'timestamp': self.data['status']['summary']['ctime'],
                                            'block_name': 'ACU_commanded_positions_boresight',
                                            'data': {'Boresight_commanded_position_influx': self.data['status']['commands']['Boresight_commanded_position']}
                                            }
                        self.agent.publish_to_feed('acu_commands_influx', acucommand_bs)

        # Only keep blocks that have changed or have new data.
        block_keys = list(new_influx_blocks.keys())
        for k in block_keys:
            if k not in influx_blocks:
                continue
            B, N = influx_blocks[k], new_influx_blocks[k]
            overdue = (N['timestamp'] - B['timestamp'] > MONITOR_MAX_TIME_DELTA)
            changes = any([B['data'][_k] != _v for _k, _v in N['data'].items()])
            if overdue or changes:
                continue
            del new_influx_blocks[k]

        for block in new_influx_blocks.values():
            # Check that we have data (commands and corotator often don't)
            if len(block['data']) > 0:
                self.agent.publish_to_feed('acu_status_influx', block)
        influx_blocks.update(new_influx_blocks)

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
