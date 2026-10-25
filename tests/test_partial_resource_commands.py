"""Command E2E for an explicit partial-process resource comparison.

Failure cases: default data promoted to complete-core claims, missing behavior
checks, unmatched state policy, or an unlabelled partial resource boundary.
Fixture values test the reporting contract, not real core performance.
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]

class PartialResourceCommandTest(unittest.TestCase):
    def test_explicit_scope_keeps_default_and_feature_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)
            driver=root/'driver.py'
            driver.write_text('''import pathlib,json,os
p=pathlib.Path(os.environ['BENCH_RESULT_DIR']);plan=json.loads(pathlib.Path(os.environ['BENCH_PLAN']).read_text())
c=json.loads(os.environ['BENCH_CANDIDATE']);(p/'proof.txt').write_text('Fixture only')
scope=plan['protocol'].get('resource_accounting')
state={'service_profile':'fixture','durability_profile':'fresh-only-no-persistence-claim' if scope else None,
'resource_scope':'whole-host','process_resource_scope':'selected-core-processes' if scope else None}
(p/'measurement.json').write_text(json.dumps({'kind':'fixture','cleanup_ok':True,'quality_errors':[],
'metrics':{'core_idle_pss_bytes':100 if c['core']=='a' else 200,'udp_ul_host_cpu_seconds_per_gbit':1},'features':{'registration':'passed'},
'comparison_state':state,'evidence':['proof.txt']}))
''')
            candidates=[{'id':n,'core':c,'radio':'same','identity':{'core':c,'radio':'same'}}
                        for n,c in [('a','a'),('a-repeat','a'),('b','b'),('b-repeat','b')]]
            plan={'schema_version':1,'method_version':2,'mode':'fixture','repetitions':8,'seed':17,
                  'timeout_seconds':10,'equivalence_percent':5,'required_features':['registration'],
                  'metrics':['core_idle_pss_bytes','udp_ul_host_cpu_seconds_per_gbit'],'protocol':{'ue_counts':[1],'duration_seconds':30,
                  'warmup_seconds':1,'packet_bytes':1200,'offered_mbps_per_ue':1},
                  'driver':[sys.executable,str(driver)],'cwd':str(root),
                  'inputs':{str(driver):hashlib.sha256(driver.read_bytes()).hexdigest()},'candidates':candidates}
            def run(name):
                config=root/(name+'.json');config.write_text(json.dumps(plan))
                result=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'run',str(config),
                        '--output',str(root/name)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                return json.loads((root/name/'report.json').read_text())
            default=run('default')
            self.assertTrue(all('incomplete_resource_boundary' in row['reasons'] for row in default['comparisons'] if row['metric']=='core_idle_pss_bytes'))
            plan['protocol']['resource_accounting']='selected-core-processes'
            partial=run('partial')
            self.assertTrue(all('incomplete_resource_boundary' not in row['reasons'] for row in partial['comparisons']))
            self.assertTrue(all('kernel' in row['resource_boundary'] for row in partial['comparisons']))
            self.assertTrue(all('excludes kernel' in row['resource_boundary'] for row in partial['comparisons'] if row['metric']=='core_idle_pss_bytes'))
            self.assertTrue(all('including kernel' in row['resource_boundary'] for row in partial['comparisons'] if row['metric']=='udp_ul_host_cpu_seconds_per_gbit'))
            self.assertFalse(partial['publishable'])
            plan['required_features']=['registration','pdu_session']
            config=root/'failed.json';config.write_text(json.dumps(plan))
            result=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'run',str(config),
                       '--output',str(root/'failed')],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            report=json.loads((root/'failed/report.json').read_text())
            self.assertTrue(all('required_feature_failed' in row['reasons'] for row in report['comparisons']))

if __name__=='__main__':
    unittest.main()
