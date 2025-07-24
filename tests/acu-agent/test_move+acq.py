#!/bin/python
'''
This script runs the test to command telescope to move to
a preset point. In addition it also turns on the udp stream capture
and writing into g3 files while the movement operation in place.

Test runs for 5 minute and exists by stopping the broadcast.
'''

from ocs.ocs_client import OCSClient
import time

client = OCSClient('acu1')
print (client.broadcast.start())

print('\nMonitoring (ctrl-c to stop and exit)...\n')

az_pos = 100.0
el_pos = 80.0

mv_cmd = False

try:
    for i in range(50):
        time.sleep(6)
        print (client.broadcast.status())
        r = client.broadcast.status()
        print (r.session.get('data'))
        if not(mv_cmd):
            print (f"Moving telescope to Az: {az_pos}, El: {el_pos}")
            client.go_to(az=az_pos, el=el_pos)
            mv_cmd = True
        print (client.go_to.status())
except KeyboardInterrupt:
    print('Exiting on ctrl-c...')
    print ()

print('Stopping data acquisition ...')
print (client.broadcast.stop())
