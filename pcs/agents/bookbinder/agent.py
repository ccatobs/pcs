import h5py
import json
import numpy as np
import os
import tempfile
import subprocess
import glob
import sys
import so3g
import spt3g
import spt3g.core
import argparse
import time
import queue
from ocs import ocs_agent, site_config
#from ocs.ocs_twisted import TimeoutLock

def read_g3_frames_from_file(fname, num_frames=None):
    """
    Read a g3 file (fname) and return a list of all the frames within.
    If num_frames is an integer, read and return only that many frames.
    """
    g3f = spt3g.core.G3File(fname)
    frames = []
    if num_frames is None:
        i = 0
        try:
            while True:
                frame = g3f.next()
                frames.append(frame)
                i += 1
        except StopIteration:
            pass
    else:
        try:
            for i in range(num_frames):
                frame = g3f.next()
                frames.append(frame)
        except StopIteration:
            pass
    return frames

def read_and_join_g3_frames_from_directory(dirname, return_filenames=False):
    """
    Extracts frames from all g3 files in a directory and returns a list of frames.
    File names are sorted lexographically before frame extraction.
    """
    files = sorted(glob.glob(os.path.join(dirname, '*.g3')))
    frames = []
    for f in files:
        _frames = read_g3_frames_from_file(f)
        frames.extend(_frames)
    if return_filenames:
        return frames, files
    else:
        return frames

# ------------------------------------------------------------------------------------------------------

