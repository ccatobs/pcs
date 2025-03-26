.. highlight:: rst

.. _raritan_pdu:

===========
Raritan PDU
===========

The Raritan PDU agent monitors and controls
the Raritan PX4-53A8C-C8E7A0 24-outlet, vertically-mounted power distribution
units (PDUs) used in Prime-Cam electronics racks. The monitoring is done via SNMP over an 
ethernet connection.

.. argparse::
    :filename: ../pcs/agents/raritan_pdu/agent.py
    :func: make_parser
    :prog: python3 agent.py

Configuration File Examples
---------------------------

Below are configuration examples for the SO-OCS site config file 
and docker-compose file for running the
Agent in a docker container.

OCS Site Config
```````````````

To run the Raritan PDU agent, a RaritanAgent block must be added
to the site config file. Here is an example configuration block with
all available arguments::

      {'agent-class': 'RaritanAgent',
       'instance-id': 'power-pdu-1',
       'manage': 'docker',
       'arguments': [
         ['--ip-address', '10.10.10.50'],
         ['--port', 161],
         ['--snmp-version', 2],
         ['--mode', 'acq'],
         ['--sample-period', 60],
         ['--lock-outlet', [1,2]]]},

The ``--ip-address`` argument should be changed to the IP address of the Raritan PDU on the network.
It is the only required argument. The port, snmp version, mode, and sample period are all shown with
default parameters and do not need to be included if they match the values here.
The lock outlet option takes a list of the outlets to lock upon startup, numbered 1-24 to match
the numbers on the PDU itself. Outlets do not need to be locked and can be locked by a client script
later, so this is not a required option for the config file.

Docker Compose
``````````````
The Raritan PDU Agent should be configured to run in a Docker container. An
example docker compose service configuration is shown here::

  ocs-power-pdu-1:
    image: ghcr.io/ccatobs/pcs:latest
    hostname: ocs-docker
    network_mode: "host"
    environment:
      - INSTANCE_ID=power-pdu-1
      - SITE_HUB=ws://127.0.0.1:8001/ws
      - SITE_HTTP=http://127.0.0.1:8001/call
      - LOGLEVEL=info
    volumes:
      - ${OCS_CONFIG_DIR}:/config

The ``LOGLEVEL`` environment variable can be used to set the log level for
debugging. The default level is "info".

Description
-----------

These 24-outlet Raritan PDUs will be used on the Prime-Cam-specific electronics racks
to deliver power to other components. The Raritan PDU agent monitors the state (on/off)
of each outlet as well as the active power, current, voltage, and AC freqency
for each outlet. It can also set the state of the outlet, cycle an outlet, and lock 
outlets to prevent changes to their state without an additional step.

The agent is set up to use v2 of the Raritan PDU Simple Network
Management Protocol (SNMP) interface. It issues SNMP GET or SET commands to request the
status from several Object Identifiers (OIDs) specified by the
Management Information Base (MIB) file provided at pcs/pcs/mibs/PDU2-MIB.py.
We sample only a subset of the OIDs defined
by the MIB. The MIB has been converted from the original .mib format to a .py
format that is consumable via pysnmp. 

As of March 2025, the Prime-Cam PDUs were running firmware version 4.2.10.5-50400.
Support documents for these PDUs (like the quick start guide, user guide, and MIB guide)
can be found `here`_ for
this version and `on this page`_ for all versions.
The original MIB text file is also available through this Raritan support document page.

.. _here: https://www.raritan.com/support/product/pdu-g4/px4-version-4.2.10
.. _on this page: https://www.raritan.com/support/product/pdu-g4

Before the agent can be used, the PDU must be configured to allow SNMP. This was configured by 
sshing into the PDU and running ``network services snmp v1/v2c enable`` from the config menu 
of their command line interface. We also had to set the write community string, which was not
initially configured, with ``network services snmp writeCommunity private`` to run SET commands.

Agent Fields
````````````

The fields returned by the Agent are built from the SNMP GET responses from the
Raritan PDU. The field names consist of the OID name and the last value or two
of the OID. The first of these last two values is the PDU number (always 1 unless
the PDUs are linked together) and the second is usually the outlet number.

For example, the raw MIB OID to get the outlet state for outlet 4 looks like
``PDU2-MIB::outletSwitchingState.1.4``, which is mapped to ``outletSwitchingState_1_04``
for the PCS field. The MIB OID for the the values that request particular sensor
measurements are even more complicated, e.g. ``PDU2-MIB::measurementsOutletSensorValue.1.4.rmsVoltage``.
These get mapped to fields that look like ``measurementsOutletSensorValue_1_04_rmsVoltage``.

The queries used in this agent return integers which map to some state for the outlet state
or to a measurement for the power, voltage, current, and frequency. These integers
get decoded into their corresponding string representations and stored in the
OCS Agent Process' session.data object. For more details on this structure, see
the Agent API below. For information about the states corresponding to these
values, refer to the MIB file.

One additional detail about the measurements - each measurement is returned as an integer, 
but another field may be queried to know if the decimal place for a given integer should be moved.
For example, the OID ``PDU2-MIB::measurementsOutletSensorValue.1.24.rmsCurrent`` might return the
value 532, but the OID ``PDU2-MIB::outletSensorDecimalDigits.1.24.rmsCurrent`` might return the value
3, which means that the true current is 532/10^3 = 0.532 A. This conversion is handled automatically
by the agent. The converted float in this example would be saved to the OCS field 
``measurementsOutletSensorValue_1_24_rmsCurrent``, but the raw value is also saved as a string in the
``measurementsOutletSensorValue_1_24_rmsCurrent_description`` field.

Example Clients
---------------

Below is an example client to control outlets::

    from ocs.ocs_client import OCSClient
    client = OCSClient('power-pdu-1')

    # Turn outlet 1 on/off
    client.set_outlet(outlet=1, state='off')
    client.set_outlet(outlet=1, state='on')

    # Cycle outlet for 10 seconds
    client.cycle_outlet(outlet=1, cycle_time=10)

    # Lock outlet 5
    client.lock_outlet(outlet=5, lock=True)
    # Unlock outlet 24
    client.lock_outlet(outlet=24, lock=False)

Agent API
---------

.. autoclass:: pcs.agents.raritan_pdu.agent.RaritanAgent
    :members:

Supporting APIs
---------------

.. autoclass:: pcs.agents.raritan_pdu.agent.update_cache
    :members:
    :noindex: