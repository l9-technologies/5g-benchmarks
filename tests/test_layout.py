"""Command checks for the module layout and independent report reconstruction.

Failure cases: missing export modules, test imports tied to the repository,
report tools that need the source tree, changed evidence accepted after export,
and public adapters that use a different shared lab boundary.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class LayoutCommandTest(unittest.TestCase):
    def test_export_and_saved_report_without_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);export=root/'export'
            p=subprocess.run([sys.executable,str(ROOT/'bundle.py'),'export','--output',str(export)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            for name in ('tests/test_neutrality.py','runner/statistics.py','runner/evidence.py','runner/reporting.py',
                         'adapters/open5gs.py','adapters/free5gc.py','adapters/ueransim.py',
                         'docs/getting-started.md','docs/adapter-contracts.md'):
                self.assertTrue((export/name).is_file(),name)
            self.assertFalse(list(export.glob('test_*.py')))
            driver=root/'driver.py'
            driver.write_text("import json,os,pathlib\np=pathlib.Path(os.environ['BENCH_RESULT_DIR'])\n(p/'proof.txt').write_text('fixture only')\n(p/'measurement.json').write_text(json.dumps({'kind':'fixture','cleanup_ok':True,'quality_errors':[],'metrics':{'attach_ready_ms':10},'features':{},'evidence':['proof.txt']}))\n")
            plan={'schema_version':1,'mode':'fixture','repetitions':6,'seed':17,'timeout_seconds':10,'equivalence_percent':10,
                  'protocol':{'ue_counts':[1],'duration_seconds':2,'warmup_seconds':1,'packet_bytes':1200,'offered_mbps_per_ue':1},
                  'driver':[sys.executable,str(driver)],'cwd':str(root),'inputs':{str(driver):hashlib.sha256(driver.read_bytes()).hexdigest()},
                  'candidates':[{'id':n,'core':'same','radio':'same','identity':{'core':'same','radio':'same'}} for n in ('candidate','candidate-repeat')]}
            path=root/'plan.json';path.write_text(json.dumps(plan));result=root/'result'
            p=subprocess.run([sys.executable,str(export/'benchmark.py'),'run',str(path),'--output',str(result)],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            before={name:(result/name).read_bytes() for name in ('report.json','report.md')}
            export.rename(root/'unavailable-source')
            p=subprocess.run([sys.executable,'-I',str(result/'report-tool.py'),'report',str(result)],cwd=root,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            for name,data in before.items():self.assertEqual((result/name).read_bytes(),data)
            proof=next(result.glob('trials/*/proof.txt'));proof.write_text('changed')
            p=subprocess.run([sys.executable,'-I',str(result/'report-tool.py'),'report',str(result)],cwd=root,capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertIn('hash',p.stderr)

    def test_public_adapters_keep_common_lab_calls(self):
        import native_lab
        from adapters import open5gs,free5gc,ueransim
        self.assertIs(native_lab.Lab.start_open5gs.__globals__['open5gs'],open5gs)
        self.assertIs(native_lab.Lab.start_free5gc.__globals__['free5gc'],free5gc)
        # Public adapters use the same process and namespace interface as external adapters.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            class RecordedLab:
                config={'radio_configs':str(ROOT/'configs'),'ueransim':str(root)}
                output=root
                core_ns='fixture-core'
                calls=[]
                def start(self,name,args,**kwargs):self.calls.append((name,args,kwargs))
                def wait(self,predicate):
                    (root/'gnb.log').write_text('NG Setup procedure is successful')
                    assert predicate()
                def ns(self,namespace,args):return 'uesimtun0 uesimtun1'
            lab=RecordedLab();elapsed,interfaces=ueransim.start(lab,2,'fixture-core')
            self.assertGreaterEqual(elapsed,0)
            self.assertEqual(interfaces,['uesimtun0','uesimtun1'])
            self.assertEqual([row[0] for row in lab.calls],['gnb','ue'])
            self.assertEqual([row[2]['role'] for row in lab.calls],['gnb','ue'])
            self.assertIn('--num-of-UE',lab.calls[1][1])
            self.assertIn('IA1: false',(root/'ue.yaml').read_text())

if __name__=='__main__':unittest.main()