class BookbinderAgent:
    """
    Class to carry out level0 bookbinding from raw detector and housekeeping (hk) data, producing a single HDF5 book
    """

    def __init__(self, agent, hk_root, det_root, output_root):
        self.agent = agent
        self.log = agent.log
        self.job_queue = queue.Queue()
        #self.lock = TimeoutLock()
        #
        self.hk_root = hk_root
        self.det_root = det_root
        self.output_root = output_root
        self.det_name = None
        self.det_date = None
        self.sess_id = None
        self.obs_end_time = None
        self.compression = 'gzip'
        self.compression_opts = None
        self.to_bind = 'all'
        self.boards_to_include = 'all'
        self.hk_cadence = 5*60
        self.hk_buffer = 1*60
        #
        print(self.hk_root, self.det_root, self.output_root)

    @ocs_agent.param('det_name', default='', type=str)
    @ocs_agent.param('det_date', default='', type=str)
    @ocs_agent.param('sess_id', default='', type=str)
    @ocs_agent.param('obs_end_time', default='', type=str)
    @ocs_agent.param('compression', default='gzip', type=str)
    @ocs_agent.param('output_root', default='', type=str)
    def add_to_queue(self, session, params):
        job_packet = dict(params)
        self.job_queue.put(job_packet)
        return True, f"Job session {job_packet['sess_id']} successfully queued"

    def status_for_binding(self):
        status = True
        for field in [self.hk_root,
                           self.det_root, self.det_name, self.det_date, self.sess_id,
                           self.obs_end_time, self.output_root]:
            if field is None:
                status = status and False
        return status

    def run_bind_queue(self, session, params):
        session.set_status('running')
        self.log.info('Bookbinder.bind started')
        #
        while session.status == 'running':
            try:
                job = self.job_queue.get(timeout=1.0)
                print(time.time(), job)
            except queue.Empty:
                continue

            self.det_name = job['det_name']
            self.det_date = job['det_date']
            self.sess_id = job['sess_id']
            self.obs_end_time = float(job['obs_end_time'])
            self.compression = job['compression']
            self.hk_files = None
            self.det_dir = os.path.join(self.det_root, self.det_name, self.det_date, self.sess_id)
            self.det_time_start = int(self.sess_id)
            self.det_time_end = float(self.obs_end_time)
            self.h5_output = os.path.join(self.output_root, f'level0_{self.sess_id}.h5')
            #
            #d = threads.deferToThread(self._bind_processing, session)
            #return d
            bind_status, bind_message = self._bind_processing()
            print(bind_status, bind_message)
            verify_status, verify_message = self._verify_book()
            print(verify_status, verify_message)
            status = bind_status and verify_status
            message = bind_message + ' / ' + verify_message
        return status, message

    def _bind_processing(self):
        #
        #if not self.status_for_binding():
        #    return
        #
        print('beginning _bind_processing')
        time.sleep(self.hk_cadence + self.hk_buffer)
        self.find_associated_hk_files()
        self.bind_timestream_data(file_mode='w')
        self.bind_hk_data(file_mode='a')
        self.bind_config_data(file_mode='a')
        self.bind_targ_data(file_mode='a')
        self.bind_vna_data(file_mode='a')
        self.bind_log_data(file_mode='a')
        #
        return True, f'Book bound: {self.sess_id}'

    def _verify_book(self):
        #
        #if not self.status_for_binding():
        #    return
        #
        print('beginning _verify_book')
        verifier = Level0_Verifier(self.hk_root, self.det_root, self.det_name, self.det_date, self.sess_id, \
                                       self.h5_output)
        verification_status = verifier.verify()
        #
        return verification_status, f'Book verified (status={verification_status}): {self.sess_id}'

    def stop_process(self, session, params):
        session.set_status('stopping')
        return True, "Requesting processing loop to stop."
        
    def find_associated_hk_files(self):
        """
        """
        if self.det_time_end is None:
            print('ERROR: det_time_end not yet computed')
            return
        #
        t0 = self.det_time_start
        t1 = self.det_time_end
        #
        t0_short = int(t0) // 100000
        t0_short_prev = t0_short-1
        t0_short_next = t0_short+1
        print(t0_short, t0_short_prev, t0_short_next)
        possible_hk_files = \
          sorted( \
                glob.glob(os.path.join(self.hk_root, str(t0_short_prev), '*.g3')) + \
                glob.glob(os.path.join(self.hk_root, str(t0_short), '*.g3')) + \
                glob.glob(os.path.join(self.hk_root, str(t0_short_next), '*.g3')) \
            )
        hk_starts = sorted([int(os.path.splitext(os.path.split(f)[-1])[0]) for f in possible_hk_files])
        hk_t0_idx = np.searchsorted(hk_starts, t0) - 1  # need preceding file
        hk_t1_idx = np.searchsorted(hk_starts, t1)      # need this file
        self.hk_files = possible_hk_files[hk_t0_idx : hk_t1_idx]
        print(self.hk_files)
        # at this point:
        # (1) make a local tmp directory to rsync the mounted hk_files to
        # (2) assign a new instance variable: self.local_hk_files with the local filenames
        # (3) rsync the mounted hk_files to the local tmp directory
        # (4) rework hk reading code to read from self.local_hk_files
        dtemp = tempfile.mkdtemp()
        self.local_hk_root = os.path.join(dtemp, 'hk')
        self.local_hk_files = [f.replace(self.hk_root, self.local_hk_root) for f in self.hk_files]
        relative_files_to_sync = [f.replace(self.hk_root, '') for f in self.hk_files]
        print(relative_files_to_sync)
        file_list_str = '\n'.join(relative_files_to_sync)
        try:
            result = subprocess.run(
                ['rsync', '-avz', '--files-from=-', self.hk_root, self.local_hk_root],
                input=file_list_str,
                text=True,
                check=True
            )
            print('rsync succeeded')
            print(result.stdout)

        except subprocess.CalledProcessError as e:
            print(f'rsync failed with exit code {e.returncode}')
            print(f'Error details:\n{e.stderr}')
            


    def bind_timestream_data(self, file_mode, t0=None, t1=None):
        #
        if self.boards_to_include == 'all':  
            bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'timestream', 'B*D*')))
        else:  # list like ['B1', 'B2']
            bd_dirs = []
            for board in boards_to_include:
                board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'timestream', board+'D*')))
                bd_dirs.extend(board_dirs)
        #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            self.det_time_end = -sys.maxsize
            for bd_dir in bd_dirs:
                bd = os.path.split(bd_dir)[-1]
                # /timestream/{bd}
                bd_group = h5f.create_group(f'/timestream/{bd}', track_order=True)
                frames, det_files = read_and_join_g3_frames_from_directory(bd_dir, return_filenames=True)
                num_frames = len(frames)
                bd_group.attrs['det_files'] = json.dumps(det_files)
                bd_group.attrs['num_frames'] = num_frames
                # /timestream/{bd}/frames
                frames_group = h5f.create_group(f'/timestream/{bd}/frames', track_order=True)
                #
                for i,frame in enumerate(frames):
                    frame_type = str(frame.type)
                    # /timestream/{bd}/frames/{i}
                    frame_i = frames_group.create_group(f'/timestream/{bd}/frames/{i}', track_order=True)
                    frame_i.attrs['frame_type'] = frame_type
                    #
                    for k in frame.keys():
                        obj = frame[k]
                        obj_type = type(obj)
                        if obj_type in [int, float, str]:
                            frame_i.attrs[k] = obj
                        elif obj_type == spt3g.core.G3Time:
                            frame_i.attrs[k] = obj.time
                        elif obj_type == spt3g.core.G3VectorInt:
                            frame_i.create_dataset(k, data=np.array(obj), dtype=np.int32, track_order=True)
                        elif obj_type == so3g.G3SuperTimestream:
                            # data group
                            frames_group_data = frames_group.create_group(f'/timestream/{bd}/frames/{i}/data', \
                                                                              track_order=True)
                            # times
                            frames_group_data.create_dataset('times', data=obj.times, track_order=True)
                            # names
                            frames_group_data.create_dataset('names', data=obj.names, dtype=h5py.string_dtype(), \
                                                            track_order=True)
                            # data
                            kwa = {
                                'name': 'data',
                                'data': obj.data,
                                'compression': self.compression,
                                'track_order': True
                            }
                            if self.compression and self.compression_opts is not None:
                                kwa['compression_opts'] = self.compression_opts
                            #frames_group_data.create_dataset('data', data=obj.data, compression='szip', track_order=True)
                            frames_group_data.create_dataset(**kwa)
                            #
    
    def bind_config_data(self, file_mode, t0=None, t1=None):
        #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            config_group = h5f.create_group('/config')
            #
            yaml_files = glob.glob(os.path.join(self.det_dir, 'config', '*.yaml'))
            for yf in yaml_files:
                yf_file = os.path.split(yf)[-1]
                with open(yf, 'r', encoding='utf-8') as f:
                    yaml_text = f.read()
                config_group.attrs[yf_file] = np.bytes_(yaml_text)
            #
            if self.boards_to_include == 'all':
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'config', 'B*D*')))
            else: # list like ['B1', 'B2']
                bd_dirs = []
                for board in boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'config', board+'D*')))
                    bd_dirs.extend(board_dirs)
            #
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                config_group_bd = h5f.create_group(f'/config/{bd}')
                yaml_files = glob.glob(os.path.join(self.det_dir, 'config', bd, '*.yaml'))
                for yf in yaml_files:
                    yf_file = os.path.split(yf)[-1]
                    with open(yf, 'r', encoding='utf-8') as f:
                        yaml_text = f.read()
                    config_group_bd.attrs[yf_file] = np.bytes_(yaml_text)
                #
                bd_combs_dir = os.path.join(self.det_dir, 'config', bd, 'combs')
                config_group_bd_combs = h5f.create_group(f'/config/{bd}/combs')
                npy_files = glob.glob(os.path.join(bd_combs_dir, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    config_group_bd_combs.create_dataset(npyf_file, data=data, track_order=True)
                #
                bd_res_dir = os.path.join(self.det_dir, 'config', bd, 'res')
                config_group_bd_res = h5f.create_group(f'/config/{bd}/res')
                npy_files = glob.glob(os.path.join(bd_res_dir, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    config_group_bd_res.create_dataset(npyf_file, data=data, track_order=True)
                    # NOTE: bd/res is currently empty in the test data

    def bind_targ_data(self, file_mode, t0=None, t1=None):
        #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            targ_group = h5f.create_group('/targ')
            #
            if self.boards_to_include == 'all':  
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'targ', 'B*D*')))
            else:  # list like ['B1', 'B2']
                bd_dirs = []
                for board in boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'targ', board+'D*')))
                    bd_dirs.extend(board_dirs)
            #
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                targ_group_bd = h5f.create_group(f'/targ/{bd}')
                npy_files = glob.glob(os.path.join(bd_full, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    targ_group_bd.create_dataset(npyf_file, data=data, track_order=True)

    def bind_vna_data(self, file_mode, t0=None, t1=None):
        #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            vna_group = h5f.create_group('/vna')
            #
            if self.boards_to_include == 'all':
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'vna', 'B*D*')))
            else:    # list like ['B1', 'B2']
                bd_dirs = []
                for board in boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'vna', board+'D*')))
                    bd_dirs.extend(board_dirs)
            #
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                vna_group_bd = h5f.create_group(f'/vna/{bd}')
                npy_files = glob.glob(os.path.join(bd_full, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    vna_group_bd.create_dataset(npyf_file, data=data, track_order=True)

    def bind_log_data(self, file_mode):
        #
        # log directory in current test data is empty, so just creating an empty h5 group for now
        #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            log_group = h5f.create_group('/log')

    def bind_hk_data(self, file_mode, t0=None, t1=None):
        #
        frames = []
        for hkf in self.local_hk_files:
            fr = read_g3_frames_from_file(hkf)
            frames.extend(fr)
            #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            hk_group = h5f.create_group('/hk', track_order=True)
            num_frames = len(frames)
            hk_group.attrs['hk_files'] = json.dumps(self.hk_files)
            hk_group.attrs['local_hk_files'] = json.dumps(self.local_hk_files)
            hk_group.attrs['num_frames'] = num_frames
            frames_group = h5f.create_group('/hk/frames', track_order=True)
            #
            for i,frame in enumerate(frames):
                frame_type = str(frame.type)
                if 'start_time' in frame.keys():  # start of g3 file - even if time is outside (t0,t1)
                    pass
                elif 'timestamp' in frame.keys():
                    if (t0 is not None) and (t1 is not None):
                        ftime = frame['timestamp']
                        if (ftime < t0) or (ftime > t1):
                            print(f'timestamp not in specified time window: {(t0, t1)}')
                            continue
                else:
                    print(f'frame {i} data not recognized')
                    continue
                #
                frame_i = frames_group.create_group(f'/hk/frames/{i}', track_order=True)
                frame_i.attrs['frame_type'] = frame_type
                if 'providers' in frame.keys():
                    frame_i_providers = frames_group.create_group(f'/hk/frames/{i}/providers', track_order=True)
                if 'blocks' in frame.keys():
                    frame_i_blocks = frames_group.create_group(f'/hk/frames/{i}/blocks', track_order=True)
                #
                for k in frame.keys():
                    obj = frame[k]
                    obj_type = type(obj)
                    if obj_type in [int, float, str]:
                        frame_i.attrs[k] = obj
                    else:
                        obj_len = len(obj)
                        frame_providers = {}
                        frame_blocks = {}
                        if 'blocks' in frame.keys():
                            frame_i.attrs['num_blocks'] = len(frame['blocks'])
                        if 'providers' in frame.keys():
                            frame_i.attrs['num_providers'] = len(frame['providers'])
                        for j in range(obj_len):
                            if k == 'blocks':
                                frame_blocks[(i,j)] = frames_group.create_group(f'/hk/frames/{i}/blocks/{j}',
                                                                                        track_order=True)
                                #
                                if type(obj[j]) == spt3g.core.G3TimesampleMap:
                                    times = frame_blocks[(i,j)].create_dataset('times', data=obj[j].times)
                                #
                                items = obj[j].items()
                                block_dsets = {}
                                for name, data in items:
                                    if type(data) == spt3g.core.G3VectorString:
                                        block_dsets[name] = frame_blocks[(i,j)].create_dataset(name,
                                                                    data=data, dtype=h5py.string_dtype())
                                        #block_dsets[name] = frame_blocks[(i,j)].create_dataset(name,
                                        #                            shape=len(data), dtype=h5py.string_dtype())
                                        #block_dsets[name][:] = data
                                    else:
                                        block_dsets[name] = frame_blocks[(i,j)].create_dataset(name, data=data)
                            elif k == 'providers':
                                frame_providers[(i,j)] = frames_group.create_group(f'/hk/frames/{i}/providers/{j}',
                                                                                        track_order=True)
                                items = obj[j].items()
                                provider_dsets = {}
                                for name, data in items:
                                    frame_providers[(i,j)].attrs[name] = data.value

# ------------------------------------------------------------------------------------------------------

class Level0_Verifier:
    """
    """
    def __init__(self, hk_root, det_root, det_name, det_date, sess_id, h5_file):
        self.hk_root = hk_root
        self.det_root = det_root
        self.det_name = det_name
        self.det_date = det_date
        self.sess_id = sess_id
        self.h5_file = h5_file
        #
        self.boards_to_include = 'all'
        self.hk_files = None
        self.det_dir = os.path.join(det_root, det_name, det_date, sess_id)
        self.det_time_start = int(sess_id)
        self.det_time_end = None
        self.not_matching = {}
        self.matching = {}
        #

    def verify(self):
        verified = True
        self.verification_log = ''
        #
        try:
            status = self.verify_timestream_data_against_h5()
            if status != 0:
                self.verification_log = self.verification_log + 'timestream verification failed' + '\n'
                self.verification_log = self.verification_log + f'timetream status={status}' + '\n'
                self.verification_log = self.verification_log + '\n'
                verified = False
        except Exception as e:
            self.verification_log = self.verification_log + 'timestream verification failed' + '\n'
            self.verification_log = self.verification_log + f'Exception Type: {type(e)}' + '\n'
            self.verification_log = self.verification_log + f'Exception Message: {e}' + '\n'
            self.verification_log = self.verification_log + '\n'
            verified = False
        #
        try:
            status = self.verify_hk_data_against_h5()
            if status != 0:
                self.verification_log = self.verification_log + 'hk verification failed' + '\n'
                self.verification_log = self.verification_log + f'hk status={status}' + '\n'
                self.verification_log = self.verification_log + '\n'
                verified = False
        except Exception as e:
            self.verification_log = self.verification_log + 'hk verification failed' + '\n'
            self.verification_log = self.verification_log + f'Exception Type: {type(e)}' + '\n'
            self.verification_log = self.verification_log + f'Exception Message: {e}' + '\n'
            self.verification_log = self.verification_log + '\n'
            verified = False
        #
        try:
            status = self.verify_config_data_against_h5()
            if status != 0:
                self.verification_log = self.verification_log + 'config verification failed' + '\n'
                self.verification_log = self.verification_log + f'config status={status}' + '\n'
                self.verification_log = self.verification_log + '\n'
                verified = False
        except Exception as e:
            self.verification_log = self.verification_log + 'config verification failed' + '\n'
            self.verification_log = self.verification_log + f'Exception Type: {type(e)}' + '\n'
            self.verification_log = self.verification_log + f'Exception Message: {e}' + '\n'
            self.verification_log = self.verification_log + '\n'
            verified = False
        #
        try:
            status = self.verify_targ_data_against_h5()
            if status != 0:
                self.verification_log = self.verification_log + 'targ verification failed' + '\n'
                self.verification_log = self.verification_log + f'targ status={status}' + '\n'
                self.verification_log = self.verification_log + '\n'
                verified = False
        except Exception as e:
            self.verification_log = self.verification_log + 'targ verification failed' + '\n'
            self.verification_log = self.verification_log + f'Exception Type: {type(e)}' + '\n'
            self.verification_log = self.verification_log + f'Exception Message: {e}' + '\n'
            self.verification_log = self.verification_log + '\n'
            verified = False
        #
        try:
            status = self.verify_vna_data_against_h5()
            if status != 0:
                self.verification_log = self.verification_log + 'vna verification failed' + '\n'
                self.verification_log = self.verification_log + f'vna status={status}' + '\n'
                self.verification_log = self.verification_log + '\n'
                verified = False
        except Exception as e:
            self.verification_log = self.verification_log + 'vna verification failed' + '\n'
            self.verification_log = self.verification_log + f'Exception Type: {type(e)}' + '\n'
            self.verification_log = self.verification_log + f'Exception Message: {e}' + '\n'
            self.verification_log = self.verification_log + '\n'
            verified = False
        #
        status = (verified) and (len(self.not_matching)==0)
        if status is True:
            self.verification_file = f'{self.h5_file}'.replace('.', '_')
            self.verification_file = self.verification_file + '_passed.txt'
            with open(self.verification_file, 'w') as f:
                h5_file_base = os.path.split(self.h5_file)[-1]
                print(f'{h5_file_base} passed all verification checks', file=f)
        else:
            self.verification_file = f'{self.h5_file}'.replace('.', '_')
            self.verification_file = self.verification_file + '_failed.txt'
            with open(self.verification_file, 'w') as f:
                print(self.verification_log, file=f)
        return status

    def verify_hk_data_against_h5(self):
        #
        with h5py.File(self.h5_file, 'r') as h5f:
            json_hkf = h5f['hk'].attrs['hk_files']
            source_hk_files = json.loads(json_hkf)
            self.hk_files = []
            for f in source_hk_files:
                p0, p1 = os.path.split(f)
                hkdir = os.path.split(p0)[-1]
                hkfile = p1
                self.hk_files.append(os.path.join(self.local_hk_root, hkdir, hkfile))
        #
        frames = []
        for hkf in self.hk_files:
            fr = read_g3_frames_from_file(hkf)
            frames.extend(fr)
        if len(frames) == 0:
            status = 1
            return status
        #
        expected_keys = {('address', str),
                            ('block_names', spt3g.core.G3VectorString),
                            ('blocks', spt3g.core.G3VectorFrameObject),
                            ('description', str),
                            ('hkagg_type', int),
                            ('hkagg_version', int),
                            ('prov_id', int),
                            ('provider_session_id', str),
                            ('providers', spt3g.core.G3VectorFrameObject),
                            ('session_id', int),
                            ('start_time', float),
                            ('timestamp', float)}
        all_frame_keys = set()
        for i_,f_ in enumerate(frames):
            for k in f_.keys():
                all_frame_keys.add((k, type(f_[k])))
        if all_frame_keys != expected_keys:
            print('WARNING: detected keys not equal to expected keys')
        #
        with h5py.File(self.h5_file, 'r') as h5f:
            h5f_key = '/hk/frames'
            for i,frame in enumerate(frames):
                h5f_key = f'/hk/frames/{i}'
                frame_type = str(frame.type)
                if frame_type != h5f[h5f_key].attrs['frame_type']:
                    self.not_matching[h5f_key] = (frame_type, h5f[h5f_key].attrs['frame_type'])
                else:
                    self.matching[h5f_key] = (frame_type, h5f[h5f_key].attrs['frame_type'])
                #
                for k in sorted(frame.keys()):
                    obj = frame[k]
                    obj_type = type(obj).__name__
                    if k == 'blocks':
                        #num_blocks = len(frame['blocks'])
                        # each block is a spt3g.core.G3TimesampleMap that contains one or more named co-sampled vectors
                        # with times and data
                        for bi, b in enumerate(frame['blocks']):
                            h5f_key = f'/hk/frames/{i}/blocks/{bi}'
                            times_h5f_key = f'{h5f_key}/times'
                            if not (h5f[times_h5f_key][:] == b.times[:]).all():
                                self.not_matching[times_h5f_key] = (h5f[times_h5f_key][:], b.times)
                            else:
                                self.matching[times_h5f_key] = (h5f[times_h5f_key][:], b.times)
                            for name, data in b.items():
                                data_h5f_key = f'{h5f_key}/{name}'
                                adata = np.array(data)
                                # need to treat strings differently in hdf5
                                if type(data) == spt3g.core.G3VectorString:
                                    if not (h5f[data_h5f_key].asstr()[:] == data[:]).all():
                                        self.not_matching[data_h5f_key] = (h5f[data_h5f_key].asstr(), data)
                                    else:
                                        self.matching[data_h5f_key] = (h5f[data_h5f_key].asstr(), data)
                                else:
                                    if not (h5f[data_h5f_key][:] == data[:]).all():
                                        self.not_matching[data_h5f_key] = (h5f[data_h5f_key][:], data)
                                    else:
                                        self.matching[data_h5f_key] = (h5f[data_h5f_key][:], data)
                    #
                    elif k == 'block_names':
                        num_block_names = len(frame['block_names'])
                        #print(f'{k}: {obj_type}, {num_block_names} elements', file=output)
                        #print('num_block_names', num_block_names, sep=',', file=output)
                        #for j in range(num_block_names):
                        #    print(f'Group: /hk/frames/{i}/block_names/{j}', file=output)
                        #    print(f'--- {frame['block_names'][j]}', file=output)
                    #
                    elif k == 'providers':
                        #num_providers = len(frame['providers'])
                        for pi, p in enumerate(frame['providers']):
                            h5f_key = f'/hk/frames/{i}/providers/{pi}'
                            for name, data in p.items():
                                # providers are stored in attrs for h5 providers/{pi} Group
                                # after extracting the .value attribute
                                # need to extract g3 data.value here to do comparison
                                h5f_val = h5f[h5f_key].attrs[name]
                                attrs_h5f_key = f'{h5f_key}:attrs/{name}'
                                if data.value != h5f_val:
                                    self.not_matching[attrs_h5f_key] = (data.value, h5f_val)
                                else:
                                    self.matching[attrs_h5f_key] = (data.value, h5f_val)
                    #
                    else:  # should be a scalar value in attrs
                        #print(f'{k}: {obj_type}, {obj}', file=output)
                        if k == 'block_names':
                            continue
                        h5f_key = f'/hk/frames/{i}'
                        h5f_val = h5f[h5f_key].attrs[k]
                        attrs_h5f_key = h5f_key + ':attrs/' + k
                        if obj != h5f_val:
                            self.not_matching[attrs_h5f_key] = (obj, h5f_val)
                        else:
                            self.matching[attrs_h5f_key] = (obj, h5f_val)
        status = 0
        return status

    def verify_timestream_data_against_h5(self):
        #
        if self.boards_to_include != 'all':  # list like ['B1', 'B2']
            bd_dirs = []
            for board in self.boards_to_include:
                board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'timestream', board+'D1')))
                bd_dirs.extend(board_dirs)
        else:
            bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'timestream', 'B*D*')))
        if len(bd_dirs) == 0:
            status = 1
            return status
        #
        with h5py.File(self.h5_file, 'r') as h5f:
            #
            h5f_key = '/timestream'            
            for bd_dir in bd_dirs:
                bd = os.path.split(bd_dir)[-1]
                h5f_key = f'/timestream/{bd}'
                json_timestream_files = h5f[h5f_key].attrs['det_files']
                det_files = json.loads(json_timestream_files)
                num_frames = int(h5f[h5f_key].attrs['num_frames'])
                frames = read_and_join_g3_frames_from_directory(bd_dir)
                #
                for i,frame in enumerate(frames):
                    h5f_key = f'/timestream/{bd}/frames/{i}'
                    frame_type = str(frame.type)
                    #
                    for k in sorted(frame.keys()):
                        obj = frame[k]
                        #obj_type = type(obj)
                        obj_type = type(obj).__name__
                        if k == 'packet_counts':
                            h5f_key = f'/timestream/{bd}/frames/{i}/{k}'
                            if not (h5f[h5f_key][:] == obj[:]).all():
                                self.not_matching[h5f_key] = (h5f[h5f_key], obj[:])                        #
                        elif k == 'data':
                            h5f_key = f'/timestream/{bd}/frames/{i}/{k}/times'
                            if not (h5f[h5f_key][:] == obj.times[:]).all():
                                self.not_matching[h5f_key] = (h5f[h5f_key][:], obj.times[:])
                            #
                            h5f_key = f'/timestream/{bd}/frames/{i}/{k}/data'
                            if not (h5f[h5f_key][:] == obj.data[:]).all():
                                self.not_matching[h5f_key] = (h5f[h5f_key][:], obj.data[:])
                            #
                            h5f_key = f'/timestream/{bd}/frames/{i}/{k}/names'
                            g3_arr = np.array(obj.names[:], dtype=object)
                            hdf5_arr = np.char.decode(h5f[h5f_key][:].astype('S'), 'utf-8')
                            if not (g3_arr == hdf5_arr).all():
                                self.not_matching[h5f_key] = (h5f[h5f_key][:], obj.names[:])
                        #
                        else:  # should be a scalar value in attrs
                            h5f_key = f'/timestream/{bd}/frames/{i}'
                            h5f_val = h5f[h5f_key].attrs[k]
                            if type(obj) == spt3g.core.G3Time:
                                obj_val = int(obj)
                            else:
                                obj_val = obj
                            attrs_h5f_key = h5f_key + ':attrs/' + k
                            if obj_val != h5f_val:
                                self.not_matching[attrs_h5f_key] = (obj, h5f_val)
                            else:
                                self.matching[attrs_h5f_key] = (obj, h5f_val)
        status = 0
        return status

    def verify_config_data_against_h5(self):
        #
        with h5py.File(self.h5_file, 'r') as h5f:
        #
            h5f_key = '/config'
            yaml_files = glob.glob(os.path.join(self.det_dir, 'config', '*.yaml'))
            for yf in yaml_files:
                yf_file = os.path.split(yf)[-1]
                with open(yf, 'r', encoding='utf-8') as f:
                    yaml_text = f.read()
                config_yf = np.bytes_(yaml_text)
                #hdf5_yf = np.char.decode(h5f[h5f_key].attrs[yf_file].astype('S'), 'utf-8')
                hdf5_yf = h5f[h5f_key].attrs[yf_file]
                if not (config_yf == hdf5_yf):
                    #self.not_matching[h5f_key] = (h5f[h5f_key].attrs[yf_file], config_yf)
                    self.not_matching[h5f_key] = (hdf5_yf, config_yf)
            #
            if self.boards_to_include != 'all':  # list like ['B1', 'B2']
                bd_dirs = []
                for board in self.boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'config', board+'D*')))
                    bd_dirs.extend(board_dirs)
            else:
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'config', 'B*D*')))
            if len(bd_dirs) == 0:
                status = 1
                return status
            #
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                h5f_key = f'/config/{bd}'
                yaml_files = glob.glob(os.path.join(self.det_dir, 'config', bd, '*.yaml'))
                for yf in yaml_files:
                    yf_file = os.path.split(yf)[-1]
                    with open(yf, 'r', encoding='utf-8') as f:
                        yaml_text = f.read()
                    config_yf = np.bytes_(yaml_text)
                    hdf5_yf = h5f[h5f_key].attrs[yf_file]
                    if not (config_yf == hdf5_yf):
                        self.not_matching[h5f_key] = (hdf5_yf, config_yf)
                #
                bd_combs_dir = os.path.join(self.det_dir, 'config', bd, 'combs')
                h5f_key = f'/config/{bd}/combs'
                npy_files = glob.glob(os.path.join(bd_combs_dir, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    h5f_key = f'/config/{bd}/combs/{npyf_file}'
                    if not (h5f[h5f_key][:] == data[:]).all():
                        self.not_matching[h5f_key] = (h5f[h5f_key][:], data[:])
                    #
        status = 0
        return status
                    
    def verify_targ_data_against_h5(self):
        #
        with h5py.File(self.h5_file, 'r') as h5f:
            h5f_key = '/targ'
            #
            if self.boards_to_include != 'all':  # list like ['B1', 'B2']
                bd_dirs = []
                for board in self.boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'targ', board+'D*')))
                    bd_dirs.extend(board_dirs)
            else:
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'targ', 'B*D*')))
            if len(bd_dirs) == 0:
                status = 1
                return status
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                h5f_key = f'/targ/{bd}'
                npy_files = glob.glob(os.path.join(bd_full, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    h5f_key = f'/targ/{bd}/{npyf_file}'
                    if not (h5f[h5f_key][:] == data[:]).all():
                        self.not_matching[h5f_key] = (h5f[h5f_key][:], data[:])
                    #

        status = 0
        return status

    def verify_vna_data_against_h5(self):
        #
        with h5py.File(self.h5_file, 'r') as h5f:
            h5f_key = '/vna'
            #
            if self.boards_to_include != 'all':  # list like ['B1', 'B2']
                bd_dirs = []
                for board in self.boards_to_include:
                    board_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'vna', board+'D*')))
                    bd_dirs.extend(board_dirs)
            else:
                bd_dirs = sorted(glob.glob(os.path.join(self.det_dir, 'vna', 'B*D*')))
            if len(bd_dirs) == 0:
                status = 1
                return status
            #
            for bd_full in bd_dirs:
                bd = os.path.split(bd_full)[-1]
                h5f_key = f'/vna/{bd}'
                npy_files = glob.glob(os.path.join(bd_full, '*.npy'))
                for npyf in npy_files:
                    npyf_file = os.path.split(npyf)[-1]
                    data = np.load(npyf)
                    h5f_key = f'/vna/{bd}/{npyf_file}'
                    if not (h5f[h5f_key][:] == data[:]).all():
                        self.not_matching[h5f_key] = (h5f[h5f_key][:], data[:])
                    #
        status = 0
        return status                    
                                       
