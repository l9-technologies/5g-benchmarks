"""Command E2E checks before implementation.
Failure cases: lost/changed trials on resume; hidden failure; partial plans
reported as complete; unequal case protocols; an unconfirmed claim; changed
profile/plan; path traversal; unsafe cleanup; missing archive members.
Fixtures do not establish live core performance.
"""
import unittest,pathlib,tempfile,subprocess,sys,json,hashlib,csv,tarfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
class StudyCommands(unittest.TestCase):
 def command(self,*args):return subprocess.run([sys.executable,str(ROOT/'benchmark.py'),*map(str,args)],capture_output=True,text=True,timeout=120)
 def inputs(self,root):
  driver=root/'driver.py';driver.write_text('''import os,json,pathlib
p=pathlib.Path(os.environ['BENCH_RESULT_DIR']);c=json.loads(os.environ['BENCH_CANDIDATE']);(p/'proof.txt').write_text('fixture')
m={'kind':'fixture','cleanup_ok':True,'quality_errors':[],'features':{'registration':'passed','cleanup':'passed'},'metrics':{'attach_ready_ms':10 if c['core']=='a' else 20},'evidence':['proof.txt']}
(p/'measurement.json').write_text(json.dumps(m))
''')
  base={'schema_version':1,'method_version':2,'mode':'fixture','seed':1729,'repetitions':8,'timeout_seconds':5,'equivalence_percent':5,'required_features':['registration','cleanup'],'protocol':{'ue_counts':[1],'duration_seconds':30,'warmup_seconds':5,'packet_bytes':1200,'offered_mbps_per_ue':1},'driver':[sys.executable,str(driver)],'cwd':str(root),'inputs':{str(driver):hashlib.sha256(driver.read_bytes()).hexdigest()},'candidates':[{'id':c+suffix,'core':c,'radio':'common','identity':{'core':c,'radio':'same'}} for c in ['a','b'] for suffix in ['','-repeat']]}
  basepath=root/'base.json';basepath.write_text(json.dumps(base));lab=root/'lab.json';lab.write_text(json.dumps({'prepared_plan':str(basepath)}));profile=root/'profile.json';profile.write_text(json.dumps({'schema_version':1,'name':'example','repetitions':8,'seed':1729,'equivalence_percent':5,'cases':[{'id':'connect-1','ue_counts':[1],'metrics':['attach_ready_ms']},{'id':'connect-10','ue_counts':[10],'metrics':['attach_ready_ms']}],'confirmation':['connect-1','connect-10']}));return profile,lab,driver
 def test_profile_run_resume_report_archive(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'study'
   first=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(first.returncode,0,first.stderr)
   status=json.loads(self.command('study','status',out).stdout);self.assertEqual(status['collected'],4);self.assertFalse(status['complete'])
   saved={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('trials/*/result.json')};self.assertEqual(len(saved),4)
   resumed=self.command('study','resume',out);self.assertEqual(resumed.returncode,0,resumed.stderr)
   for name,data in saved.items():self.assertEqual((out/name).read_bytes(),data)
   status=json.loads(self.command('study','status',out).stdout);self.assertEqual(status['collected'],128);self.assertTrue(status['complete']);self.assertEqual(status['failed'],0)
   report=self.command('study','report',out);self.assertEqual(report.returncode,0,report.stderr);folder=pathlib.Path(json.loads(report.stdout)['report'])
   outcomes=list(csv.DictReader((folder/'trial-outcomes.csv').read_text().splitlines()));self.assertEqual(len(outcomes),128)
   comparisons=json.loads((folder/'comparisons.json').read_text());self.assertTrue(comparisons);self.assertFalse(any(x['claim_eligible'] for x in comparisons),'Fixture promoted to performance claim')
   original={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
   replay=subprocess.run([sys.executable,str(out/'report-tool.py'),'study','report',str(out)],capture_output=True,text=True,timeout=120);self.assertEqual(replay.returncode,0,replay.stderr);again=pathlib.Path(json.loads(replay.stdout)['report']);self.assertEqual(original,{p.name:p.read_bytes() for p in again.iterdir() if p.is_file()})
   archive=root/'evidence.tgz';a=self.command('study','archive',out,'--output',archive);self.assertEqual(a.returncode,0,a.stderr)
   verified=self.command('study','verify-archive',archive);self.assertEqual(verified.returncode,0,verified.stderr)
   victim=next(out.rglob('trials/*/result.json'));victim.write_text(victim.read_text()+' ')
   denied=self.command('study','resume',out);self.assertNotEqual(denied.returncode,0);self.assertIn('hash',denied.stderr)
 def test_health_check_covers_each_adapter_without_a_performance_claim(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'check'
   q=self.command('study','check',profile,'--lab',lab,'--output',out);self.assertEqual(q.returncode,0,q.stderr)
   result=json.loads(q.stdout);self.assertTrue(result['check_passed']);self.assertTrue(result['verification_only']);self.assertEqual(result['checked_candidates'],4)
   rows=[json.loads(p.read_text()) for p in out.rglob('trials/*/result.json')];self.assertEqual(len(rows),8)
   self.assertEqual(len({r['trial']['candidate'] for r in rows}),4)
   q=self.command('study','report',out);self.assertEqual(q.returncode,0,q.stderr)
   comparisons=json.loads((pathlib.Path(json.loads(q.stdout)['report'])/'comparisons.json').read_text())
   self.assertFalse(any(c['claim_eligible'] for c in comparisons))
 def test_resume_recovers_a_complete_result_without_rerunning_it(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'study'
   q=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(q.returncode,0,q.stderr)
   manifest=next(out.glob('runs/*/*/manifest.json'));m=json.loads(manifest.read_text());name=next(iter(m['result_hashes']));del m['result_hashes'][name];manifest.write_text(json.dumps(m))
   result=manifest.parent/'trials'/name/'result.json';before=result.read_bytes()
   q=self.command('study','resume',out);self.assertEqual(q.returncode,0,q.stderr);self.assertEqual(result.read_bytes(),before)
   self.assertTrue(list(manifest.parent.glob('recoveries/*.json')))
 def test_declared_delivery_limit_is_checked_for_every_adapter(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);p=json.loads(profile.read_text());p['defaults']={'traffic_modes':['tcp_dl'],'load_tolerance_percent':5};profile.write_text(json.dumps(p));out=root/'out'
   q=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(q.returncode,0,q.stderr)
   rows=[json.loads(p.read_text()) for p in out.rglob('trials/*/result.json')];self.assertEqual(len(rows),4)
   self.assertTrue(all(r['status']=='failed' and 'delivered load' in r['error'] for r in rows))
 def test_unverified_metric_keeps_its_value_and_blocks_its_comparison(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);driver.write_text(driver.read_text().replace("'quality_errors':[]","'metric_notes':{'attach_ready_ms':'Unverified timer boundary'},'quality_errors':[]"));bp=pathlib.Path(json.loads(lab.read_text())['prepared_plan']);base=json.loads(bp.read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();bp.write_text(json.dumps(base));out=root/'study'
   q=self.command('study','run',profile,'--lab',lab,'--output',out);self.assertEqual(q.returncode,0,q.stderr)
   report=pathlib.Path(json.loads(q.stdout)['report']);rows=json.loads((report/'comparisons.json').read_text());self.assertTrue(rows)
   self.assertTrue(all('metric_boundary_unverified' in row['reasons'] for row in rows))
   metrics=list(csv.DictReader((report/'all-metrics.csv').read_text().splitlines()));self.assertTrue(any(row['value']=='10' and row['metric_note']=='Unverified timer boundary' for row in metrics))
 def test_invalid_profile_and_unknown_keys(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);p=json.loads(profile.read_text());p['cases'][0]['typo']=True;profile.write_text(json.dumps(p))
   q=self.command('study','prepare',profile,'--lab',lab,'--output',root/'out');self.assertNotEqual(q.returncode,0);self.assertIn('typo',q.stderr)
 def test_failed_trial_is_retained_on_resume(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);text=driver.read_text().replace("'quality_errors':[]","'quality_errors':['intentional fixture failure'] if c['core']=='b' else []");driver.write_text(text);bp=json.loads(lab.read_text())['prepared_plan'];base=json.loads(pathlib.Path(bp).read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();pathlib.Path(bp).write_text(json.dumps(base));out=root/'out'
   q=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(q.returncode,0,q.stderr)
   failed=[p for p in out.rglob('trials/*/result.json') if json.loads(p.read_text())['status']=='failed'];self.assertEqual(len(failed),2);hashes={str(p):p.read_bytes() for p in failed}
   self.command('study','resume',out)
   for p,data in hashes.items():self.assertEqual(pathlib.Path(p).read_bytes(),data)
   s=json.loads(self.command('study','status',out).stdout);self.assertTrue(s['complete']);self.assertEqual(s['failed'],64)
if __name__=='__main__':unittest.main()
