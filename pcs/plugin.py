package_name = 'pcs'
agents = {
    'LS325Agent': {'module': 'pcs.agents.lakeshore325.agent', 'entry_point': 'main'},
    'RaritanAgent': {'module': 'pcs.agents.raritan_pdu.agent', 'entry_point': 'main'},
    'ACUAgent': {'module': 'pcs.agents.acu_interface.agent', 'entry_point': 'main'},
    'Bluefors_TC_Agent': {'module': 'pcs.agents.bluefors_tc.agent', 'entry_point': 'main'},
    'ColdloadAgent_ScpiPsu': {'module': 'pcs.agents.coldload_scpipsu.agent', 'entry_point': 'main'},
    'PrimecamBiasAgent': {'module': 'pcs.agents.primecam_bias.agent', 'entry_point': 'main'},
    'BeamMapperAgent': {'module': 'pcs.agents.beam_mapper.agent', 'entry_point': 'main'}
}
