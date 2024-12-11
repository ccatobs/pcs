.. highlight:: rst

.. _bluefors_tc:

=============
Bluefors Temperature Controller
=============

.. todo::
    Describe the Bluefors Temperature Controller

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