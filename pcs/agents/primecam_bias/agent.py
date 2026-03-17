import time
from ocs import ocs_agent, site_config
import redis
import json
import argparse
import yaml

class LNAagent:
    def __init__(self, agent):
        self.agent = agent
        self.client = redis.Redis(host='192.168.2.80', port=6379)
        self.psub = self.client.pubsub()
        self.psub.subscribe('sparkreply')
        self.psub.get_message()
    def send_command(self, command_arg, channel, add_arg = False):
        print('debug: entered send_command with:', channel)
        if type(channel) == list and type(channel[0]) == list:
            responses = []
            for ch in channel:
                response = send_command(command_arg, ch, add_arg = add_arg)
                responses.append(response)
            return responses

        print("Debug Channel:", channel, type(channel))
        card, chan = channel[0],channel[-1] 
        if add_arg:
            key = add_arg['key']
            value = add_arg['value']
            command = {"command": command_arg, "args": {"card": int(card), "channel": int(chan), key: value}}
            print(command)
        else:
            command = {"command": command_arg, "args": {"card": int(card), "channel": int(chan)}}
            print('debug:', command)
        publish = self.client.publish('sparkommand', json.dumps(command))
        print('debug: pass client', publish)
        try:
            message = self.psub.get_message(True, timeout=5)
            print('debug message:', message)
            response = json.loads(message['data'].decode())
            print('debug response:', response)
        except:
            print("Failed")
            return
        return response

    @ocs_agent.param('channel')
    def get_status(self, session, params):
        """
        Gets the channel status.
        """
        print("debug: entered get_status")
        channel = params['channel']
        #session.set_status('running')
        #channel = params['channel']
        card, chan = channel[0], channel[1]

        try:
            status = self.send_command('getStatus', channel)
        except Exception as e:
            #session.set_status('stopping')
            return False, f"Failed to get status for channel {channel}: {e}"

        #session.data = {'timestamp': time.time(),
            #'card': card,
            #'channel': chan,
            #'status': self.send_command('get_status', channel)}

        #session.set_status('done')
        return True, {'status': status}

        #card, chan = channel[0],channel[-1]
        #command_arg = "getStatus"
        #return send_command(command_arg, channel)

    @ocs_agent.param('channel')
    def turn_on_channel(self, session, params):
        """
        Turns on a given channel.
        """
        #session.set_status('running')
        #channel = params['channel']
        channel = params['channel']
        card, chan = channel[0],channel[-1]
        try:
            status = self.send_command('enableOutput', channel)
        except Exception as e:
            #session.set_status('stopping')
            return False, f"Failed to get status for channel {channel}: {e}"

        #session.data = {'timestamp': time.time(),
            #'card': card,
            #'channel': chan,
            #'status': self.send_command('enableOutput', channel)}

        #session.set_status('done')

        return True, {'status': status}

    @ocs_agent.param('channel')
    def turn_off_channel(self, session, params):
        """
        Turns off a given channel.
        """
        channel = params['channel']
        card, chan = channel[0],channel[-1]
        try:
            status = self.send_command('disableOutput', channel)
        except Exception as e:
            #session.set_status('stopping')
            return False, f"Failed to get status for channel {channel}: {e}"

        #session.data = {'timestamp': time.time(),
            #'card': card,
            #'channel': chan,
            #'status': self.send_command('disableOutput', channel)}

        #session.set_status('done')
        return True, {'status': status}
    
    @ocs_agent.param('current')
    @ocs_agent.param('channel')
    def set_current(self, session, params):
        """
        Sets the current for a given channel.
        """
        #session.set_status('running')
        #channel = params['channel']
        current = params['current']
        channel = params['channel']
        card, chan = channel[0],channel[-1]
        try:
            status = self.send_command('seekCurrent', channel, add_arg = {"key": "current", "value":float(current)})
        except Exception as e:
            #session.set_status('stopping')
            return False, f"Failed to get status for channel {channel}: {e}"

        #session.data = {'timestamp': time.time(),
            #'card': card,
            #'channel': chan,
            #'status': self.send_command('seekCurrent', channel, add_arg = {"key": "current", "value":float(current)})}

        #session.set_status('done')
        return True, {'status': status}

    @ocs_agent.param('voltage')
    @ocs_agent.param('channel')
    def set_voltage(self, session, params):
        """
        Sets voltage for a given channel.
        """

        voltage = params['voltage']
        channel = params['channel']
        card, chan = channel[0],channel[-1]
        try:
            status = self.send_command('seekVoltage', channel, add_arg = {"key": "voltage", "value":float(voltage)})
        except Exception as e:
            #session.set_status('stopping')
            return False, f"Failed to get status for channel {channel}: {e}"

        #session.data = {'timestamp': time.time(),
            #'card': card,
            #'channel': chan,
            #'status': self.send_command('seekVoltage', channel, {"key": "voltage", "value":float(voltage)})}

        #session.set_status('done')

        return True, {'status': status}

    # #@ocs_agent.param('bias_file')
    # def load_channel_map(self, bias_file="bias_map.yaml"):
    # 	"""
    # 	Loads the mapping between drone IDs and bias channels
    # 	"""
    # 	#session.set_status('running')
    # 	#bias_file = params['bias_file']

    # 	with open(bias_file, "r") as file:
    # 		for f in yaml.safe_load_all(file):
    # 			channel_map = f

    # 	#session.set_status('done')
    # 	return channel_map

    @ocs_agent.param('channel')
    def summarize_status(self, session, params):
        #session.set_status('running')
                #channel = params['channel']

        channel = params['channel']
        status = {}
        if type(channel) == list and type(channel[0]) == list:
            for chl in channel:
                status[chl[-1]] = self.get_status(chl)
        else:
            status[channel] = self.get_status(channel)

        print('debug: status', status)

        #print("\n")
        #for chl, stats in status.items():
            #print("Chan:", chl, " Enabled?", stats['outputEnabled'], "\tCurr [mA]: ", round(stats['current'], 5), "\tV_bus:",round(stats['vbus'], 4))

        print("\n")
        #session.set_status('done')
        return

def main(args=None):
    args = site_config.parse_args(agent_class="LNAagent", args=args)
    agent, runner = ocs_agent.init_site_agent(args)
    LNA = LNAagent(agent)
    agent.register_task('get_status', LNA.get_status)
    agent.register_task('turn_on_channel', LNA.turn_on_channel)
    agent.register_task('turn_off_channel', LNA.turn_off_channel)
    agent.register_task('set_current', LNA.set_current)
    agent.register_task('set_voltage', LNA.set_voltage)
    #agent.register_task('load_channel_map', LNA.load_channel_map)
    agent.register_task('summarize_status', LNA.summarize_status)
    runner.run(agent, auto_reconnect=True)

if __name__ == '__main__':
    main()
