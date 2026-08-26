pcs-web
=======

pcs-web is a broswer-based GUI for remote control of OCS/PCS agents
that uses the `ocs-web`_ interface designed for Simons Observatory as a 
base and is adapted for CCAT-specific requirements.

If an agent does not have a specialized agent panel designed for its agent 
class, pcs-web will use the Generic Agent panel.  This panel displays
address and connection status on the left side and any registered tasks
and processes for the agent on the right side.  Operations that do not 
require parameters or have default parameters can be run, but attempting to
run operations that do require parameters to be entered will lead to an
error.  Here is an example of a Generic Agent panel:

.. _`ocs-web`: https://ocs.readthedocs.io/en/main/user/ocs_web.html