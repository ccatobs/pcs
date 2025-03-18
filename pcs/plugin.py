package_name = 'pcs'
agents = {
    'LS325Agent': {'module': 'pcs.agents.lakeshore325.agent', 'entry_point': 'main'},
    'ACUAgent': {'module': 'pcs.agents.acu_interface.agent', 'entry_point': 'main'},
}
