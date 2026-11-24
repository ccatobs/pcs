#!/bin/python

from ocs.ocs_client import OCSClient
import time
from datetime import datetime

client = OCSClient('acu1')
print (client.tcs_broadcast(udp_host='172.17.0.1', udp_port=5601))
time.sleep(5)

#'''
print (client.broadcast.start())

print('\nMonitoring (ctrl-c to stop and exit)...\n')

#aximuth scan parameters
num_scans = 20
scan_param = {'start_time': float(datetime.utcnow().timestamp()),
              'turnaround_time': 1.0,
              'elevation': 20.0,
              'speed': 1.0,
              'num_scans': num_scans,
              'azimuth_range': [10.0, 110.0]}

#execute the scan
print (f"Moving telescope for Azimuth Scan ...")
client.az_scan(scan_params=scan_param)

stay_alive = 3900 #seconds
it = 0
while it*10 < stay_alive:
    print (client.az_scan.status())
    time.sleep(10)
    it += 1


print('Stopping data acquisition ...')
print (client.broadcast.stop())
#'''


