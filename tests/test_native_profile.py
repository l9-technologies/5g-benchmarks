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
    def test_input_permissions_distinguish_programs_from_data(self):
        from adapters.common import validate_runtime_inputs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);program=root/'program';program.write_text('fixture');program.chmod(0o755)
            receipt=root/'receipt.json';receipt.write_text('{}');receipt.chmod(0o644)
            validate_runtime_inputs([program,receipt],[program])
            with self.assertRaises(ValueError):validate_runtime_inputs([program,receipt],[receipt])
            with self.assertRaises(ValueError):validate_runtime_inputs([program,root/'missing'],[program])

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
            for nf in FREE_NFS:(root/'source/config'/f'{nf}cfg.yaml').write_text('oauth: true\npem: cert/nrf.pem\nexpires: 2026-10-21T00:00:00Z\n')
            seen=[]
            for n in range(2):
                output=Path(directory)/str(n);output.mkdir()
                def start(name,args,role):
                    if name=='core-nrf':
                        cfg=json.loads((output/'free5gc/nrfcfg.yaml').read_text());self.assertTrue(cfg['oauth']);self.assertEqual(cfg['expires'],'2026-10-21T00:00:00Z')
                        path=Path(cfg['pem']);seen.append(path.read_text());path.write_text('changed fixture')
                lab=SimpleNamespace(config={'free5gc':str(root)},output=output,core_ns='fixture',start=start,ns=lambda *args:'8000 38412',start_database=lambda:None,provision_database=lambda *args:None,wait=lambda *args:None)
                free5gc.start(lab,1)
                self.assertEqual(original.read_text(),'fixture certificate')
            self.assertEqual(seen,['fixture certificate','fixture certificate'])

if __name__ == '__main__': unittest.main()

class ServiceConfigurationCommands(unittest.TestCase):
    def test_slice_override_reaches_every_open5gs_service(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'open5gs_config.py'),'--config-dir',str(root/'config'),'--interface','lo','--n3-advertise-host','10.3.0.2','--slice-sd','000000','--metadata',str(root/'meta.json')]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in ['amf','smf','nssf']:
                data=json.loads((root/'config'/(name+'.yaml')).read_text());slices=[]
                def visit(value):
                    if isinstance(value,dict):
                        for key,item in value.items():
                            if key=='sd':slices.append(item)
                            else:visit(item)
                    elif isinstance(value,list):
                        for item in value:visit(item)
                visit(data);self.assertTrue(slices);self.assertEqual(set(slices),{'000000'})

class NasProfileEvidence(unittest.TestCase):
    def test_each_ue_needs_the_declared_challenge_and_algorithms(self):
        from adapters.ueransim import profile_proof
        lines=[]
        for i in range(1,3):
            prefix=f'[2026-10-20 00:00:00.001] [{1010000000000+i:015d}|nas] [debug] '
            lines += [prefix+'Received SQN [000000000020]',prefix+'Selected integrity[2] ciphering[0]']
        log='\n'.join(lines);config={'cipher_algorithm':0,'provisioned_sqn':32}
        self.assertTrue(profile_proof(log,2,config)['passed'])
        for bad in [log.replace('000000000020','000000000040',1),log+'\n'+lines[0],log.replace('ciphering[0]','ciphering[2]',1),log.replace(lines[0],'')]:
            self.assertFalse(profile_proof(bad,2,config)['passed'])

class SubscriberPopulation(unittest.TestCase):
    def test_recovery_identity_is_explicit_and_applies_to_every_core(self):
        from adapters.common import subscriber_population
        self.assertEqual(subscriber_population({},1),2)
        self.assertEqual(subscriber_population({'minimum_subscribers':1},1),1)
        self.assertEqual(subscriber_population({'minimum_subscribers':1},10),10)
        with self.assertRaises(ValueError):subscriber_population({'minimum_subscribers':0},1)
        from native_lab import Lab
        with tempfile.TemporaryDirectory() as directory:
            class Recorded(Lab):
                def start_open5gs(self,count):return count
            for minimum in [1,2]:
                output=Path(directory)/str(minimum);output.mkdir()
                lab=Recorded({'minimum_subscribers':minimum},output)
                self.assertEqual(lab.start_core('open5gs',1),minimum)

class ProcessCustody(unittest.TestCase):
    def test_cancellation_after_spawn_keeps_the_process_owned(self):
        # A signal between spawn and tracking must not leave an unowned child.
        import signal
        from unittest.mock import patch
        from native_lab import Lab
        with tempfile.TemporaryDirectory() as directory:
            lab=Lab({},Path(directory))
            class Child:
                pid=43210
            def spawn(*args,**kwargs):
                lab.interrupt(signal.SIGTERM)
                return Child()
            with patch('native_lab.subprocess.Popen',side_effect=spawn):
                with self.assertRaises(InterruptedError):lab.start('fixture',['fixture-program'],role='core')
            self.assertEqual([p.pid for p,_,_ in lab.processes],[43210])

class NegativeProfileConfiguration(unittest.TestCase):
    def test_slice_rejection_changes_the_ue_after_common_profile_setup(self):
        from adapters import ueransim
        with tempfile.TemporaryDirectory() as directory:
            class Recorded:
                config={'radio_configs':str(Path(__file__).resolve().parents[1]/'configs'),'ueransim':directory,
                        'slice_sd':'000000','cipher_algorithm':0,'ue_variant':'bad-slice'}
                core_ns='fixture-core';output=Path(directory)
                def start(self,name,args,**kwargs):
                    if name=='gnb':(self.output/'gnb.log').write_text('NG Setup procedure is successful')
                def wait(self,predicate):assert predicate()
                def ns(self,*args):return 'uesimtun0'
            lab=Recorded();ueransim.start(lab,1,'open5gs')
            self.assertIn('sd: 0',(lab.output/'gnb.yaml').read_text())
            self.assertIn('sd: 1',(lab.output/'ue.yaml').read_text())
