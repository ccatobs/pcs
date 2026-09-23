import so3g
from spt3g import core
import numpy as np
import matplotlib.pyplot as plt
import glob, os

def load_timestream_from_rfsoc_g3_file(fname):
    """
    Extracts data from a single rfsoc g3 file (fname) 
    and returns session_id, stream_id,times,names,data for the full file.
    Assumes names are the same for each frame in file.
    """
    frames = []
    for frame in core.G3File(fname):
        frames.append(frame)
    to_store = []
    for i in range(len(frames)):
        if(frames[i].type==core.G3FrameType.Scan):
            to_store.append(frames[i]['data'])

    combined_times = np.hstack(list(to_store[i].times for i in range(len(to_store))))
    combined_data = np.hstack(list(to_store[i].data for i in range(len(to_store))))
    
    return frames[0]['session_id'], frames[0]['ccatstream_id'], combined_times,to_store[0].names, combined_data

def load_joined_timestream_from_rfsoc_g3_directory(dirname):
    """
    Extracts data from all rfsoc g3 files in a directory for a single drone (dirname) 
    and returns joined timestamp array and joined data array by concatenating those arrays
    across multiple files.

    The rfsoc-streamer rotates g3 files off after every 10 minutes of writing (configurable),
    so observations lasting longer than 10 minutes will be broken into multiple g3 files.
    This function rejoins the time and data arrays in those separated timestreams.
    """
    rfsoc_g3_files = sorted(glob.glob(os.path.join(dirname, '*.g3')))
    joined_times = None
    joined_data = None
    for f in rfsoc_g3_files:
        session_id, stream_id, times, frame_names, data = load_timestream_from_rfsoc_g3_file(f)
        if joined_times is None:
            joined_times = times.copy()
            joined_data = data.copy()
        else:
            try:
                assert times[0] > joined_times[-1]    # assure monotonically increasing
                joined_times = np.concatenate((joined_times, times))
                joined_data = np.concatenate((joined_data, data), axis=1)
            except AssertionError:
                print('Error: multiple g3 files must be joined in correct temporal order')
                return None
    return session_id, stream_id, frame_names, joined_times, joined_data

def read_frames(fname, num_frames=None):
    """
    Read a g3 file (fname) and return a list of all the frames within.
    """
    g3f = core.G3File(fname)
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

def _read_and_join_sequential_g3_frames_from_directory(dirname):
    """
    Extracts data from all hk files in a directory for a single drone (dirname) 
    and returns joined timestamp array and joined data array by concatenating those arrays
    across multiple files.
    """
    files = sorted(glob.glob(os.path.join(dirname, '*.g3')))
    frames = []
    for f in files:
        _frames = read_frames(f)
        frames.extend(_frames)
    return frames

def read_and_join_g3_frames_from_directory(dirname, return_filenames=False):
    """
    Extracts frames from all g3 files in a directory and returns a list of frames.
    """
    files = sorted(glob.glob(os.path.join(dirname, '*.g3')))
    frames = []
    for f in files:
        _frames = read_frames(f)
        frames.extend(_frames)
    if return_filenames:
        return frames, files
    else:
        return frames

def print_frames(fname, num_frames=None):
    """
    Read a g3 file (fname) and print a summary of all the frames within.
    """
    g3f = core.G3File(fname)
    if num_frames is None:
        i = 0
        try:
            while True:
                frame = g3f.next()
                print(i)
                print(frame)
                i += 1
        except StopIteration:
            pass
    else:
        try:
            for i in range(num_frames):
                frame = g3f.next()
                print(i)
                print(frame)
        except StopIteration:
            pass

def get_timestamps_of_all_g3_files(rootdir):
    """
    Extract the timestamps from all the rfsoc*_drone*.g3 files in a directory,
    and return a dictionary keyed on (stream_idx, stream_idx) pointing to combined time array
    """
    g3dirs = glob.glob(os.path.join(rootdir, "rfsoc*_drone*"))
    timestamps = {}
    for g3dir in g3dirs:
        g3files = glob.glob(os.path.join(g3dir, "*.g3"))
        for g3file in g3files:
            fname = os.path.split(g3file)[-1]
            stream_idx = fname.split('.')[0].split('_')[-1]
            stream_id, combined_times,frame_names,combined_data = load_timestream_from_rfsoc_g3_file(g3file)
            timestamps[(stream_id, stream_idx)] = combined_times
    return timestamps

def get_timestamps_of_all_drones(rootdir):
    """
    Extract the timestamps from all the rfsoc*_drone*.g3 files in a directory,
    and return a dictionary keyed on drone name pointing to combined time array
    e.g., timestamps['rfsoc03_drone1'] -> timestamp array for concatentated rfsoc03_drone1_*.g3 files
    """
    g3timestamps = get_timestamps_of_all_g3_files(rootdir)
    timestamps = {}
    drones = set([k[0] for k in g3timestamps.keys()])
    subfiles = {}
    for d in drones:
        sub = sorted( [ k[1] for k in g3timestamps.keys() if k[0]==d ] )
        subfiles[d] = sub
        for s in sub:
            if d not in timestamps:
                a = g3timestamps[(d, s)]
                timestamps[d] = a
            else:
                a0 = timestamps[d]
                a1 = g3timestamps[(d, s)]
                timestamps[d] = np.concatenate((a0, a1))
    return timestamps
                
