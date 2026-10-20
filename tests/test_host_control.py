"""Command E2E checks written before the host controller.
Failures: busy owner, changed adapter, missing headroom, noise, failed restore,
parent death, deadline expiry, lost evidence, or omitted controller on resume.
These fixtures do not establish live core performance.
"""
import hashlib,json,os,pathlib,signal,subprocess,sys,tempfile,time,unittest
from tests import test_studies
ROOT=pathlib.Path(__file__).resolve().parents[1]

class HostCommands(unittest.TestCase):
 def command(self,*args):
  return subprocess.run([sys.executable,str(ROOT/'benchmark.py'),*map(str,args)],capture_output=True,text=True,timeout=90)
 def inputs(self,root):
  profile,lab,driver=test_studies.StudyCommands().inputs(root)
  p=json.loads(profile.read_text());p['cases']=p['cases'][:1];p['confirmation']=[];profile.write_text(json.dumps(p))
  adapter=root/'host.py';adapter.write_text('''import json,os,pathlib,sys
root=pathlib.Path(__file__).parent;action=sys.argv[1];lease=pathlib.Path(sys.argv[2]);state=root/'ci-state'
if action=='snapshot': result={'passed':state.read_text()=='running','state':state.read_text()}
elif action=='acquire': state.write_text('paused');result={'passed':True}
elif action=='restore': state.write_text('running');result={'passed':not (root/'restore-fail').exists()}
elif action=='verify_restored': result={'passed':state.read_text()=='running'}
else: result={'passed':not (root/'noise').exists(),'generator_headroom':True}
print(json.dumps(result))
''')
  (root/'ci-state').write_text('running')
  host={'schema_version':1,'locks':[str(root/'host.lock')],'max_seconds':20,'restore_seconds':5,'sample_seconds':.1,
        'minimum_free_bytes':1,'minimum_available_memory_bytes':1,'inputs':{str(adapter):hashlib.sha256(adapter.read_bytes()).hexdigest()},
        'commands':{a:[sys.executable,str(adapter),a,'{lease}'] for a in ['snapshot','acquire','sample','restore','verify_restored']}}
  l=json.loads(lab.read_text());l['host_control']=host;lab.write_text(json.dumps(l));return profile,lab,driver
 def test_doctor_prepares_once_and_collect_resume_retains_host_receipts(self):
  with tempfile.TemporaryDirectory() as t:
   r=pathlib.Path(t);p,l,d=self.inputs(r)
   q=self.command('lab','doctor',p,'--lab',l,'--output',r/'doctor');self.assertEqual(q.returncode,0,q.stderr)
   doctor=json.loads(q.stdout);l=pathlib.Path(doctor['lab']);self.assertTrue(doctor['passed']);self.assertEqual((r/'ci-state').read_text(),'running');self.assertFalse(list((r/'doctor').rglob('trials/*/result.json')))
   q=self.command('study','collect',p,'--lab',l,'--output',r/'week','--max-trials','5');self.assertEqual(q.returncode,0,q.stderr)
   receipts=list((r/'week').rglob('host/lease.json'));self.assertEqual(len(receipts),5)
   self.assertTrue(all(json.loads(x.read_text())['restored'] for x in receipts));self.assertEqual((r/'ci-state').read_text(),'running')
   before={str(x):x.read_bytes() for x in (r/'week').rglob('trials/*/result.json')}
   q=self.command('study','resume',r/'week','--max-trials','1');self.assertEqual(q.returncode,0,q.stderr)
   for name,data in before.items():self.assertEqual(pathlib.Path(name).read_bytes(),data)
   q=self.command('study','status',r/'week');self.assertEqual(q.returncode,0,q.stderr);self.assertIn('next_action',json.loads(q.stdout))
   q=self.command('study','resume',r/'week');self.assertEqual(q.returncode,0,q.stderr);done=json.loads(q.stdout)
   (r/'host.py').rename(r/'host-hidden.py')
   replay=subprocess.run([sys.executable,'-I',str(r/'week/collection/report-tool.py'),'study','report',str(r/'week/collection')],capture_output=True,text=True,timeout=60)
   self.assertEqual(replay.returncode,0,replay.stderr)
   original=pathlib.Path(done['report']);rebuilt=pathlib.Path(json.loads(replay.stdout)['report'])
   self.assertEqual({x.name:x.read_bytes() for x in original.iterdir() if x.is_file()},{x.name:x.read_bytes() for x in rebuilt.iterdir() if x.is_file()})
   self.assertEqual(self.command('study','verify-archive',done['archive']['archive']).returncode,0)
 def test_noise_is_a_failed_measurement_and_restore_failure_stops(self):
  for marker in ['noise','restore-fail']:
   with self.subTest(marker=marker),tempfile.TemporaryDirectory() as t:
    r=pathlib.Path(t);p,l,d=self.inputs(r);(r/marker).touch()
    q=self.command('study','collect',p,'--lab',l,'--output',r/'week','--max-trials','2');self.assertEqual(q.returncode,2,q.stderr)
    self.assertEqual((r/'ci-state').read_text(),'running');self.assertTrue(list((r/'week').rglob('host/lease.json')))
    if marker=='restore-fail':
     rows=[json.loads(x.read_text()) for x in (r/'week').rglob('trials/*/result.json')];self.assertEqual(len(rows),1);self.assertFalse(rows[0]['measurement']['cleanup_ok'])
 def test_changed_host_input_rejected_without_pause(self):
  with tempfile.TemporaryDirectory() as t:
   r=pathlib.Path(t);p,l,d=self.inputs(r);(r/'host.py').write_text('raise Exception("changed")')
   q=self.command('study','collect',p,'--lab',l,'--output',r/'week','--max-trials','1');self.assertEqual(q.returncode,2);self.assertEqual((r/'ci-state').read_text(),'running')
 def test_guard_restores_after_controller_is_killed(self):
  with tempfile.TemporaryDirectory() as t:
   r=pathlib.Path(t);p,l,d=self.inputs(r)
   d.write_text('import time\ntime.sleep(60)\n');bp=r/'base.json';b=json.loads(bp.read_text());b['inputs'][str(d)]=hashlib.sha256(d.read_bytes()).hexdigest();bp.write_text(json.dumps(b))
   with (r/'command.log').open('w') as log:
    proc=subprocess.Popen([sys.executable,str(ROOT/'benchmark.py'),'study','collect',str(p),'--lab',str(l),'--output',str(r/'week'),'--max-trials','1'],stdout=log,stderr=log)
    try:
     end=time.monotonic()+20;lease=None
     while time.monotonic()<end:
      paths=list((r/'week').rglob('host/lease.json'))
      if paths:
       lease=json.loads(paths[0].read_text())
       if lease.get('job_pid'):break
      time.sleep(.05)
     self.assertIsNotNone(lease);self.assertTrue(lease.get('job_pid'));proc.kill();proc.wait(timeout=5)
     end=time.monotonic()+15
     while time.monotonic()<end and not json.loads(paths[0].read_text()).get('restored'):time.sleep(.1)
     self.assertTrue(json.loads(paths[0].read_text())['restored']);self.assertEqual((r/'ci-state').read_text(),'running')
    finally:
     if proc.poll() is None:proc.kill();proc.wait()

 def test_kill_during_partial_acquisition_does_not_pause_after_restore(self):
  with tempfile.TemporaryDirectory() as t:
   r=pathlib.Path(t);p,l,d=self.inputs(r)
   adapter=r/'host.py';adapter.write_text(adapter.read_text().replace("elif action=='acquire': state.write_text('paused');result={'passed':True}","elif action=='acquire':\n (root/'acquire-started').touch();__import__('time').sleep(2);state.write_text('paused');result={'passed':True}"))
   c=json.loads(l.read_text());c['host_control']['inputs'][str(adapter)]=hashlib.sha256(adapter.read_bytes()).hexdigest();l.write_text(json.dumps(c))
   with (r/'log').open('w') as log:
    proc=subprocess.Popen([sys.executable,str(ROOT/'benchmark.py'),'study','collect',str(p),'--lab',str(l),'--output',str(r/'week'),'--max-trials','1'],stdout=log,stderr=log)
    try:
     end=time.monotonic()+15
     while not (r/'acquire-started').exists() and time.monotonic()<end:time.sleep(.05)
     self.assertTrue((r/'acquire-started').exists());proc.kill();proc.wait(timeout=5)
     time.sleep(3);self.assertEqual((r/'ci-state').read_text(),'running')
     receipts=list((r/'week').rglob('host/lease.json'));self.assertTrue(json.loads(receipts[0].read_text())['restored'])
    finally:
     if proc.poll() is None:proc.kill();proc.wait()
 def test_busy_lock_and_memory_shortfall_do_not_change_ci(self):
  import fcntl
  for kind in ['busy','memory']:
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as t:
    r=pathlib.Path(t);p,l,d=self.inputs(r)
    with (r/'host.lock').open('a') as lock:
     if kind=='busy':fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
     else:
      config=json.loads(l.read_text());config['host_control']['minimum_available_memory_bytes']=2**60;l.write_text(json.dumps(config))
     q=self.command('lab','doctor',p,'--lab',l,'--output',r/'doctor');self.assertEqual(q.returncode,2,q.stderr)
     self.assertEqual((r/'ci-state').read_text(),'running')
 def test_noise_during_driver_and_timeout_remain_failed(self):
  for kind in ['noise','timeout']:
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as t:
    r=pathlib.Path(t);p,l,d=self.inputs(r)
    suffix="\n__import__('time').sleep(5)\n"
    if kind=='noise':suffix="\npathlib.Path(__file__).with_name('noise').touch()\n"+suffix
    d.write_text(d.read_text()+suffix);bp=r/'base.json';b=json.loads(bp.read_text());b['timeout_seconds']=1;b['inputs'][str(d)]=hashlib.sha256(d.read_bytes()).hexdigest();bp.write_text(json.dumps(b))
    q=self.command('study','collect',p,'--lab',l,'--output',r/'week','--max-trials','1');self.assertEqual(q.returncode,2,q.stderr)
    rows=[json.loads(x.read_text()) for x in (r/'week').rglob('trials/*/result.json')];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'failed')
    self.assertTrue(rows[0]['measurement']['quality_errors']);self.assertEqual((r/'ci-state').read_text(),'running')
