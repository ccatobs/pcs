.. highlight:: rst

.. _lakeshore325:

=============
Lakeshore 325
=============

.. todo::
    Describe the Lakeshore 325

.. argparse::
    :filename: ../pcs/agents/lakeshore325/agent.py
    :func: make_parser
    :prog: python3 agent.py

Dependencies
------------
.. todo::
    Any dependencies?

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

.. autoclass:: pcs.agents.lakeshore325.agent.LS325_Agent
    :members:

.. todo::
    Can also add driver API eventually