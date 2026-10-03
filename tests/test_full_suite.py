"""Command checks for export, distributed evidence, and measurement boundaries.

Failure cases: a private dependency in the export, missing or duplicate shards,
changed evidence, different workloads or host classes, unpaired hosts, omitted
failed trials, sender-only traffic, missed timing events, and empty probes.
These fixture checks are not radio or core measurements.
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


class FullSuiteCommandTest(unittest.TestCase):
    def command(self, *args):
        return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)

    def test_export_runs_without_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / 'runner'
            r = self.command(ROOT / 'bundle.py', 'export', '--output', output)
            self.assertEqual(r.returncode, 0, r.stderr)
            manifest = json.loads((output / 'bundle.json').read_text())
            self.assertTrue((output / 'LICENSE').is_file())
            self.assertFalse((output/'private_adapters.py').exists())
            for name, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
            for name in ('benchmark.py', 'native_lab.py'):
                r = self.command(output / name, '--help')
                self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn('/Users/', (output / 'docs/methodology.md').read_text())

    def test_distributed_run_and_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            driver = root / 'driver.py'
            driver.write_text('''import json,os,pathlib
p=pathlib.Path(os.environ['BENCH_RESULT_DIR'])
(p/'proof.txt').write_text('fixture only')
(p/'measurement.json').write_text(json.dumps({'kind':'fixture','cleanup_ok':True,
'quality_errors':[],'metrics':{'attach_ready_ms':10},'features':{},'evidence':['proof.txt']}))
''')
            candidates = [{'id': n, 'core': c, 'radio': 'same', 'identity': {'core': c, 'radio': 'same'}}
                          for n,c in [('ours','ours'),('repeat','ours'),('peer','peer')]]
            plan = {'schema_version':1,'mode':'fixture','repetitions':6,'seed':17,
                    'timeout_seconds':10,'equivalence_percent':10,
                    'protocol':{'ue_counts':[1,10],'duration_seconds':2,'warmup_seconds':1,
                                'packet_bytes':1200,'offered_mbps_per_ue':1},
                    'driver':[sys.executable,str(driver)],'cwd':str(root),
                    'inputs':{str(driver):hashlib.sha256(driver.read_bytes()).hexdigest()},'candidates':candidates}
            path=root/'plan.json';path.write_text(json.dumps(plan))
            shards=[root/f'shard{i}' for i in range(2)]
            for i,output in enumerate(shards):
                r=self.command(ROOT/'benchmark.py','run',path,'--output',output,'--shard-index',i,'--shard-count',2)
                self.assertEqual(r.returncode,0,r.stderr)
            output=root/'merged'
            r=self.command(ROOT/'benchmark.py','merge',*shards,'--output',output)
            self.assertEqual(r.returncode,0,r.stderr)
            report=json.loads((output/'report.json').read_text())
            self.assertEqual(report['trial_count'],36)
            self.assertFalse(report['publishable'])
            self.assertEqual(report['status'],'verification_only')
            before=(output/'report.json').read_bytes()
            r=self.command(output/'report-tool.py','report',output)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(before,(output/'report.json').read_bytes())
            r=self.command(ROOT/'benchmark.py','merge',shards[0],shards[0],'--output',root/'duplicate')
            self.assertNotEqual(r.returncode,0)
            r=self.command(ROOT/'benchmark.py','merge',shards[0],'--output',root/'missing')
            self.assertNotEqual(r.returncode,0)
            next(shards[1].glob('trials/*/proof.txt')).write_text('changed')
            r=self.command(ROOT/'benchmark.py','merge',*shards,'--output',root/'changed')
            self.assertNotEqual(r.returncode,0)

    def test_procedure_timing_and_probe_loss(self):
        import native_lab
        text='''[2026-10-02 12:00:00.000] [nas] Sending Initial Registration
[2026-10-02 12:00:00.050] [nas] Initial Registration is successful
[2026-10-02 12:00:00.051] [nas] Sending PDU Session Establishment Request
[2026-10-02 12:00:00.151] [nas] PDU Session establishment is successful
'''
        result=native_lab.procedure_metrics(text,1)
        self.assertAlmostEqual(result['registration_p50_ms'],50,places=2)
        self.assertAlmostEqual(result['session_p50_ms'],100,places=2)
        with self.assertRaises(ValueError):native_lab.procedure_metrics(text,2)
        result=native_lab.ping_metrics('time=1.0 ms\ntime=3.0 ms\n3 packets transmitted, 2 received')
        self.assertAlmostEqual(result['loss_percent'],100/3)
        self.assertEqual(result['p99_ms'],3)
        with self.assertRaises(ValueError):native_lab.ping_metrics('')
        self.assertEqual(native_lab.ping_metrics('3 packets transmitted, 0 received')['loss_percent'],100)


if __name__ == '__main__':unittest.main()
