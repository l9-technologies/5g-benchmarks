"""Pilot command E2E: cover every candidate and device count before scaling."""
import collections,json,pathlib,subprocess,sys,tempfile,unittest
from tests import test_studies
ROOT=pathlib.Path(__file__).resolve().parents[1]

class PilotSelection(unittest.TestCase):
 def command(self,*args):
  return subprocess.run([sys.executable,str(ROOT/'benchmark.py'),*map(str,args)],capture_output=True,text=True,timeout=90)
 def test_pilot_coverage_resume_and_no_comparison_claim(self):
  with tempfile.TemporaryDirectory() as temporary:
   root=pathlib.Path(temporary);test_studies.StudyCommands().inputs(root);path=root/'base.json';plan=json.loads(path.read_text());plan['protocol']['ue_counts']=[1,10,50];path.write_text(json.dumps(plan));out=root/'pilot'
   q=self.command('run',path,'--output',out,'--pilot','--max-trials','1');self.assertEqual(q.returncode,2,q.stderr)
   saved={p:p.read_bytes() for p in out.glob('trials/*/result.json')};self.assertEqual(len(saved),1)
   q=self.command('run',path,'--output',out,'--resume');self.assertEqual(q.returncode,0,q.stderr)
   manifest=json.loads((out/'manifest.json').read_text());rows=manifest['schedule']
   self.assertEqual(collections.Counter((r['ue_count'],r['candidate']) for r in rows),collections.Counter({(n,c['id']):1 for n in [1,10,50] for c in plan['candidates']}))
   self.assertEqual({r['repetition'] for r in rows},{1})
   for p,data in saved.items():self.assertEqual(p.read_bytes(),data)
   report=json.loads((out/'report.json').read_text());self.assertEqual(report['status'],'batch_complete');self.assertFalse(report['comparison_claims_ready'])
   bad=self.command('run',path,'--output',root/'invalid','--pilot','--trial-ids',rows[0]['id']);self.assertNotEqual(bad.returncode,0);self.assertFalse((root/'invalid').exists())
