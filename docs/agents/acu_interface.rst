.. highlight:: rst

.. _acu_interface:

====================================
PrimeCam-ACU Controller Interface
====================================

ACUE-interface agent lives within the PCS-DAQ system and communicates with OCS-TCS-ACU 
components of the telescope. The primary objective of the agent is to capture the 200Hz 
UDP position data stream from ACU and writing into PCS HK files. The agent also executes 
ACU commands via http requests and performs various types of scans. In addition, it has 
the ability to store 1Hz influx DB data stream and telescope/scan summery and status for 
live monitoring purposes.

.. argparse::
    :filename: ../pcs/agents/acu_interface/agent.py
    :func: make_parser
    :prog: python3 agent.py


Dependencies
------------
.. todo:: Add installation of soaculib library and any additional dependencies


Configuration File Examples
---------------------------

Below are configuration examples for the ocs config file and for running the
Agent in a docker container.

OCS Site Config
```````````````
To configure the ACU Agent we need to add a block to the ocs configuration
file. An example configuration block using all availabile arguments is below::

 {'agent-class': 'ACUAgent',
       'instance-id': 'acu',
       'arguments': [['--acu-config', 'acu_config.yaml'],
                        ['--device', 'acu-sim']]}

Docker Compose
``````````````

We also require a configuration in the docker compose file for the agent, 
an example configuration of the agent is shown below::

  ocs-acu:
    image: "acu-interface-agent"
    build: /path/to/agent/dockerfile/directory/
    hostname: ccat-docker
    environment:
      - INSTANCE_ID=acu
      - LOGLEVEL=info
      - SITE_HUB=ws://127.0.0.1:8001/ws
      - SITE_HTTP=http://127.0.0.1:8001/call
    volumes:
      - ${OCS_CONFIG_DIR}:/config:ro
      - /path/to/workspace/with/acu-config/file/:/work:ro
      - /path/to/certificate/directory/:/tls:ro
    network_mode: "host"

ACU Configuration
`````````````````
Additionally, we need a configuration file for the ACU interface settings.
An example block configuration is shown below::

 devices:
    # ACU simulator
    'acu-sim':
      #Address of the "remote" interface
      'base_url': 'https://127.0.0.1:5600'
      #Local interface IP addr
      'interface_ip': "172.17.0.1"

      # Sleep time to wait for motion to end.
      'motion_waittime': 1.0
      # List of streams to configure.
      'streams':
        'main':
          'acu_name': 'PositionBroadcast'
          'port': 5601
          'schema': 'v0'
      # Certificates
      'certs':
        'server_cert': '/tls/server.cert.pem'
        'client_cert': '/tls/client.cert.pem'
        'client_key': '/tls/client.key.pem'
        'verify': False
 stream_schemas:
  v0:
    format: '<Lddddddddddd'
    fields: ['Day', 'Time', 'Azimuth', 'Elevation', 'AzEncoder', 
            'ElEncoder', 'AzCurrent1', 'AzCurrent2', 'AzCurrent3', 
            'AzCurrent4', 'ElCurrent1', 'ElCurrent2']

 datasets:
  ccat:
    'default_dataset': 'ccat'
    'datasets':
      - ['ccat',       'DataSets.StatusCCatDetailed8100']
      - ['general',    'DataSets.StatusGeneral8100']
      - ['extra',      'DataSets.StatusExtra8100']
      - ['third',      'DataSets.Status3rdAxis']
      - ['faults',     'DataSets.StatusDetailedFaults']
      - ['pointing',   'DataSets.CmdPointingCorrection']
      - ['spem',       'DataSets.CmdSPEMParameter']
      - ['weather',    'DataSets.CmdWeatherStation']
      - ['azimuth',    'Antenna.SkyAxes.Azimuth']
      - ['elevation',  'Antenna.SkyAxes.Elevation']


Description
-----------

Antenna Control Unit (ACU) is a specialized computer responsible for moving the telescope platform and capturing the readout of encoder measurements. This system lives within the centrally managed Telescope Control System (TCS). Within the PrimeCam DAQ framework, an interface agent commuincates with the ACU to execute telescope movement for observation scans and writes the position data stream into G3 files in the PCS HK database.


Agent API
---------

# Autoclass the Agent from docstrings

.. autoclass:: pcs.agents.acu_interface.agent.ACUAgent
   :members:

Agent Setup Guide
-----------------

.. todo:: Add installation and setup guide on how to launch the TCS-ACU system and run example scan and readout capture.
