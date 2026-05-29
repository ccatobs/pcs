@inlineCallbacks
def _run_track(self, session, point_gen, step_time, stop_accel=0.5, track_axes=['az'],
                point_batch_count=None, free_form=False, unabort_failure=False):
    """Run a ProgramTrack track scan, with points provided by a
    generator.

    Args:
        session: session object for the parent operation.
        point_gen: generator that yields points
        step_time: the minimum time between point track points.
        This is used to guarantee that points are uploaded
        sufficiently in advance for the servo unit to process
        them.
        stop_accel: float acceleration value used to generate the
        stop PointTrack for the scan. If _run_track is called from
        generate_scan, stop_accel is equal to the az_accel for the
        scan. By default the stop will be generated with stop_accel=0.5.
        track_axes: list of strings indicating which axes ('az',
        'el') should be put in ProgramTrack mode.  Axes not
        included here will not have their mode changed.
        point_batch_count: number of points to include in batch
        uploads.  This parameter can be used to increase the value
        beyond the minimum set internally based on step_time.
        free_form: if True, disable ACU linear interpolation and
        turn-around profiling.
        unabort_failure: if True don't fail on a bad exit.

    Returns:
        Tuple (success, msg) where success is a bool.

    """
    # The approximate loop time
    LOOP_STEP = 0.1  # seconds

    # Time to allow for initial ProgramTrack transition.
    MAX_PROGTRACK_SET_TIME = 5.

    # Minimum number of points to have in the stack.  While the
    # docs strictly require 4, this number should be at least 1
    # more than that to allow for rounding when we are setting the
    # refill threshold.
    MIN_STACK_POP = 6  # points
    MAX_ALLOWABLE_FREE_POSITIONS = FULL_STACK - MIN_STACK_POP

    # Minimum amount of time (seconds), in advance, to populate
    # the trajectory.  In cases where step_time is short, this
    # creates a longer track window to survive agent outages.
    # (The cost is that stopping a scan may take a little longer.)
    MIN_STACK_ADVANCE_TIME = 3.

    # Special error bits to watch here
    PTRACK_FAULT_KEYS = [
        'ProgramTrack_position_failure',
        'Track_start_too_early',
        'Turnaround_accel_too_high',
        'Turnaround_time_too_short',
    ]

    if free_form:
        init_cmds = [
            ('Clear Stack', 0.),
            ('Set Profiler Off', 0.),
            ('Set Interpolation Spline', 0.5)
        ]
    else:
        init_cmds = [
            ('Clear Stack', 0.),
            ('Set Profiler On', 0.),
            ('Set Interpolation Linear', 0.5)
        ]

    with self.azel_lock.acquire_timeout(0, job='generate_scan') as acquired:
        if not acquired:
            return False, f"Operation failed: {self.azel_lock.job} is running."
        if session.status not in ['starting', 'running']:
            return False, "Operation aborted before motion began."

        for _c, _d in init_cmds:
            resp = yield self.acu_control.http.Command(
                'DataSets.CmdTimePositionTransfer', _c)
            if resp != b'OK, Command executed.':
                return False, f"Failed to init: {_c}"
            if _d > 0:
                yield dsleep(_d)

        if track_axes is not None and len(track_axes) > 0:
            assert ([_ax in ['az', 'el'] for _ax in track_axes])
            mode_args = {_ax: 'ProgramTrack' for _ax in track_axes}
            yield self._set_modes(**mode_args)

        yield dsleep(0.1)

        # Values for mode are:
        # - 'go' -- keep uploading points (unless there are no more to upload).
        # - 'stop' -- do not request more points from generator;
        #   finish the ones that are already in "points", let the stack empty,
        #   and wait for settling condition.
        # - 'abort' -- do not upload more points; exit loop with error; wait
        #   a few seconds and clear the stack.
        mode = 'go'

        point_prov = sh.PointProvider(point_gen)
        last_mode = None
        last_upload_az = None
        start_time = time.time()
        got_progtrack = False
        faults = {}
        got_points_in = False
        first_upload_time = None
        last_uploaded_timestamp = 0
        wait_stop_timeout = None

        prog_track_err = False
        stop_message = ""
        while True:
            now = time.time()
            current_modes = {'Az': self.data['status']['summary']['Azimuth_mode'],
                                'El': self.data['status']['summary']['Elevation_mode'],
                                'Remote': self.data['status']['platform_status']['Remote_mode']}
            az_state = {'pos': self.data['status']['summary']['Azimuth_current_position'],
                        'vel': self.data['status']['summary']['Azimuth_current_velocity']}
            free_positions = self.data['status']['summary']['Free_upload_positions']

            # Use this var to detect case where we're uploading
            # points but ACU is quietly dumping them because the
            # vel is too high.
            got_points_in = got_points_in \
                or (got_progtrack and free_positions < FULL_STACK)

            if last_mode != mode:
                self.log.info(f'scan mode={mode}, line_buffer={len(point_prov)}, track_free={free_positions}')
                last_mode = mode

            for k in PTRACK_FAULT_KEYS:
                if k not in faults and self.data['status']['ACU_failures_errors'].get(k):
                    self.log.info('Fault during track: "{k}"', k=k)
                    faults[k] = True

            if mode != 'abort':
                # Reasons we might decide to abort ...
                if current_modes['Az'] == 'ProgramTrack':
                    got_progtrack = True
                else:
                    if got_progtrack:
                        self.log.warn('Unexpected exit from ProgramTrack mode!')
                        if mode == 'stop':
                            prog_track_err = True
                        mode = 'abort'
                    elif now - start_time > MAX_PROGTRACK_SET_TIME:
                        self.log.warn('Failed to set ProgramTrack mode in a timely fashion.')
                        mode = 'abort'
                if not got_points_in and (first_upload_time is not None) \
                    and (now - first_upload_time > 10):
                    self.log.warn('ACU seems to be dumping our track. Vel too high?')
                    mode = 'abort'
                if current_modes['Remote'] == 0:
                    self.log.warn('ACU no longer in remote mode!')
                    mode = 'abort'
                if session.status == 'stopping' and mode not in ['stop', 'abort']:
                    mode = 'stop'
                    stop_message = 'User-requested stop.'
                    point_prov.stop(free_form, stop_accel)

            if mode == 'abort':
                point_prov.abort()

            # Is it time to upload more lines?
            # This happens when the current time of uploaded points is less
            # than the MIN_STACK_ADVANCE_TIME.
            # (Meaning we have less than the minimum time worth of points uploaded).
            # Or if the total number of free positions is higher than the MAX_ALLOWABLE_FREE_POSITIONS.
            # (Meaning we haven't uploaded at least the minimum number of points)
            if ((last_uploaded_timestamp - time.time()) <= MIN_STACK_ADVANCE_TIME) \
                or (free_positions > MAX_ALLOWABLE_FREE_POSITIONS):

                upload_lines = []
                # Grab points from point_prov until our last point is at least
                # 2 * MIN_STACK_ADVANCE_TIME seconds from now.
                # If that isn't enough points to have MIN_STACK_POP_TIME amount of points uploaded,
                # Keep grabbing points until we have enough.
                while not point_prov.is_empty() and (len(upload_lines) == 0
                                                        or upload_lines[-1].timestamp - time.time() < (2 * MIN_STACK_ADVANCE_TIME)
                                                        or (free_positions - len(upload_lines) > MAX_ALLOWABLE_FREE_POSITIONS)):

                    upload_lines.append(point_prov.pop())

                # If the last line has a "group" flag, keep transferring lines.
                while not point_prov.is_empty() and len(upload_lines) and upload_lines[-1].group_flag != 0:
                    upload_lines.append(point_prov.pop())

                if point_prov.is_empty() and mode == 'go':
                    mode = 'stop'
                    stop_message = 'Stop due to end of the planned track.'

                if len(upload_lines):
                    # Discard the group flag and upload all.
                    text = sh.get_track_points_text(
                        upload_lines, timestamp_offset=3, text_block=True)
                    for attempt in range(5):
                        _dt = time.time()
                        try:
                            # This seems to return b'Ok.' no matter ~what,
                            # so not much point checking it.
                            yield self.acu_control.http.UploadPtStack(text)
                            break
                        except Exception as err:
                            _dt = time.time() - _dt
                            self.log.warn(f'Upload {len(upload_lines)} failed (attempt {attempt}) after {_dt:.3f} seconds')
                            self.log.warn('Exception was: {err}', err=err)
                    else:
                        raise RuntimeError('Upload fail.')
                    if first_upload_time is None:
                        first_upload_time = time.time()
                    last_upload_az = upload_lines[-1].az

                    # Track the timestamp of the current upload.
                    last_uploaded_timestamp = upload_lines[-1].timestamp

            if point_prov.is_empty() and free_positions >= FULL_STACK - 1:
                if mode == 'stop':
                    if wait_stop_timeout is None:
                        self.log.info('Stack is empty; waiting for settling...')
                        wait_stop_timeout = now + 20.
                    elif now > wait_stop_timeout:
                        self.log.warn('Graceful stop condition not met in a timely fashion.')
                        mode = 'abort'
                    # Await safe exit condition.
                    pos_ok = last_upload_az is None or (
                        abs(az_state['pos'] - last_upload_az) < 0.01)
                    vel_ok = abs(abs(az_state['vel']) < .01)
                    if pos_ok and vel_ok:
                        break
                else:
                    self.log.warn('Somehow ran out of points!')
                    break

            yield dsleep(LOOP_STEP)

        # Go to Stop mode?
        # yield self.acu_control.stop()

        # Wait a couple more seconds and clear the stack.
        yield dsleep(2)
        yield self.acu_control.http.Command('DataSets.CmdTimePositionTransfer',
                                            'Clear Stack')

    if mode == 'abort':
        if unabort_failure and prog_track_err:
            return True, 'Problems on shutdown but close enough.'
        return False, 'Problems during scan'
    return True, f'Scan ended. {stop_message}'