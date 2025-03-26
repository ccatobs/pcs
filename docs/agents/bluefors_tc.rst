.. highlight:: rst

.. _bluefors_tc:

===============================
Bluefors Temperature Controller
===============================

The Bluefors Temperature Controller agent monitors and controls
the Bluefors Temperature Controller device for temperature readout
and DR control in newer Bluefors dilution refrigerators. 
The monitoring is done via HTTP via the requests package.

.. argparse::
    :filename: ../pcs/agents/bluefors_tc/agent.py
    :func: make_parser
    :prog: python3 agent.py

Dependencies
------------
.. todo::
    Any dependencies or special set up?

Configuration File Examples
---------------------------

Below are configuration examples for the ocs config file and for running the
Agent in a docker container.

OCS Site Config
````````````````
.. todo::
    Add example config file entry 

Docker Compose
``````````````
.. todo::
    Add example docker compose file entry

Direct Communication with Driver Code
`````````````````````````````````````
.. todo::
    Describe how to use driver code to communicate directly over serial

Agent API
---------

.. autoclass:: pcs.agents.bluefors_tc.agent.Bluefors_TC_Agent
    :members:

.. todo::
    Can also add driver API eventually