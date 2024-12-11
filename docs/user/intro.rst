Introduction
============

The Prime-Cam Control System (`PCS`_) contains agents for use with the Simons 
Observatory's `OCS`_ system that are specific to controlling hardware and 
software for the Prime-Cam instrument on the CCAT Collaboration's 
Fred Young Submillimeter Telescope.
It is closely modeled off of the collection of OCS agents in the `SOCS`_` library.

In order to use PCS, SO's OCS must be set up first. 
PCS agents can be integrated into this system once it is working.

General usage of OCS is covered in the `documentation for OCS`_, including
`network configuration`_ and `creating new agents`_. Information about SOCS
can be found in `the SOCS documentation`_.

This documentation describes how to install PCS and then details the available 
agents and how to use them. 

.. _`PCS`: https://github.com/ccatp/pcs/
.. _`OCS`: https://github.com/simonsobs/ocs/
.. _`SOCS`: https://github.com/simonsobs/socs/
.. _`documentation for OCS`: https://ocs.readthedocs.io/
.. _`network configuration`: https://ocs.readthedocs.io/en/latest/user/network.html
.. _`creating new agents`: https://ocs.readthedocs.io/en/latest/developer/agents.html
.. _`the SOCS documentation`: https://socs.readthedocs.io/en/latest/index.html