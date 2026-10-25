"""Command E2E: selected batches must resume, merge, and retain DUT failures.
Reject changed selections, unknown IDs, duplicates, reordering and missing or
changed evidence. Partial batches must not become complete comparisons.
"""
import hashlib,json,pathlib,subprocess,sys,tempfile,unittest
from runner.contracts import schedule
from tests import test_studies
ROOT=pathlib.Path(__file__).resolve().parents[1]

class TrialBatches(unittest.TestCase):
 def command(self,*args):
  return subprocess.run([sys.executable,str(ROOT/'benchmark.py'),*map(str,args)],capture_output=True,text=True,timeout=90)
 def test_checkpoint_resume_merge_and_failed_core(self):
  with tempfile.TemporaryDirectory() as temporary:
   root=pathlib.Path(temporary);_,_,driver=test_studies.StudyCommands().inputs(root);path=root/'base.json';plan=json.loads(path.read_text())
   driver.write_text(driver.read_text().replace("'quality_errors':[]","'quality_errors':['injected core failure'] if c['id']=='b' else []"));plan['inputs'][str(driver)]=hashlib.sha256(driver.read_bytes()).hexdigest();path.write_text(json.dumps(plan))
   ids=[r['id'] for r in schedule(plan)];outputs=[];saved={}
   for offset in range(0,len(ids),5):
    out=root/('batch-'+str(offset));outputs.append(out);selected=ids[offset:offset+5]
    first=self.command('run',path,'--output',out,'--trial-ids',*selected,'--max-trials','1')
    self.assertTrue((out/'manifest.json').exists(),first.stderr)
    before={p:p.read_bytes() for p in out.glob('trials/*/result.json')};self.assertEqual(len(before),1)
    wrong=self.command('run',path,'--output',out,'--resume','--trial-ids',*ids[-1:]);self.assertNotEqual(wrong.returncode,0);self.assertIn('selection',wrong.stderr)
    self.command('run',path,'--output',out,'--resume')
    for p,data in before.items():self.assertEqual(p.read_bytes(),data)
    manifest=json.loads((out/'manifest.json').read_text());self.assertEqual([r['id'] for r in manifest['schedule']],selected)
    for p in out.glob('trials/*/result.json'):saved[p.parent.name]=p.read_bytes()
    self.assertFalse(json.loads((out/'report.json').read_text())['publishable'])
   merged=root/'merged';self.command('merge',*outputs,'--output',merged)
   rows=[json.loads(p.read_text()) for p in merged.glob('trials/*/result.json')]
   self.assertEqual(len(rows),len(ids));self.assertEqual(sum(r['status']=='failed' for r in rows),8)
   for p in merged.glob('trials/*/result.json'):self.assertEqual(p.read_bytes(),saved[p.parent.name])
   manifest=json.loads((merged/'manifest.json').read_text());self.assertNotIn('trial_ids',manifest)
   original=(merged/'report.json').read_bytes();q=self.command('report',merged);self.assertEqual((merged/'report.json').read_bytes(),original)
   self.assertFalse(json.loads(original)['publishable'])
 def test_invalid_or_reordered_selection_has_no_output(self):
  with tempfile.TemporaryDirectory() as temporary:
   root=pathlib.Path(temporary);test_studies.StudyCommands().inputs(root);path=root/'base.json';ids=[r['id'] for r in schedule(json.loads(path.read_text()))]
   for i,selected in enumerate([['unknown'],[ids[0],ids[0]],[ids[1],ids[0]]]):
    out=root/str(i);q=self.command('run',path,'--output',out,'--trial-ids',*selected)
    self.assertNotEqual(q.returncode,0);self.assertFalse(out.exists())
