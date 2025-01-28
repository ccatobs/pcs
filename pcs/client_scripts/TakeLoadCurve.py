import time
import numpy as np
from ocs.ocs_client import OCSClient
import argparse

# Setting up arguments
parser = argparse.ArgumentParser()
parser.add_argument("filename", help="Name for the timestamp output file",
					type=str)
parser.add_argument("-t", "--time", 
                    help="The length to wait for the system to stabilize between every point (in seconds)",
                    type=int, default=1800)
args = parser.parse_args()

#Setting up bftc client

bftc = OCSClient('bluefors',args=[])
bftc.init_bftc()

still_powers = np.arange(2.5e-3, 30e-3, 2.5e-3) #powers are in watts
mxc_powers = np.arange(0, 800e-6, 100e-6)
#still_powers = [2.5e-3, 5e-3, 7e-3] #powers are in watts
#mxc_powers = [1e-6, 2e-6,3e-6]
wait_time = args.time

# Printing for the log file
print("Still powers: ")
print(still_powers)
print("MXC powers (in W): ")
print(mxc_powers)
print("Waiting time between each point: " + str(wait_time))

# Building the array to store the two powers and timestamp for each step
total_steps = still_powers.size*mxc_powers.size
output = np.zeros((total_steps,3))
#output = np.zeros((,3))
bftc.set_heater_power(heater='still', output=0.00)
bftc.set_heater_power(heater='sample', output=0.00)
bftc.heater_switch(heater='still', state='on')
bftc.heater_switch(heater='sample', state='on')

bftc.acq.start()

i = 0
for power in still_powers:
    # Set manual output
    print("Set still power to %s W" %power)
    bftc.set_heater_power(heater= 'sample', output=0.00)
    bftc.set_heater_power(heater='still',output=power)
    time.sleep(wait_time)
    
    for powur in mxc_powers:
       print("Set mxc power to %s" %powur)
       print("Starting this power at " + str(time.asctime(time.localtime())))
       bftc.set_heater_power(heater= 'sample', output=powur)
       output[i] = [power,powur,time.time()]
       i = i+1
       time.sleep(wait_time)

bftc.acq.stop()
bftc.heater_switch(heater='still', state='off')
bftc.heater_switch(heater='sample', state='off')
print("Finished running load curve sweep")
print("Finished data taking at " + str(time.asctime(time.localtime())))
print("Saving timestamp file to npy file: " + args.filename)
np.save(args.filename, output)


