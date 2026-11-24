#!/bin/python

import os, yaml, json, sys
import time
import socket, struct, requests

#: Global variable to hold the most-recent config block from calling

cache = None

#: Per-request HTTP timeout (s) for TCS calls, (connect, read). Without it a hung Go
#: TCS blocks the call indefinitely: on the reactor thread that freezes every Process,
#: the 200 Hz broadcast included; in a worker thread it holds the task, and a typed
#: scan's azel_lock. The read budget is generous so a slow TCS is not cut off.
TCS_HTTP_TIMEOUT = (5, 30)  # (connect, read) seconds

#: The Go TCS refuses a program-track start under 9.8 s out
#: (checkProgramTrackStartTime at TCS b7bbbf9, its pad on the ACU Extension
#: ICD's 5 s minimum); 10 s clears it.
SCAN_DISPATCH_BUFFER_SEC: float = 10.0


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
    try:
        return cache['stream_schemas'][version]
    except KeyError:
        raise KeyError(f"stream schema {version!r} is not in stream_schemas of the config "
                       f"(named by streams.main.schema of the device)") from None

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
                 client_key="", tcs_direct=False, verify_cert=True,
                 readonly_url=None):
        self.url = url
        self.readonly_url = readonly_url
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

        # Separate unauthenticated session for direct ACU hardware queries.
        self.readonly_session = requests.Session()
        self.readonly_session.verify = False

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
        if not data:
            self.log.info(f"sending {self.url}{cmd}")
        else:
            self.log.info(f"sending {data} to {self.url}{cmd}")

        try:
            response = self.session.post(
                    f"{self.url}{cmd}", json=data, verify=self.verify_cert, #allow_redirects=True
                    timeout=TCS_HTTP_TIMEOUT
                    )
            self.log.debug(f"response code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            raise SystemExit(e)
        self.log.debug(f"{response.text}")
        if response.status_code == 503:
            try:
                self.log.warning(response.json().get("message", ""))
            except Exception:
                pass
            return {}
        try:
            if response.json().get("status", "") == "error":
                self.log.error(response.json().get("message", ""))
        except Exception:
            pass

        return response

    def get_status(self):
        cmd = f"{self.url_prefix}/status"
        # temporarily (?) disabling since this spams the log
        # self.log.info(f"getting status from {self.url}{cmd}")
        try:
            r = self.session.get(
                    self.url + cmd, verify=self.verify_cert, timeout=TCS_HTTP_TIMEOUT
                    )
        except requests.exceptions.ConnectionError as e:
            self.log.error(
                    f"failed to connect on {self.url} check is server up, exiting"
                    )
            sys.exit(-1)
        if r.status_code != 200:
            raise RuntimeError(f"get_status: HTTP {r.status_code}")
        self.status = r.json()
        return self.status

    
    def abort(self):
        cmd = f"{self.url_prefix}/abort"
        # No body: a Go TCS reached directly refuses an /abort with one, even ""
        # (checkNoBody at TCS b7bbbf9); the OCS API forwards none.
        r = self.post(cmd, None)
        if not hasattr(r, "content"):
            raise RuntimeError(f"{cmd}: HTTP 503, no answer from the TCS")
        response = json.loads(r.content.decode())
        self.log.info(response)
        return response

    # def stop(self):
    #     cmd = f"{self.url_prefix}/abort" #is the abort command right?
    #     r = self.post(cmd, "")
    #     response = json.loads(r.content.decode())
    #     self.log.info(response)
    #     #post command to change az and el modes to stop
    #     return response

    def clear(self):
        # clears the ProgramTrack stack and sets Az and El modes to "Stop"
        cmd = f"{self.url_prefix}/clear-track"
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
        # ``post`` returns {} (not a Response) on HTTP 503, and the OCS API's own
        # HTTP 500 body is plain text, not JSON: log either without raising.
        if hasattr(response, "json"):
            try:
                self.log.info(response.json())
            except ValueError:
                self.log.info(response.text)
        return response

    def azimuth_scan(self, start_time: float, elevation: float,
                     azimuth_range: list, num_scans: int,
                     turnaround_time: float, speed: float):
        """send telescope on an azimuth scan at a constant elevation
        :start_time: when to begin, as Unix seconds, or seconds from now below 1e5
            (the agent's az_scan converts an ISO-8601 string). Go TCS b5666ae and later
            first move the dish to (azimuth_range[0], elevation): 0 starts the scan about
            10 s after that move (earlier builds refuse it), and any other start must leave
            the move time plus 5 s; a 200 means queued, so a start reached too late fails
            after this returns.
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
        # The fraction of a second is kept: a start truncated to the second can
        # be under 9.8 s out.
        data = {
                "start_time": time.time() + SCAN_DISPATCH_BUFFER_SEC,
                "coordsys": "Horizon",
                "points": points
                }
        # Return scan_pattern's value (Response on success, {} on a 503
        # short-circuit), as every other command method does, so the caller can
        # read the status via tcs_response_status.
        return self.scan_pattern(data)

    # def wait(self, cmd): ''' make generic for all commands ? '''
    #     # Nolta manual post command to STOP
    #     response = self.post(cmd, data)
    #     if response:
    #         return response
    #     else :
    #         time.sleep(0.1)
    #         # wait to read response from STOP command before continue
        