# ------------------------------------------------------------------------------------------------------

def make_parser(parser=None):
    """Build the argument parser for the Agent."""

    if parser is None: parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--hk-root', type=str, help='hk root directory')
    pgroup.add_argument('--det-root', type=str, help='detector_data root directory')
    pgroup.add_argument('--output-root', type=str, help='h5 output root directory')
    #pgroup.add_argument('--det-date', type=str, help='detector date - subdirectory name')
    #pgroup.add_argument('--sess-id', type=str, help='rfsoc_controller sess_id')
    #pgroup.add_argument('--obs-end-time', type=float, help='observation end time')
    #pgroup.add_argument('--log-root', type=str, help='log root directory')
    #pgroup.add_argument('--compression', type=str, help='compression algorithm')
    return parser


def main(args=None):
    parser = make_parser()
    args = site_config.parse_args(agent_class='BookbinderAgent', parser=parser, args=args)
    agent, runner = ocs_agent.init_site_agent(args)
    bookbinder = BookbinderAgent(agent,
                                     hk_root = args.hk_root,
                                     det_root = args.det_root,
                                     output_root = args.output_root)
    #                                  det_date = args.det_date,
    #                                  sess_id = args.sess_id,
    #                                  obs_end_time = args.obs_end_time,
    #                                  output_root = args.output_root,
    #                                  log_root = args.log_root,
    #                                  compression = args.compression,
    #                                  )
    #agent.register_task('bind', bookbinder.bind, blocking=False)
    #agent.register_task('bind', bookbinder.bind)
    agent.register_task('add_to_queue', bookbinder.add_to_queue)
    agent.register_process('run_pipeline', bookbinder.run_bind_queue, bookbinder.stop_process)
    agent.register_task('stop_process', bookbinder.stop_process)
    runner.run(agent, auto_reconnect=True)



if __name__ == '__main__':
    main()

        
