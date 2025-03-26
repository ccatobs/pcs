PCS - Prime-Cam Control System
==============================

Overview
--------

The Prime-Cam Control System (`PCS`_) contains agents for use with the Simons Observatory's `OCS`_ system that are specific to controlling hardware and software for the Prime-Cam instrument on the CCAT Collaboration's Fred Young Submillimeter Telescope.

.. _`PCS`: https://github.com/ccatp/pcs/
.. _`OCS`: https://github.com/simonsobs/ocs/

Installation
------------
Currently must install from GitHub directly via ``git clone``. 
Once the repo is cloned, you can pip install it from within the top level of the repo (e.g. ``pip install .``). This will enable PCS agents to be discoverable by SO's OCS for client scripts.

Docker Images
-------------
Coming soon!

Documentation
-------------
The documentation for individual PCS agents can be found at `this ReadtheDocs page`_.

.. _this ReadtheDocs page: https://pcs.readthedocs.io/en/latest/

Tests
-----
Tests have not yet been implemented for PCS, though it is a long-term goal to include them.

Contribution Guidelines
-----------------------
Anyone developing agents for Prime-Cam related hardware is welcome to contribute new agents! Please make a new
branch, develop and test your agent with its associated hardware on it, then submit a pull request for moderator
review to merge it into the main branch of PCS.

License
-------
This project is licensed under the BSD 2-Clause License - see the `LICENSE.txt`_ file for details.

.. _LICENSE.txt: https://github.com/ccatp/pcs/blob/main/LICENSE.txt