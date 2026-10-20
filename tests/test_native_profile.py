"""End-to-end adapter configuration through a recorded Linux lab boundary.

Failure cases: divergent subscriber counts, N2/N3 endpoints, NAS algorithms,
SQN, slice identifiers, or AMBR; hardcoded host paths; unbound CPU roles;
credentials written to the report. Live acceptance is a separate gate.
"""
import json
from pathlib import Path
import tempfile
import unittest
from adapters import common

class NativeProfile(unittest.TestCase):
    def test_shared_service_contract(self):
        profile = common.service_profile({'cipher_algorithm': 0, 'slice_sd': '000001', 'provisioned_sqn': 32})
        self.assertEqual(profile['security'], 'NIA2/NEA0')
        self.assertEqual(profile['slice'], '1/000001')
        self.assertEqual(profile['session_ambr_mbps'], 1000)
        with self.assertRaises(ValueError): common.service_profile({'cipher_algorithm': 9})

    def test_community_adapters_use_shared_lab(self):
        from adapters import ella, qcore
        with tempfile.TemporaryDirectory() as directory:
            class Lab:
                config = {'ella_bin': '/opt/ella-core', 'qcore_bin': '/opt/qcore', 'cipher_algorithm': 0, 'slice_sd': '000000', 'n2_ip': '10.3.0.2', 'n3_ip': '10.3.0.2'}
                output = Path(directory)
                core_ns = 'fixture-core'
                calls = []
                def ns(self, namespace, args):
                    self.calls.append(('ns', namespace, args))
                    if args[0] == 'curl':
                        return json.dumps({'result': {'token': 'fixture-token'}})
                    return '38412 5002'
                def start(self, name, args, **kwargs): self.calls.append(('start', name, list(map(str,args)), kwargs))
                def wait(self, predicate): assert predicate()
            lab = Lab();ella.start(lab,1);qcore.start(lab,1)
            cfg = json.loads((lab.output/'ella.yaml').read_text())
            self.assertEqual(cfg['interfaces']['n2']['address'], '10.3.0.2')
            self.assertEqual((lab.output/'sims.toml').read_text().count('[imsi-'),2)
            self.assertEqual([c[1] for c in lab.calls if c[0]=='start'], ['core-ella', 'core-qcore'])
            self.assertFalse(any(p.name.endswith('token') for p in lab.output.iterdir()))

class FreshCertificateState(unittest.TestCase):
    def test_each_trial_uses_its_own_certificate_copy(self):
        from adapters import free5gc
        from adapters.common import FREE_NFS
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'core';(root/'source/config').mkdir(parents=True);(root/'source/cert').mkdir()
            original=root/'source/cert/nrf.pem';original.write_text('fixture certificate')
            for nf in FREE_NFS:(root/'source/config'/f'{nf}cfg.yaml').write_text('oauth: true\npem: cert/nrf.pem\n')
            seen=[]
            for n in range(2):
                output=Path(directory)/str(n);output.mkdir()
                def start(name,args,role):
                    if name=='core-nrf':
                        cfg=json.loads((output/'free5gc/nrfcfg.yaml').read_text());self.assertTrue(cfg['oauth'])
                        path=Path(cfg['pem']);seen.append(path.read_text());path.write_text('changed fixture')
                lab=SimpleNamespace(config={'free5gc':str(root)},output=output,core_ns='fixture',start=start,ns=lambda *args:'8000 38412',start_database=lambda:None,provision_database=lambda *args:None,wait=lambda *args:None)
                free5gc.start(lab,1)
                self.assertEqual(original.read_text(),'fixture certificate')
            self.assertEqual(seen,['fixture certificate','fixture certificate'])

if __name__ == '__main__': unittest.main()
