package_name = 'pcs'
agents = {
    'LS325Agent': {'module': 'pcs.agents.lakeshore325.agent', 'entry_point': 'main'},
    'RaritanAgent': {'module': 'pcs.agents.raritan_pdu.agent', 'entry_point': 'main'},
    'ACUAgent': {'module': 'pcs.agents.acu_interface.agent', 'entry_point': 'main'},
    'Bluefors_TC_Agent': {'module': 'pcs.agents.bluefors_tc.agent', 'entry_point': 'main'},
    'AdamCPWAgent': {'module': 'pcs.agents.adam_cpw.agent', 'entry_point': 'main'},
    'DymoAgent': {'module': 'pcs.agents.dymo.agent', 'entry_point': 'main'},
    'TeledyneAgent': {'module': 'pcs.agents.teledyne.agent', 'entry_point': 'main'},
    'PfeifferAgent': {'module': 'pcs.agents.pfeiffer_singlegauge.agent', 'entry_point': 'main'},
    'Adam_Agent':{'module': 'pcs.agents.adam.agent', 'entry_point': 'main'}
}
