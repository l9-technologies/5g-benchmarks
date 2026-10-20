"""Check tshark NAS Session-AMBR fields (TS 24.501, 9.11.4.14)."""
import xml.etree.ElementTree as ET
UNITS=(1000,4000,16000,64000,256000,1000000,4000000,16000000,64000000,
       256000000,1000000000,4000000000,16000000000,64000000000,
       256000000000,1000000000000,4000000000000)

def rows_from_pdml(text):
 rows=[]
 names=['ngap.RAN_UE_NGAP_ID','nas-5gs.sm.unit_for_session_ambr_dl',
        'nas-5gs.sm.session_ambr_dl','nas-5gs.sm.unit_for_session_ambr_ul',
        'nas-5gs.sm.session_ambr_ul']
 for protocol in ET.fromstring(text).iter('proto'):
  if protocol.get('name')!='ngap':continue
  fields=list(protocol.iter('field'))
  if not any(f.get('name')=='nas-5gs.sm.message_type' and int(f.get('show','0'),0)==0xc2 for f in fields):continue
  values=[]
  for name in names:
   found=[f.get('show') for f in fields if f.get('name')==name]
   if len(found)!=1 or found[0] is None:raise ValueError('Ambiguous NAS Session-AMBR fields within one NGAP message')
   values.append(found[0])
  rows.append('\t'.join(values))
 return '\n'.join(rows)

def verify(text,count,expected_bps):
 result={}
 for line in text.splitlines():
  if not line.strip():continue
  try:
   ue,dl_unit,dl,ul_unit,ul=map(int,line.split('\t'))
   if not (1<=dl_unit<=len(UNITS) and 1<=ul_unit<=len(UNITS)):raise ValueError('Reserved AMBR unit')
   rates={'downlink_bps':UNITS[dl_unit-1]*dl,'uplink_bps':UNITS[ul_unit-1]*ul}
  except (ValueError,IndexError) as error:raise ValueError('Session-AMBR fields are incomplete or invalid') from error
  if rates!={'downlink_bps':expected_bps,'uplink_bps':expected_bps}:raise ValueError('Session-AMBR differs: '+str(rates))
  if ue in result and result[ue]!=rates:raise ValueError('UE session limit changed')
  result[ue]=rates
 if len(result)!=count:raise ValueError('Session-AMBR not verified for each distinct UE')
 return result

def verify_ngap_limits(text,expected_bps):
 names={'ngap.'+kind+'AggregateMaximumBitRate'+direction for kind in ['uE','pDUSession'] for direction in ['DL','UL']}
 rows=[]
 for protocol in ET.fromstring(text).iter('proto'):
  if protocol.get('name')!='ngap':continue
  for field in protocol.iter('field'):
   if field.get('name') not in names:continue
   value=int(field.get('show','-1'))
   if value!=expected_bps:raise ValueError('NGAP rate limit differs: '+field.get('name')+'='+str(value))
   rows.append({'field':field.get('name'),'bits_per_second':value})
 return rows
