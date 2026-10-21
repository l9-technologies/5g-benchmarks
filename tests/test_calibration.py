"""Command checks before direct calibration implementation.
Reject malformed profiles before host changes. Do not overwrite results or
admit a core capacity claim from direct-tool data.
"""
import json,pathlib,subprocess,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class CalibrationCommands(unittest.TestCase):
 def test_invalid_cpu_and_duration_profiles_do_not_create_results(self):
  for change in [{'generator_cpus':[3,3]},{'duration_seconds':0},{'rates_mbps':[-1]},{'loss_percent':-1},{'receive_steering':'true'},{'iperf_bin':'relative'},{'iperf_build_receipt':'/opt/receipt.json'}]:
   with self.subTest(change=change),tempfile.TemporaryDirectory() as t:
    r=pathlib.Path(t);plan={'schema_version':1,'rates_mbps':[10],'packet_bytes':[1200],'streams':10,'duration_seconds':30,'warmup_seconds':5,'generator_cpus':[3,4],'observer_cpus':[5],'loss_percent':1,'headroom_multiple':2,**change};p=r/'plan.json';p.write_text(json.dumps(plan))
    q=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'lab','calibrate',str(p),'--output',str(r/'out')],capture_output=True,text=True,timeout=10)
    self.assertEqual(q.returncode,2);self.assertIn('calibration',q.stderr);self.assertFalse((r/'out').exists())
 def test_memory_idle_interval_and_mixed_packet_profile_compile(self):
  from tests import test_studies
  with tempfile.TemporaryDirectory() as t:
   root=pathlib.Path(t);profile,lab,driver=test_studies.StudyCommands().inputs(root)
   p=json.loads(profile.read_text());p['cases']=[{'id':'memory','ue_counts':[1,10,50],'metrics':['core_idle_pss_bytes'],'idle_seconds':30,'readiness_timeout_seconds':3,'probe_timestamp_precision':'nano','traffic_modes':[]},{'id':'mixed','ue_counts':[10],'metrics':['udp_ul_pps'],'traffic_modes':['udp_ul'],'packet_bytes_by_stream':[64]*5+[1200]*5}];p['confirmation']=[];profile.write_text(json.dumps(p))
   q=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'study','prepare',str(profile),'--lab',str(lab),'--output',str(root/'out')],capture_output=True,text=True,timeout=10)
   self.assertEqual(q.returncode,0,q.stderr)
   memory=json.loads((root/'out/cases/memory/main/plan.json').read_text());self.assertEqual(memory['protocol']['idle_seconds'],30);self.assertEqual(memory['protocol']['probe_timestamp_precision'],'nano')
   mixed=json.loads((root/'out/cases/mixed/main/plan.json').read_text());self.assertEqual(mixed['protocol']['packet_bytes_by_stream'],[64]*5+[1200]*5)
   p['cases'][1]['packet_bytes_by_stream']=[64];profile.write_text(json.dumps(p))
   bad=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'study','prepare',str(profile),'--lab',str(lab),'--output',str(root/'bad')],capture_output=True,text=True,timeout=10)
   self.assertEqual(bad.returncode,2);self.assertFalse((root/'bad').exists())
 def test_mixed_calibration_plan_has_one_payload_per_stream(self):
  # Profile validation is the trust boundary before Linux or root access.
  from runner.calibration import validate
  p={'schema_version':1,'rates_mbps':[10],'packet_bytes':[],'packet_patterns':[[64]*5+[1200]*5],'streams':10,'duration_seconds':30,'warmup_seconds':5,'generator_cpus':[3,4],'observer_cpus':[5],'loss_percent':1,'headroom_multiple':2}
  self.assertEqual(validate(p),p)
  p['packet_patterns']=[[64]]
  with self.assertRaises(ValueError):validate(p)
 def test_calibration_report_keeps_failed_points_and_rejects_changed_evidence(self):
  import csv,hashlib
  with tempfile.TemporaryDirectory() as t:
   r=pathlib.Path(t);raw=r/'raw';raw.mkdir()
   plan={'schema_version':1,'rates_mbps':[10],'packet_bytes':[64,1200],'streams':10,'duration_seconds':30,'warmup_seconds':5,'generator_cpus':[3,4],'observer_cpus':[5],'loss_percent':1,'headroom_multiple':2}
   (raw/'profile.json').write_text(json.dumps(plan))
   direction={'sender_mbps':10,'receiver_mbps':10,'receiver_pps':1000,'loss_percent':0,'load_matched':True}
   data={'kind':'direct-tool-calibration','comparison_admitted':False,'status':'completed_diagnostic','expected_points':2,'cleanup_ok':True,'cases':[{'payload_bytes':64,'offered_mbps':10,'passed':True,'cleanup_ok':True,'headroom':{'passed':True},'directions':{'udp_ul':direction,'udp_dl':direction},'metrics':{'udp_ul_mbps':10}},{'payload_bytes':1200,'offered_mbps':10,'passed':False,'cleanup_ok':True,'errors':['fixture delivery failure'],'directions':{},'metrics':{}}]}
   (raw/'calibration.json').write_text(json.dumps(data));(raw/'evidence.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()}))
   q=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'lab','calibration-report',str(raw),'--output',str(r/'report')],capture_output=True,text=True,timeout=10)
   self.assertEqual(q.returncode,0,q.stderr);rows=list(csv.DictReader((r/'report/points.csv').read_text().splitlines()))
   self.assertEqual(len(rows),4);self.assertEqual(sum(x['status']=='failed' for x in rows),2);self.assertTrue(all(x['receiver_mbps']=='' for x in rows if x['status']=='failed'))
   self.assertFalse(json.loads((r/'report/summary.json').read_text())['comparison_admitted'])
   (raw/'calibration.json').write_text((raw/'calibration.json').read_text()+' ')
   q=subprocess.run([sys.executable,str(ROOT/'benchmark.py'),'lab','calibration-report',str(raw),'--output',str(r/'changed')],capture_output=True,text=True,timeout=10)
   self.assertEqual(q.returncode,2);self.assertFalse((r/'changed').exists())
