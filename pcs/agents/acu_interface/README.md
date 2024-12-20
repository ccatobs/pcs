# ACU Interface Agent
This agent lives within the PCS-DAQ system and communicates with OCS-TCS-ACU components of the telescope. The primary objective of the agent is to capture the 200Hz UDP position data stream from ACU and writing into PCS HK files. The agent also executes ACU commands via http requests and performs various types of scans. In addition, it has the ability to store 1Hz influx DB data stream and telescope/scan summery and status for live monitoring purposes.