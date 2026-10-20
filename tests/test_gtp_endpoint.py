"""Reject incomplete, conflicting, or ambiguous live tunnel state before routing."""
import pathlib,sys,unittest
from adapters.kernel_gtp import tunnels_from_pdml

def message(fields):return '<proto name="ngap">'+''.join('<field name="'+k+'" show="'+v+'"/>' for k,v in fields)+'</proto>'
def request(ue,ip,teid):return message([('ngap.RAN_UE_NGAP_ID',str(ue)),('nas-5gs.sm.pdu_addr_inf_ipv4',ip),('ngap.gTP_TEID',teid),('ngap.gTP_TEID','ff:ff:ff:ff'),('ngap.transportLayerAddress','0a:03:00:02'),('ngap.qosFlowIdentifier','9')])
def response(ue,teid):return message([('ngap.RAN_UE_NGAP_ID',str(ue)),('ngap.gTP_TEID',teid),('ngap.transportLayerAddress','0a:03:00:03')])
class Tunnels(unittest.TestCase):
 def test_bundled_messages_and_distinct_teids(self):
  text='<pdml><packet>'+request(1,'10.45.0.2','00:00:00:02')+request(2,'10.45.0.3','00:00:00:04')+response(2,'00:00:00:09')+response(1,'00:00:00:08')+'</packet></pdml>'
  self.assertEqual(tunnels_from_pdml(text,['10.45.0.2','10.45.0.3']),[{'ran_ue_id':1,'ue_ip':'10.45.0.2','uplink_teid':2,'downlink_teid':8,'peer_ip':'10.3.0.2','qfi':9},{'ran_ue_id':2,'ue_ip':'10.45.0.3','uplink_teid':4,'downlink_teid':9,'peer_ip':'10.3.0.2','qfi':9}])
 def test_missing_tunnel_fails(self):
  with self.assertRaises(ValueError):tunnels_from_pdml('<pdml>'+request(1,'10.45.0.2','00:00:00:02')+'</pdml>',['10.45.0.2'])
 def test_changed_tunnel_fails(self):
  with self.assertRaises(ValueError):tunnels_from_pdml('<pdml>'+request(1,'10.45.0.2','00:00:00:02')+response(1,'00:00:00:08')+response(1,'00:00:00:09')+'</pdml>',['10.45.0.2'])
 def test_unexpected_address_fails(self):
  with self.assertRaises(ValueError):tunnels_from_pdml('<pdml>'+request(1,'10.45.0.2','00:00:00:02')+response(1,'00:00:00:08')+'</pdml>',['10.45.0.3'])

class WireProof(unittest.TestCase):
 def test_each_live_ue_needs_matching_ul_qfi(self):
  from adapters.kernel_gtp import verify_wire_tunnels
  rows=[{'uplink_teid':2,'qfi':9},{'uplink_teid':4,'qfi':1}]
  self.assertEqual(verify_wire_tunnels('0x00000002\t1\t9\n0x00000004\t1\t1\n',rows),{'verified_tunnels':2,'packet_rows':2})
 def test_missing_or_wrong_qfi_fails(self):
  from adapters.kernel_gtp import verify_wire_tunnels
  for text in ['','0x00000002\t1\t8\n','0x00000002\t0\t9\n','0x00000002\t\t\n']:
   with self.assertRaises(ValueError):verify_wire_tunnels(text,[{'uplink_teid':2,'qfi':9}])
