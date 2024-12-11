.. _installation:

Installation
============

As of December 2024, PCS is only available for
installation by source via cloning from GitHub. 
It will eventually become available for pip installation 
directly from PyPI.

Installing from Source
----------------------

To install from source, clone the respository and install with pip::

    git clone https://github.com/ccatobs/pcs.git
    cd pcs/
    pip3 install -r requirements.txt
    pip3 install .

.. note::
    If you are expecting to develop socs code you should consider using
    the `-e` flag.

.. note::
    If you would like to install for just the local user, throw the `--user`
    flag when running `setup.py`.

Getting Started
===============

Configuration Files
-------------------
To use PCS agents, one must modify the SO-OCS site
configuration file (SCF) and docker-compose.yml file.
See each agent for example blocks for that agent. See the OCS
documentation for more information about these files.
