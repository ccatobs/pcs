.. pcs documentation master file, created by
   sphinx-quickstart on Wed Dec 11 00:13:35 2024.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Prime-Cam Control System
=================================

The Prime-Cam Control System (PCS) repository contains code for operating
hardware specific to Prime-Cam with the CCAT Observatory. It makes use of the
Observatory Control System (OCS) framework developed by the Simons Observatory.
For information about general OCS components, see the `OCS Documentation
<https://ocs.readthedocs.io/en/latest/?badge=latest>`_.

Contents
========

===============================  ============================================================
Section                          Description
===============================  ============================================================
:ref:`User Guide <user>`           Start here for information about the design and use of PCS.
:ref:`Agent Reference <agents>`    Details on configuration and use of the OCS Agents
                                   provided by PCS.
===============================  ============================================================

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   user/intro
   user/installation

.. toctree::
   :maxdepth: 2
   :caption: Agent Reference

   agents/bftc_agent
   agents/lakeshore325


