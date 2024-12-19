#!/bin/python

import os, socket, yaml
import time
import socket, struct, requests

#: Global variable to hold the most-recent config block from calling

cache = None

def load_config(filename=None, update_cache=True):
    '''Load ACU configuration file and return the settings content
    as dict.
    Args:
        filename (str): Path of the config filename.
        update_cache (bool): By default, it wil update the internal cache.

    TO DO: Cache configuration not set, so need to assign config
    file every time agent is initiated.

    If no config file provided, raise exception.
    '''
    global cache
    if filename is None:
        raise RuntimeError("Config file not found;")
    else:
        config = yaml.safe_load(open(filename, 'r').read())
    
    #annotate
    for k,v in config.get('devices', {}).items():
        v['_name'] = k
        v['_filename'] = filename

    if update_cache:
        cache = config
    return config

def get_stream_schema(version):
    '''
    Args:
        version (str): Name of the schema version defined in the config.yaml

    Returns the ACU UDP data stream structure.
    '''
    return cache['stream_schemas'][version]

def get_datagram(raw_data, decoded_data,
                 fmt, fmt_len):
    now = time.time()
    offset = 0
    while len(raw_data)-offset >= fmt_len:
        d = struct.unpack(fmt, raw_data[offset:offset+fmt_len])
        decoded_data.append((now, d))
        offset += fmt_len
    return decoded_data


class observatory_control_system:
    '''Base class for interfacing with the ACU commands within TCS
    via http requests.

    Parameters:
        url (str): base URL address of the telescope
        log (obj): a logger object for storing the log info
        server_cert (str): location of the server certificate file
        client_cert (str): location of the client certificate file
        client_key (str): location of the client key file
        tcs_direct (bool): If the commands are directly appended to
            the base telescope url. Default is False.
        verify_cert (bool): Whether to set up TLS verification,
            default is True.
    '''
    def __init__(self, url, log, server_cert="", client_cert="", 
                 client_key="", tcs_direct=False, verify_cert=True):
        self.url = url
        self.server_cert = server_cert
        self.client_cert = client_cert
        self.client_key = client_key

        self.log = log
        self.verify_cert = verify_cert

        self.log.info("Setting up TCS connection")

        self.start_session()
        self.log.info("TCS commanding session started")

        if tcs_direct:
            self.url_prefix = ""
        else:
            self.url_prefix = "/api/v1/telescope"

    def start_session(self):
        if self.server_cert == "" or self.client_cert == "" \
            or self.client_key == "":
            self.session = requests.Session()
            self.session.verify = False
            return
        # check certs are present
        for cert in [self.server_cert, self.client_cert, self.client_key]:
            if not os.path.exists(cert):
                self.log.error(f"cert {cert} not found, exiting")
                sys.exit(-1)
        self.session = requests.Session()
        self.session.verify = self.server_cert
        self.session.cert = (self.client_cert, self.client_key)

    def post(self, cmd, data):
        if data == "":
            self.log.info(f"sending {self.url}{cmd}")
        else:
            self.log.info(f"sending {data} to {self.url}{cmd}")

        try:
            response = self.session.post(
                    f"{self.url}{cmd}", json=data, verify=self.verify_cert #allow_redirects=True
                    )
            self.log.debug(f"response code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            raise SystemExit(e)
        self.log.debug(f"{response.text}")
        if response.status_code == 503:
            self.log.warning(response.json().get("message", ""))
            return {}
        if response.json().get("status", "") == "error":
            self.log.error(response.json().get("message", ""))

        return response

    def get_status(self):
        cmd = f"{self.url_prefix}/acu/status"
        self.log.info(f"getting status from {self.url}{cmd}")
        try:
            self.status = self.session.get(self.url + cmd, verify=self.verify_cert).json()
        except requests.exceptions.ConnectionError as e:
            self.log.error(
                    f"failed to connect on {self.url} check is server up, exiting"
                    )
            sys.exit(-1)
        return self.status

    def abort(self):
        cmd = f"{self.url_prefix}/abort"
        r = self.post(cmd, "")
        response = json.loads(r.content.decode())
        self.log.info(response)
        return response


    def move_to(self, azimuth: float, elevation: float):
        """send telescope to a given azimuth,elevation
            :param azimuth
            :param elevation
        """
        #self.abort()
        #time.sleep(2)
        cmd = f"{self.url_prefix}/move-to"
        data = {"azimuth": azimuth, "elevation": elevation}
        response = self.post(cmd, data)
        self.log.info(response.json())
        return response

    def azimuth_scan(self, start_time: float, elevation: float,
                     azimuth_range: list, num_scans: int,
                     turnaround_time: float, speed: float):
        """send telescope on an azimuth scan at a constant elevation
        :start_time: time in future to begin scan, in format %Y-%m-%dT%H:%M:%SZ
        :param elevation: elevation of telescope in deg
        :param azimuth_range: list with 2 floats containing range of azimuth
        :param num_scan: numbers of cycles of the azimuth scan
        :turnaround_time: time to change scan direction in seconds
        :speed: speed of scan in degrees/second
        """
        # azimuth scan example
        cmd = f"{self.url_prefix}/azimuth-scan"
        data = {
                "azimuth_range": azimuth_range,
                "elevation": elevation,
                "num_scans": num_scans,
                "start_time": start_time,
                "turnaround_time": turnaround_time,
                "speed": speed,
                }
        response = self.post(cmd, data)
        return response

    def scan_pattern(self, data):
        dt = datetime.datetime.now() + datetime.timedelta(seconds=10)
        cmd = f"{self.url_prefix}/path"
        self.log.info(data)
        response = self.post(cmd, data)
        return response

    def scan_pattern_from_file(self, file_path):
        # Initialize an empty list to hold the rows
        points = []
        # Open the file and read line by line
        with open(file_path, 'r') as file:
            for line in file:
                # Split each line by spaces and convert to float
                row = [float(value) for value in line.split()]
                # Append the row to the master list
                points.append(row)
        # Now data_rows contains each row of the file as a list
        start_dt = datetime.datetime.now() + datetime.timedelta(seconds=10)
        data = {
                "start_time": float(f"{time.mktime(start_dt.timetuple()):3.5f}"),
                "coordsys": "Horizon",
                "points": points
                }
        self.scan_pattern(data)

        return













