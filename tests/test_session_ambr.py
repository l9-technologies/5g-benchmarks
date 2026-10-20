"""Match decoded NAS session limits before a throughput comparison."""
import pathlib
import sys
import unittest
from adapters.session_ambr import verify,rows_from_pdml

class SessionAmbrChecks(unittest.TestCase):
 def test_equivalent_encodings_match(self):
  self.assertEqual(len(verify('0\t6\t768\t6\t768\n1\t10\t3\t10\t3\n',2,768000000)),2)
 def test_mismatched_or_missing_rate_fails(self):
  for text in ['0\t11\t1\t11\t1\n','0\t0\t1\t0\t1\n','']:
   with self.assertRaises(ValueError):verify(text,1,768000000)
 def test_each_ue_must_be_present(self):
  with self.assertRaises(ValueError):verify('0\t10\t3\t10\t3\n',2,768000000)
 def test_retransmission_does_not_add_a_ue(self):
  text='0\t10\t3\t10\t3\n'*2
  self.assertEqual(len(verify(text,1,768000000)),1)
  with self.assertRaises(ValueError):verify(text,2,768000000)
 def test_bundled_messages_keep_their_ue_identity(self):
  parts=[]
  for ue in [1,2]:
   values={'ngap.RAN_UE_NGAP_ID':str(ue),'nas-5gs.sm.message_type':'0xc2','nas-5gs.sm.unit_for_session_ambr_dl':'10','nas-5gs.sm.session_ambr_dl':'3','nas-5gs.sm.unit_for_session_ambr_ul':'10','nas-5gs.sm.session_ambr_ul':'3'}
   parts.append('<proto name="ngap">'+''.join(f'<field name="{k}" show="{v}" />' for k,v in values.items())+'</proto>')
  xml='<pdml><packet><proto name="ngap"><field name="nas-5gs.mm.message_type" show="0x56" /></proto>'+''.join(parts)+'</packet></pdml>'
  self.assertEqual(len(verify(rows_from_pdml(xml),2,768000000)),2)

if __name__=='__main__':unittest.main()

class NgapRateLimits(unittest.TestCase):
 def test_present_ue_and_session_limits_must_match(self):
  from adapters.session_ambr import verify_ngap_limits
  fields=['ngap.uEAggregateMaximumBitRateDL','ngap.uEAggregateMaximumBitRateUL','ngap.pDUSessionAggregateMaximumBitRateDL','ngap.pDUSessionAggregateMaximumBitRateUL']
  text='<pdml><proto name="ngap">'+''.join(f'<field name="{n}" show="8192000000"/>' for n in fields)+'</proto></pdml>'
  self.assertEqual(len(verify_ngap_limits(text,8192000000)),4)
  with self.assertRaises(ValueError):verify_ngap_limits(text.replace('8192000000','1000000000',1),8192000000)
 def test_absent_optional_ngap_fields_are_recorded_as_absent(self):
  from adapters.session_ambr import verify_ngap_limits
  self.assertEqual(verify_ngap_limits('<pdml><proto name="ngap"/></pdml>',8192000000),[])
