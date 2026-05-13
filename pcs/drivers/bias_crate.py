import redis
import txaio
import time
import json
txaio.use_twisted()

class BiasCrate:
    def __init__(self, host: str, port: int, pub_chan: str, sub_chan: str, timeout=30):
        self.log = txaio.make_logger()
        self.r = redis.Redis(host=host, port=port, decode_responses=True)
        try:
            self.r.ping()
        except redis.TimeoutError as e:
            err = f"Failed to connect to Redis server at {host}:{port} with Exception:\n {e}" 
            self.log.error(err)
            raise RuntimeError(err)

        self.timeout = timeout
        self.pub_chan, self.sub_chan = pub_chan, sub_chan
        self.p = self.r.pubsub(ignore_subscribe_messages=True)
        self.p.subscribe(sub_chan)
    
    # Bias Crate Commands
    # ===================
    def get_bias_cards(self):
        id, num_clients = self.pub_command('getAvailableCards')
        return self.sub_response(id)

    def enable_output(self, card: int, channel: int):
        id, num_clients = self.pub_command('enableOutput', card=card, channel=channel)
        return self.sub_response(id)
    
    def disable_output(self, card: int, channel: int):
        id, num_clients = self.pub_command('disableOutput', card=card, channel=channel)
        return self.sub_response(id)
    
    def get_status(self, card: int, channel: int):
        id, num_clients = self.pub_command('getStatus', card=card, channel=channel)
        return self.sub_response(id)
    
    def seek_voltage(self, card: int, channel: int, voltage: float):
        id, num_clients = self.pub_command('seekVoltage', card=card, channel=channel, voltage=voltage)
        return self.sub_response(id)
    
    def seek_current(self, card: int, channel: int, current: float):
        id, num_clients = self.pub_command('seekCurrent', card=card, channel=channel, current=current)
        return self.sub_response(id)
    
    def enable_testload(self, card: int, channel: int):
        id, num_clients = self.pub_command('enableTestload', card=card, channel=channel)
        return self.sub_response(id)

    def disable_testload(self, card: int, channel: int):
        id, num_clients = self.pub_command('disableTestload', card=card, channel=channel)
        return self.sub_response(id)
    
    # Redis helper methods
    # ====================
    def pub_command(self, command: str, **kwargs):
        id = int(time.time())
        com_dict = {'id': id,
                    'command': command,
                    'args': kwargs}
        num_clients = self.r.publish(self.pub_chan, json.dumps(com_dict))
        if num_clients == 0: self.log.error(f'No clients received command {command}!')
        return id, num_clients
    
    def sub_response(self, id: int):
        timed_out, last_resp = False, None
        while not timed_out:
            start_time = time.time()
            resp = self.p.get_message(timeout=self.timeout)
            timed_out = time.time() - start_time > self.timeout

            if resp is not None:
                last_resp = resp
                resp = json.loads(resp['data'])
                if resp['id'] == id: # Response from valid command should always match published ID
                    if resp['status'] == 'error':
                        self.log.error(f"Command exited with code {resp['code']}: {resp['msg']}")
                    return resp
        else:
            if last_resp is not None and last_resp['id'] == 0: # Invalid commands should return with ID == 0
                self.log.error(f"Invalid command exited with code {last_resp['code']}: {last_resp['msg']}")
            else:
                err = 'No response received from command. Check connection to Redis server.'
                self.log.error(err)
                last_resp = {'status': 'error', 'msg': err}
            return last_resp
    
