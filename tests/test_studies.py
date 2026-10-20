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
   summary=json.loads((pathlib.Path(json.loads(q.stdout)['report'])/'report.json').read_text());self.assertEqual(summary['expected'],8);self.assertEqual(summary['missing'],0)
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
   q=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(q.returncode,2,q.stderr)
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
 def test_installation_profile_checks_fit_separate_reserved_windows(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'installation'
   script=ROOT/'scripts/verify-live-study.py'
   def check(case):return subprocess.run([sys.executable,str(script),'--profile',str(profile),'--lab',str(lab),'--output',str(out),'--stage','profile','--case',case],capture_output=True,text=True,timeout=120)
   # This is a fixture boundary for a preceding interoperability stage.
   from importlib.util import spec_from_file_location,module_from_spec
   spec=spec_from_file_location('verification_script',script);tool=module_from_spec(spec);spec.loader.exec_module(tool)
   tool.initialize(profile,lab,out);interop=out/'interoperability';interop.mkdir()
   (interop/'base-plan.json').write_bytes((root/'base.json').read_bytes());(interop/'check-receipt.json').write_text(json.dumps({'check_passed':True}))
   first=check('connect-1');self.assertEqual(first.returncode,0,first.stderr)
   progress=json.loads((out/'verification-progress.json').read_text());self.assertIn('profile:connect-1',progress);self.assertNotIn('profile',progress)
   saved={str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')};self.assertEqual(len(saved),4)
   second=check('connect-10');self.assertEqual(second.returncode,0,second.stderr)
   progress=json.loads((out/'verification-progress.json').read_text());self.assertTrue(progress['profile']['check_passed'])
   for name,data in saved.items():self.assertEqual(pathlib.Path(name).read_bytes(),data)
   before=len(list(out.rglob('trials/*/result.json')));again=check('connect-1');self.assertEqual(again.returncode,0,again.stderr);self.assertEqual(len(list(out.rglob('trials/*/result.json'))),before)
 def test_failed_trial_is_retained_on_resume(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);text=driver.read_text().replace("'quality_errors':[]","'quality_errors':['intentional fixture failure'] if c['core']=='b' else []");driver.write_text(text);bp=json.loads(lab.read_text())['prepared_plan'];base=json.loads(pathlib.Path(bp).read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();pathlib.Path(bp).write_text(json.dumps(base));out=root/'out'
   q=self.command('study','run',profile,'--lab',lab,'--output',out,'--max-trials','4');self.assertEqual(q.returncode,2,q.stderr)
   failed=[p for p in out.rglob('trials/*/result.json') if json.loads(p.read_text())['status']=='failed'];self.assertEqual(len(failed),2);hashes={str(p):p.read_bytes() for p in failed}
   self.command('study','resume',out)
   for p,data in hashes.items():self.assertEqual(pathlib.Path(p).read_bytes(),data)
   s=json.loads(self.command('study','status',out).stdout);self.assertTrue(s['complete']);self.assertEqual(s['failed'],64)
 def test_collect_uses_frozen_source_checks_resume_report_and_archive(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'weekly'
   first=self.command('study','collect',profile,'--lab',lab,'--output',out,'--max-trials','2');self.assertEqual(first.returncode,0,first.stderr)
   status=json.loads(first.stdout);self.assertEqual(status['phase'],'checking');self.assertEqual(status['checks']['checked_trials'],2);self.assertEqual(status['collection']['collected'],0)
   saved={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('trials/*/result.json')}
   profile.rename(root/'original-profile-moved.json');lab.rename(root/'original-lab-moved.json')
   second=self.command('study','resume',out,'--max-trials','8');self.assertEqual(second.returncode,0,second.stderr)
   s=json.loads(second.stdout);self.assertTrue(s['checks']['check_passed']);self.assertEqual(s['collection']['collected'],2)
   for name,data in saved.items():self.assertEqual((out/name).read_bytes(),data)
   final=self.command('study','resume',out);self.assertEqual(final.returncode,0,final.stderr);s=json.loads(final.stdout)
   self.assertEqual(s['phase'],'complete');self.assertEqual(s['collection']['collected'],128)
   self.assertTrue(pathlib.Path(s['report']).joinpath('all-metrics.csv').exists());archive=pathlib.Path(s['archive']['archive'])
   self.assertEqual(self.command('study','verify-archive',archive).returncode,0)
   before={str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')};archive_bytes=archive.read_bytes()
   again=self.command('study','resume',out);self.assertEqual(again.returncode,0,again.stderr);self.assertEqual(json.loads(again.stdout)['report'],s['report'])
   self.assertEqual(archive.read_bytes(),archive_bytes)
   out.with_name(out.name+'.archive.json').unlink()  # Simulate a lost archive-receipt checkpoint.
   recovered=self.command('study','resume',out);self.assertEqual(recovered.returncode,0,recovered.stderr);self.assertEqual(archive.read_bytes(),archive_bytes)
   for name,data in before.items():self.assertEqual(pathlib.Path(name).read_bytes(),data)
   self.assertEqual(self.command('study','status',out).returncode,0)
   self.assertEqual(self.command('study','report',out).returncode,0)
 def test_collect_preserves_a_failed_check_and_does_not_start_main(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root)
   driver.write_text(driver.read_text().replace("'quality_errors':[]","'quality_errors':['intentional verification failure'] if c['core']=='b' else []"))
   bp=root/'base.json';base=json.loads(bp.read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();bp.write_text(json.dumps(base));out=root/'weekly'
   result=self.command('study','collect',profile,'--lab',lab,'--output',out);self.assertEqual(result.returncode,2,result.stderr);s=json.loads(result.stdout)
   self.assertEqual(s['phase'],'check_failed');self.assertEqual(s['collection']['collected'],0);self.assertFalse(s['checks']['check_passed'])
   self.assertTrue(pathlib.Path(s['archive']['archive']).exists());saved={str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')}
   result=self.command('study','resume',out);self.assertEqual(result.returncode,2,result.stderr)
   self.assertEqual(saved,{str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')})
 def test_collect_keeps_timed_failures_after_passing_verification(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root)
   driver.write_text(driver.read_text().replace("'quality_errors':[]","'quality_errors':['intentional timed failure'] if c['core']=='b' and json.loads(pathlib.Path(os.environ['BENCH_PLAN']).read_text())['protocol']['duration_seconds']==30 else []"))
   bp=root/'base.json';base=json.loads(bp.read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();bp.write_text(json.dumps(base));out=root/'weekly'
   result=self.command('study','collect',profile,'--lab',lab,'--output',out);self.assertEqual(result.returncode,2,result.stderr);s=json.loads(result.stdout)
   self.assertTrue(s['checks']['check_passed']);self.assertEqual(s['phase'],'complete_with_failures');self.assertEqual(s['collection']['failed'],64)
   rows=list(csv.DictReader(pathlib.Path(s['report']).joinpath('trial-outcomes.csv').read_text().splitlines()));self.assertEqual(sum(r['status']=='failed' for r in rows),64)
 def test_collect_archives_a_cleanup_failure_and_refuses_to_continue(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'weekly'
   driver.write_text(driver.read_text().replace("'cleanup_ok':True","'cleanup_ok':json.loads(pathlib.Path(os.environ['BENCH_PLAN']).read_text())['protocol']['duration_seconds']!=30"))
   bp=root/'base.json';base=json.loads(bp.read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();bp.write_text(json.dumps(base))
   q=self.command('study','collect',profile,'--lab',lab,'--output',out);self.assertEqual(q.returncode,2,q.stderr);s=json.loads(q.stdout)
   self.assertEqual(s['phase'],'cleanup_failed');self.assertEqual(s['collection']['collected'],1);self.assertTrue(pathlib.Path(s['archive']['archive']).exists())
   before={str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')}
   q=self.command('study','resume',out);self.assertEqual(q.returncode,2,q.stderr);self.assertEqual(before,{str(p):p.read_bytes() for p in out.rglob('trials/*/result.json')})
 def test_collect_refuses_changed_saved_inputs_and_source(self):
  for name in ['profile.json','source/runner/contracts.py']:
   with tempfile.TemporaryDirectory() as temp:
    root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'weekly'
    first=self.command('study','collect',profile,'--lab',lab,'--output',out,'--max-trials','1');self.assertEqual(first.returncode,0,first.stderr)
    victim=out/name;victim.write_text(victim.read_text()+'\n');saved=len(list(out.rglob('trials/*/result.json')))
    denied=self.command('study','resume',out);self.assertEqual(denied.returncode,2);self.assertIn('changed',denied.stderr)
    self.assertEqual(len(list(out.rglob('trials/*/result.json'))),saved)
 def test_collect_recovers_a_health_checkpoint_without_extra_checks(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'weekly'
   q=self.command('study','collect',profile,'--lab',lab,'--output',out,'--max-trials','2');self.assertEqual(q.returncode,0,q.stderr)
   path=next((out/'checks').glob('runs/*/*/manifest.json'));manifest=json.loads(path.read_text());key=next(reversed(manifest['result_hashes']));del manifest['result_hashes'][key];path.write_text(json.dumps(manifest))
   q=self.command('study','resume',out,'--max-trials','6');self.assertEqual(q.returncode,0,q.stderr);s=json.loads(q.stdout)
   self.assertTrue(s['checks']['check_passed']);self.assertEqual(s['checks']['checked_trials'],8);self.assertEqual(s['collection']['collected'],0)
 def test_collect_handles_a_real_interrupt_without_rewriting_the_failed_trial(self):
  import os,signal,time
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);profile,lab,driver=self.inputs(root);out=root/'weekly'
   driver.write_text(driver.read_text()+"\nif json.loads(pathlib.Path(os.environ['BENCH_PLAN']).read_text())['protocol']['duration_seconds']==30:\n (p/'interrupt-ready').write_text('ready')\n __import__('time').sleep(2)\n")
   bp=root/'base.json';base=json.loads(bp.read_text());base['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();bp.write_text(json.dumps(base))
   child=subprocess.Popen([sys.executable,str(ROOT/'benchmark.py'),'study','collect',str(profile),'--lab',str(lab),'--output',str(out)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
   try:
    deadline=time.monotonic()+30
    while not list((out/'collection').glob('runs/*/*/trials/*/interrupt-ready')):
     if child.poll() is not None or time.monotonic()>deadline:self.fail('workflow did not reach its timed driver')
     time.sleep(.02)
    os.killpg(child.pid,signal.SIGINT);child.communicate(timeout=30)
   finally:
    if child.poll() is None:os.killpg(child.pid,signal.SIGKILL);child.communicate()
   rows=list((out/'collection').glob('runs/*/*/trials/*/result.json'));self.assertEqual(len(rows),1)
   record=json.loads(rows[0].read_text());self.assertEqual(record['status'],'failed');self.assertEqual(record['driver_return_code'],130);before=rows[0].read_bytes()
   q=self.command('study','resume',out,'--max-trials','1');self.assertEqual(q.returncode,2,q.stderr);self.assertEqual(rows[0].read_bytes(),before)
   s=json.loads(q.stdout);self.assertEqual(s['collection']['collected'],2);self.assertEqual(s['collection']['failed'],1)
if __name__=='__main__':unittest.main()
