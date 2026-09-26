import h5py
import json
import numpy as np
import os
import glob
import sys
import so3g
import spt3g
import spt3g.core
import argparse
from ocs import ocs_agent, site_config
from ocs.ocs_twisted import TimeoutLock

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

class BookbinderAgent:
    """
    Class to carry out level0 bookbinding from raw detector and housekeeping (hk) data, producing a single HDF5 book
    """

    def __init__(self, agent, hk_root, det_root):
        self.agent = agent
        self.log = agent.log
        #self.lock = TimeoutLock()
        #
        self.hk_root = hk_root
        self.det_root = det_root
        #self.output_root = None
        self.det_name = None
        self.det_date = None
        self.sess_id = None
        self.obs_end_time = None
        self.compression = 'gzip'
        self.compression_opts = None
        self.to_bind = 'all'
        self.boards_to_include = 'all'
        #
        print(self.hk_root, self.det_root)

    def status_for_binding(self):
        status = True
        for field in [self.hk_root,
                           self.det_root, self.det_name, self.det_date, self.sess_id,
                           self.obs_end_time, self.output_root]:
            if field is None:
                status = status and False
        return status

    @ocs_agent.param('det_name', default='', type=str)
    @ocs_agent.param('det_date', default='', type=str)
    @ocs_agent.param('sess_id', default='', type=str)
    @ocs_agent.param('obs_end_time', default='', type=str)
    @ocs_agent.param('compression', default='gzip', type=str)
    @ocs_agent.param('output_root', default='', type=str)
    def bind(self, session, params):
        self.det_name = params['det_name']
        self.det_date = params['det_date']
        self.sess_id = params['sess_id']
        self.obs_end_time = float(params['obs_end_time'])
        self.compression = params['compression']
        self.output_root = params['output_root']
        #
        self.hk_files = None
        self.det_dir = os.path.join(self.det_root, self.det_name, self.det_date, self.sess_id)
        self.det_time_start = int(self.sess_id)
        self.det_time_end = float(self.obs_end_time)
        self.h5_output = os.path.join(self.output_root, f'level0_{self.sess_id}.h5')
        #
        if not self.status_for_binding():
            return
        #
        self.find_associated_hk_files()
        self.bind_timestream_data(file_mode='w')
        self.bind_hk_data(file_mode='a')
        self.bind_config_data(file_mode='a')
        self.bind_targ_data(file_mode='a')
        self.bind_vna_data(file_mode='a')
        self.bind_log_data(file_mode='a')

        return True, 'Book bound'
        
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
                            frames_group_data.create_dataset('names', shape=len(obj.names), dtype=h5py.string_dtype(), \
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
        for hkf in self.hk_files:
            fr = read_g3_frames_from_file(hkf)
            frames.extend(fr)
            #
        with h5py.File(self.h5_output, file_mode, track_order=True) as h5f:
            hk_group = h5f.create_group('/hk', track_order=True)
            num_frames = len(frames)
            hk_group.attrs['hk_files'] = json.dumps(self.hk_files)
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
                                                                    shape=len(data), dtype=h5py.string_dtype())
                                        block_dsets[name][:] = data
                                    else:
                                        block_dsets[name] = frame_blocks[(i,j)].create_dataset(name, data=data)
                            elif k == 'providers':
                                frame_providers[(i,j)] = frames_group.create_group(f'/hk/frames/{i}/providers/{j}',
                                                                                        track_order=True)
                                items = obj[j].items()
                                provider_dsets = {}
                                for name, data in items:
                                    frame_providers[(i,j)].attrs[name] = data.value


def make_parser(parser=None):
    """Build the argument parser for the Agent."""

    if parser is None: parser = argparse.ArgumentParser()

    # Add options specific to this agent.
    pgroup = parser.add_argument_group('Agent Options')
    pgroup.add_argument('--hk-root', type=str, help='hk root directory')
    pgroup.add_argument('--det-root', type=str, help='detector_data root directory')
    #pgroup.add_argument('--det-date', type=str, help='detector date - subdirectory name')
    #pgroup.add_argument('--sess-id', type=str, help='rfsoc_controller sess_id')
    #pgroup.add_argument('--obs-end-time', type=float, help='observation end time')
    #pgroup.add_argument('--output-root', type=str, help='h5 output root directory')
    #pgroup.add_argument('--log-root', type=str, help='log root directory')
    #pgroup.add_argument('--compression', type=str, help='compression algorithm')
    return parser


def main(args=None):
    parser = make_parser()
    args = site_config.parse_args(agent_class='BookbinderAgent', parser=parser, args=args)
    agent, runner = ocs_agent.init_site_agent(args)
    bookbinder = BookbinderAgent(agent,
                                     hk_root = args.hk_root,
                                     det_root = args.det_root)
    #                                  det_date = args.det_date,
    #                                  sess_id = args.sess_id,
    #                                  obs_end_time = args.obs_end_time,
    #                                  output_root = args.output_root,
    #                                  log_root = args.log_root,
    #                                  compression = args.compression,
    #                                  )
    agent.register_task('bind', bookbinder.bind, blocking=False)
    runner.run(agent, auto_reconnect=True)



if __name__ == '__main__':
    main()

        
