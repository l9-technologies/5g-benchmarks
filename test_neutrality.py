"""Command checks for method 2.

Failure cases: label-dependent decisions, asymmetric margins, a missing peer
baseline, hidden failed comparisons, unequal authentication/durability, kernel
cost omitted from a core claim, and a report that hides higher UE counts.
Fixtures verify method behavior. They are not live measurements.
"""
import collections
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent


class NeutralityCommandTest(unittest.TestCase):
    def test_command_keeps_all_outcomes_and_candidate_baselines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            driver=root/'driver.py'
            driver.write_text('''import json,os,pathlib
p=pathlib.Path(os.environ['BENCH_RESULT_DIR'])
c=json.loads(os.environ['BENCH_CANDIDATE'])
(p/'proof.txt').write_text('Fixture only')
v=100 if c['core']=='a' else 111
state={'service_profile':'fixture-service','authentication_profile':'fixture-auth',
'durability_profile':None,'resource_scope':'process-only','radio_capability_profile':None}
(p/'measurement.json').write_text(json.dumps({'kind':'fixture','cleanup_ok':True,
'quality_errors':[],'metrics':{'attach_ready_ms':v,'core_idle_rss_bytes':v},
'features':{'registration':'passed'},'comparison_state':state,'evidence':['proof.txt']}))
''')
            candidates=[{'id':n,'core':c,'radio':'same','identity':{'core':c,'radio':'same'}}
                        for n,c in [('vendor-a','a'),('vendor-a-repeat','a'),('peer','b'),('peer-repeat','b')]]
            plan={'schema_version':1,'method_version':2,'mode':'fixture','repetitions':8,'seed':17,
                  'timeout_seconds':10,'equivalence_percent':10,'required_features':['registration'],
                  'protocol':{'ue_counts':[1,10],'duration_seconds':2,'warmup_seconds':1,
                              'packet_bytes':1200,'offered_mbps_per_ue':1},
                  'driver':[sys.executable,str(driver)],'cwd':str(root),
                  'inputs':{str(driver):hashlib.sha256(driver.read_bytes()).hexdigest()},'candidates':candidates}
            config=root/'plan.json';config.write_text(json.dumps(plan))
            command=[sys.executable,str(ROOT/'benchmark.py')]
            p=subprocess.run(command+['run',str(config),'--output',str(root/'run')],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            report=json.loads((root/'run/report.json').read_text())
            self.assertEqual(report['method_version'],2)
            self.assertEqual(report['trial_count'],64)
            import benchmark
            self.assertEqual(len(report['comparisons']),2*4*len(benchmark.METRICS))
            self.assertTrue(any('incomplete_comparison_state' in c['reasons'] for c in report['comparisons'] if c['metric']=='core_idle_rss_bytes'))
            self.assertTrue(any('missing_metric' in c['reasons'] for c in report['comparisons']))
            self.assertFalse(report['publishable'])
            for c in report['comparisons']:
                if c['metric']=='attach_ready_ms':self.assertEqual(c['statistical_decision'],'a_better')
            for count in (1,10):
                rows=[r for r in benchmark.schedule(plan) if r['ue_count']==count]
                for name in [c['id'] for c in candidates]:
                    positions=collections.Counter(i%4 for i,r in enumerate(rows) if r['candidate']==name)
                    self.assertEqual(set(positions.values()),{2})
            original=(root/'run/report.json').read_bytes()
            p=subprocess.run(command+['report',str(root/'run')],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(original,(root/'run/report.json').read_bytes())
            plan['required_features']=['registration','paging']
            config.write_text(json.dumps(plan))
            p=subprocess.run(command+['run',str(config),'--output',str(root/'missing-feature')],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            failed=json.loads((root/'missing-feature/report.json').read_text())
            self.assertEqual(failed['trial_count'],64)
            evidence=json.loads(next((root/'missing-feature').glob('trials/*/driver-measurement.json')).read_text())
            self.assertNotIn('paging',evidence['features'])
            self.assertEqual(len(failed['required_feature_failures']),64)
            self.assertFalse(failed['publishable'])
            self.assertTrue(all(c['decision']=='required_feature_failed' for c in failed['comparisons']))
            # Every distinct core/radio needs its own repeat.
            plan['candidates']=candidates[:-1]
            config.write_text(json.dumps(plan))
            p=subprocess.run(command+['validate',str(config)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)

    def test_live_plan_rejects_old_method(self):
        import benchmark
        with self.assertRaisesRegex(ValueError,'method 2'):
            benchmark.validate_plan({'schema_version':1,'mode':'live'})

    def test_swap_and_zero_values_use_symmetric_rules(self):
        import benchmark
        a=[100]*8;b=[111]*8
        first=benchmark.neutral_statistics(a,b,17,10,'lower')
        second=benchmark.neutral_statistics(b,a,17,10,'lower')
        self.assertEqual(first['margin'],second['margin'])
        self.assertEqual(first['decision'],'a_better')
        self.assertEqual(second['decision'],'b_better')
        self.assertEqual(first['absolute_interval'],[-second['absolute_interval'][1],-second['absolute_interval'][0]])
        self.assertEqual(benchmark.neutral_statistics([0]*8,[0]*8,17,10,'lower')['decision'],'equivalent')
        values=[1,3,2,8,5,6,9,4]
        x=benchmark.neutral_statistics(values,b,17,10,'lower')
        y=benchmark.neutral_statistics(b,values,17,10,'lower')
        for i in (0,1):self.assertAlmostEqual(x['absolute_interval'][i],-y['absolute_interval'][1-i])

    def test_state_contracts_and_host_accounting(self):
        import benchmark,native_lab
        state={'service_profile':'common','authentication_profile':'same','durability_profile':'verified-same',
               'resource_scope':'whole-host','radio_capability_profile':'verified-same'}
        rows=[{'measurement':{'comparison_state':state}}]*8
        self.assertEqual(benchmark.comparison_reasons('attach_ready_ms',rows,rows,'core'),[])
        changed=[{'measurement':{'comparison_state':{**state,'authentication_profile':'other'}}}]*8
        self.assertIn('comparison_state_mismatch',benchmark.comparison_reasons('attach_ready_ms',rows,changed,'core'))
        self.assertIn('incomplete_resource_boundary',benchmark.comparison_reasons('core_cpu_seconds',rows,rows,'core'))
        self.assertEqual(benchmark.comparison_reasons('tcp_ul_host_cpu_seconds_per_gbit',rows,rows,'core'),[])
        self.assertEqual(native_lab.initial_sqn(1),32)
        self.assertEqual(native_lab.initial_sqn(200),6400)


if __name__=='__main__':unittest.main()
