.. _snmp:

=========================================
Simple Network Management Protocol (SNMP)
=========================================

SOCS supports monitoring of networked devices via the Simple Network Management
Protocol (SNMP). SNMP is a standard protocol for collecting and organizing
information about devices on the network.

SNMP support is provided through the python module `pysnmp`_ (Note that the old snmplabs.com
version of this documentation is hacked! Don't go to it, but use the lextudio.com version
linked here). pysnmp supports
twisted as an I/O framework, which integrates nicely with OCS/SOCS/PCS. PCS makes this
twisted interface for SNMP available via the SNMPTwister class, which is borrowed from SOCS
and amended to use the PCS MIB files.
The full SOCS SNMP documentation can be found `here`_. 

.. _pysnmp: https://docs.lextudio.com/pysnmp/v7.1/
.. _here: socs.readthedocs.io/en/latest/developer/interfaces/snmp.html

MIB to Python Conversion
------------------------
For developers adding a new SNMP monitoring PCS Agent, you may need to convert
a MIB file to python. This can be done with mibdump, a utility
provided by pysmi. This used to be a conversion script called
mibdump.py that had to be downloaded and used, but now it should come installed
with pysmi. Other useful (linux) packages for debugging include smitools
and snmp.

An example for converting the Raritan PDU MIB that was run from the same directory
that contained the raw MIB text file (hence the ``--mib-source=.``):

    $ mibdump --mib-source=. --mib-source=/usr/share/snmp/mibs/ --mib-source=https://mibs.pysnmp.com/asn1/@mib@ PDU2-MIB

Examples
--------
A standalone example of using ``SNMPTwister`` to interact with a device::

    from twisted.internet import reactor
    from twisted.internet.defer import inlineCallbacks
    from pcs.snmp import SNMPTwister

    # Setup communication with Raritan PDU
    snmp = SNMPTwister('10.10.10.50', 161)

    # Define OIDs to query
    num_outlets=24
    get_list = []
    for i in range(1, num_outlets+1):
        get_list.append(('PDU2-MIB', 'outletSwitchingState', 1, i))

    set_list = [('PDU2-MIB', 'switchingOperation', 1, 4)]

    @inlineCallbacks
    def query_snmp():
        x = yield snmp.get(get_list, 2)
        for i in range(len(x)):
            print(x[i][0].prettyPrint())
            print(x[i][1]._value)
        reactor.stop()

    @inlineCallbacks
    def cycle_outlet():
        # Would power cycle outlet 2 in this example
        x = yield snmp.set(set_list, 2, 2)
        print(x)
        reactor.stop()

    # Call query_snmp within the reactor
    reactor.callWhenRunning(query_snmp)
    # Call cycle_outlet
   # reactor.callWhenRunning(cycle_outlet)
    reactor.run()

Running this code would return a list of the names of the commands and the
states for all 24 outlets. The first outlet's output looks like:

    PDU2-MIB::outletSwitchingState.1.1
    7

where the 7 means that it is on - see the MIB file for all of the possibilities.

API
---

If you are developing an SNMP monitoring agent, the SNMP + twisted
interface is available for use and detailed here:

.. autoclass:: pcs.snmp.SNMPTwister
    :members:
    :noindex:
