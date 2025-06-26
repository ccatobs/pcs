.. highlight:: rst

.. _coldload_scpipsu:

========
Coldload
========

The Coldload agent controls and monitors the temperature of a coldload. The coldload therommeter is read out using a Lakeshore 240/372/etc. 
through the associated Lakeshore agent. The temperature of the coldload is controlled by varying the current supplied through a Standard Commands for Programmable Instruments (SCPI) power supply unit. 
Communication with the power supply unit can be done through direct Ethernet connection (if availabile) or can be mediated through GPIB (e.g, using a Prologix Interface).

.. argparse::
    :filename: ../pcs/agents/coldload_scpipsu/agent.py
    :func: add_agent_args
    :prog: python3 agent.py

Configuration File Examples
---------------------------

Below are configuration examples for the SO-OCS site config file 
and docker-compose file for running the
Agent in a docker container.

OCS Site Config
```````````````

To run the Coldload agent, a RaritanAgent block must be added
to the site config file. Here is an example configuration block with
all available arguments::

      {'agent-class': 'ColdloadAgent_ScpiPsu',
       'instance-id': 'power-psu-coldload',
       'manage': 'docker',
       'arguments': [
         ['--ip-address', '10.10.10.50'],
         ['--gpib-slot', '15'],
         ['--psu-channel', 1],
         ['--lakeshore', ['cryo-ls240-lsa291f', 'Channel_4']],
         ['--max-current', 0.6]
         ]},

The ``--ip-address`` argument should be changed to the IP address of the BK Precision power supply on the network.
The ``--gpib-slot`` argument should be changed to the GPIB port if using a Prologix Interface for communication.
The ``--port`` argument should be added if communicating through Ethernet directly.
The ``--psu-channel`` argument should be changed to the power supply channel connected to the coldload.
The ``--lakeshore`` argument should be a list with two elements: The instance-id of the Lakeshore agent and the coldload thermometer channel.
The ``-max-current`` argument specifies the maximum current that will be supplied by the power supply. The default is shown above.

Docker Compose
``````````````
The Coldload agent should be configured to run in a Docker container. An
example docker compose service configuration is shown here::

  ocs-coldload: 
    image: ghcr.io/ccatobs/pcs:latest
    <<: *log-options
    hostname: ocs-docker
    network_mode: "host"
    environment:
      - INSTANCE_ID=power-psu-coldload
      - SITE_HUB=ws://192.168.24.55:8001/ws
      - SITE_HTTP=http://192.168.24.55:8001/call
    volumes:
      - ${OCS_CONFIG_DIR}:/config:ro

Description
-----------

A "coldload" is an approximate blackbody that is used cryogenically as a calibration source. The coldload used for Mod-Cam is an aluminum plate coated with epoxy.
The temperature of the coldload is controlled by varying the current supplied by a BK Precision 9130B power supply through resistors mounted on the coldload. The
temperature is read out using a Lakeshore LS240. The Coldload agent subclasses the Simon's Observatory SOCS `ScpiPsu agent <https://domain.invalid/>`_  for control over the power supply
but limits control to only the power supply channel connected to the coldload. Additionally, the Coldload agent subscribes to the Lakeshore agent feed monitoring the coldload temperature.
The Coldload agent's main functionality is controlling the power supply (turning the channel on/off and getting/setting the voltage/current), but subscribing to the temperature feed also allows
getting the temperature of the coldload (get_temp()) and setting the temperature of the coldload using a PID controller (set_temp()). When setting the temperature of the coldload, the PID values 
(error, integral of error, and derivative of error) as well as the current are continously published to the 'pid_output' OCS feed for monitoring. Finally, the Coldload agent exposes the serial read(), write()
commands to allow for greater control over the power supply unit.

Example Clients
---------------

Below is an example client to control outlets::

    from ocs.ocs_client import OCSClient
    client = OCSClient('power-psu-coldload')

    # Get channel output 
    client.get_output()

    # Set channel output
    client.set_output(state='on')
    client.set_output(state='off')

    # Get/set voltage
    client.get_voltage()
    client.set_voltage(volts=15) # Volts

    # Get/set current
    client.get_current()
    client.set_current(current=0.1) # Amps

    # Get/set temperature
    client.get_temp()
    client.set_temp(temp=65, max_current=1, pid=[2.25e-3, 5.1e-7, 0.71]) # Kelvin

    # Read/write serial commands
    client.write(msg='*idn?')
    client.read()

Agent API
---------

.. autoclass:: pcs.agents.coldload_scpipsu.agent.ColdloadAgent_ScpiPsu
    :members: