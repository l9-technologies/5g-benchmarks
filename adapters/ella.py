"""Ella Core 1.19 API provisioning through the common lab boundary."""
import json
from pathlib import Path
import secrets
from runner.evidence import write
from .common import KEY, OPC, service_profile, subscriber_sqn


def start(lab, count):
    count=max(count,2)  # Retain a second identity for the fresh-session recovery checks.
    profile = service_profile(lab.config)
    def api(path, body=None, method=None):
        args = ['curl', '--fail-with-body', '--silent', '--show-error', '--max-time', '10',
                '-X', method or ('POST' if body is not None else 'GET'),
                'http://127.0.0.1:5002/api/v1/' + path, '-H', 'Content-Type: application/json']
        if token: args += ['-H', 'Authorization: Bearer ' + token]
        if body is not None: args += ['--data', json.dumps(body)]
        return json.loads(lab.ns(lab.core_ns, args))['result']
    config = {'logging': {'system': {'level': 'info', 'output': 'stdout'}, 'audit': {'output': 'stdout'}},
              'db': {'path': str(lab.output/'ella.db')},
              'interfaces': {'n2': {'address': lab.config['n2_ip']}, 'n3': {'name': 'n3'},
                             'n6': {'name': 'dn0'}, 'api': {'address': '127.0.0.1', 'port': 5002}},
              'datapath': {'attach-mode': 'tcx'}}
    path = lab.output/'ella.yaml'; write(path, config)
    lab.start('core-ella', [Path(lab.config['ella_bin']), '--config', path], role='core')
    lab.wait(lambda: '5002' in lab.ns(lab.core_ns, ['ss', '-lntH']))
    token = None
    token = api('init', {'email': 'benchmark@example.invalid', 'password': secrets.token_urlsafe(24)})['token']
    api('operator/id', {'mcc': '001', 'mnc': '01'}, 'PUT')
    api('operator/tracking', {'supportedTacs': ['000001']}, 'PUT')
    api('operator/nas-security', {'ciphering': ['NULL'] if profile['cipher_algorithm']==0 else ['AES'], 'integrity': ['AES']}, 'PUT')
    api('networking/data-networks/internet', {'ipv4_pool': '10.45.0.0/24', 'dns': '192.0.2.1', 'mtu': 1400}, 'PUT')
    api('profiles/default', {'ue_ambr_uplink': '1000 Mbps', 'ue_ambr_downlink': '1000 Mbps', 'allow_4g': False, 'allow_5g': True}, 'PUT')
    api('slices/default', {'sst': 1, 'sd': profile['slice_sd']}, 'PUT')
    api('policies/default', {'profile_name': 'default', 'slice_name': 'default', 'data_network_name': 'internet',
                            'var5qi': 9, 'arp': 8, 'session_ambr_uplink': '1000 Mbps', 'session_ambr_downlink': '1000 Mbps'}, 'PUT')
    for i in range(1, count+1):
        # Ella advances its stored SQN by 32 before the first challenge.
        stored = subscriber_sqn(lab.config, i) - 32
        if stored < 0: raise ValueError('Ella first challenge SQN must be at least 32')
        api('subscribers', {'imsi': f'{1010000000000+i:015d}', 'key': KEY, 'opc': OPC,
                            'sequenceNumber': f'{stored:012x}', 'profile_name': 'default'})
    lab.wait(lambda: '38412' in lab.ns(lab.core_ns, ['ss', '-lSnH']))
