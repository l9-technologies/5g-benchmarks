"""Use live NAS/NGAP tunnel state for a Linux GTP-U traffic endpoint."""
import xml.etree.ElementTree as ET
import ipaddress,json,pathlib,sys,time,signal,socket

def tunnels_from_pdml(text,addresses):
 sessions={}
 for protocol in ET.fromstring(text).iter('proto'):
  if protocol.get('name')!='ngap':continue
  values={}
  for f in protocol.iter('field'):values.setdefault(f.get('name'),[]).append(f.get('show'))
  teids=values.get('ngap.gTP_TEID',[])
  if not teids:continue
  ids=values.get('ngap.RAN_UE_NGAP_ID',[])
  if len(ids)!=1:raise ValueError('Ambiguous NGAP UE identity')
  uid=int(ids[0]);row=sessions.setdefault(uid,{'ran_ue_id':uid})
  peers=values.get('ngap.transportLayerAddress',[])
  if not peers:raise ValueError('NGAP tunnel address missing')
  peer=str(ipaddress.IPv4Address(bytes.fromhex(peers[0].replace(':',''))))
  if peer=='10.3.0.2':
   ips=values.get('nas-5gs.sm.pdu_addr_inf_ipv4',[])
   if len(ips)!=1:raise ValueError('Live NAS UE address missing or ambiguous')
   qfis=values.get('ngap.qosFlowIdentifier',[])
   if len(qfis)!=1 or not 1<=int(qfis[0])<=63:raise ValueError('Single live QoS flow missing or invalid')
   update={'qfi':int(qfis[0]),'ue_ip':str(ipaddress.IPv4Address(ips[0])),'uplink_teid':int(teids[0].replace(':',''),16),'peer_ip':peer}
  elif peer=='10.3.0.3':
   if len(teids)!=1:raise ValueError('Ambiguous downlink tunnel')
   update={'downlink_teid':int(teids[0].replace(':',''),16)}
  else:raise ValueError('Unexpected live GTP peer')
  if any(k in row and row[k]!=v for k,v in update.items()):raise ValueError('Live tunnel state changed')
  row.update(update)
 rows=sorted(sessions.values(),key=lambda r:r['ran_ue_id'])
 if len(rows)!=len(addresses) or any(set(row)!={'ran_ue_id','ue_ip','uplink_teid','downlink_teid','peer_ip','qfi'} for row in rows):raise ValueError('Live tunnel state incomplete')
 if sorted(r['ue_ip'] for r in rows)!=sorted(addresses) or len(set(addresses))!=len(addresses):raise ValueError('Live tunnel addresses do not match all UE interfaces')
 if len({r['downlink_teid'] for r in rows})!=len(rows) or any(not 0<r[k]<=0xffffffff for r in rows for k in ['uplink_teid','downlink_teid']):raise ValueError('Invalid live TEID allocation')
 return rows

def verify_wire_tunnels(text,tunnels):
 expected={r['uplink_teid']:r['qfi'] for r in tunnels};seen=set();count=0
 for line in text.splitlines():
  fields=line.split('\t')
  try:
   teid=int(fields[0],0);direction=int(fields[1]);qfi=int(fields[2])
  except (ValueError,IndexError) as error:raise ValueError('Incomplete N3 PDU Session Container') from error
  if teid not in expected or direction!=1 or qfi!=expected[teid]:raise ValueError('N3 QFI differs from live NGAP session')
  seen.add(teid);count+=1
 if seen!=set(expected):raise ValueError('N3 QFI not verified for every UE')
 return {'verified_tunnels':len(seen),'packet_rows':count}

def serve(config_path):
 from pyroute2 import IPRoute
 from pyroute2.netlink import genlmsg,NLM_F_REQUEST,NLM_F_ACK,NLM_F_CREATE,NLM_F_EXCL,NLM_F_DUMP
 from pyroute2.netlink.generic import GenericNetlinkSocket
 from pyroute2.netlink.rtnl.ifinfmsg.plugins.gtp import gtp
 # Ubuntu's link decoder predates IFLA_GTP_ROLE; use the installed kernel ABI.
 gtp.nla_map=gtp.nla_map+(('IFLA_GTP_ROLE','uint32'),)
 class Message(genlmsg):
  nla_map=(('GTPA_UNSPEC','none'),('GTPA_LINK','uint32'),('GTPA_VERSION','uint32'),('GTPA_TID','uint64'),('GTPA_PEER_ADDRESS','ip4addr'),('GTPA_MS_ADDRESS','ip4addr'),('GTPA_FLOW','uint16'),('GTPA_NET_NS_FD','uint32'),('GTPA_I_TEI','uint32'),('GTPA_O_TEI','uint32'))
 config=json.loads(pathlib.Path(config_path).read_text());sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.bind(('10.3.0.3',32152))
 ipr=IPRoute();ipr.link('add',ifname='benchgtp',kind='gtp',gtp_fd1=sock.fileno(),gtp_pdp_hashsize=1024,gtp_role=1);index=ipr.link_lookup(ifname='benchgtp')[0]
 ipr.link('set',index=index,state='up',mtu=1400)
 nl=GenericNetlinkSocket();nl.bind('gtp',Message)
 for row in config:
  m=Message();m['cmd']=0;m['version']=0;m['attrs']=[('GTPA_LINK',index),('GTPA_VERSION',1),('GTPA_PEER_ADDRESS',row['peer_ip']),('GTPA_MS_ADDRESS',row['ue_ip']),('GTPA_I_TEI',row['downlink_teid']),('GTPA_O_TEI',row['uplink_teid']),('GTPA_FLOW',row['qfi'])]
  nl.nlm_request(m,msg_type=nl.prid,msg_flags=NLM_F_REQUEST|NLM_F_ACK|NLM_F_CREATE|NLM_F_EXCL)
 m=Message();m['cmd']=2;m['version']=0;m['attrs']=[('GTPA_LINK',index)]
 contexts=nl.nlm_request(m,msg_type=nl.prid,msg_flags=NLM_F_REQUEST|NLM_F_DUMP)
 actual={(x.get_attr('GTPA_I_TEI'),x.get_attr('GTPA_O_TEI'),x.get_attr('GTPA_MS_ADDRESS'),x.get_attr('GTPA_PEER_ADDRESS')) for x in contexts}
 expected={(x['downlink_teid'],x['uplink_teid'],x['ue_ip'],x['peer_ip']) for x in config}
 assert actual==expected,'Kernel PDP context readback differs from live NGAP'
 link=ipr.get_links(index)[0];role=link.get_attr('IFLA_LINKINFO').get_attr('IFLA_INFO_DATA').get_attr('IFLA_GTP_ROLE');assert role==1,'Kernel GTP role differs'
 pathlib.Path(config_path).with_name('kernel-gtp-context-readback.json').write_text(json.dumps({'passed':True,'role':'SGSN','contexts':[list(x) for x in sorted(actual)]},indent=2)+'\n')
 ipr.route('add',dst='192.0.2.1/32',oif=index)
 print('GTP_ENDPOINT_READY',flush=True)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));signal.signal(signal.SIGINT,lambda *_:sys.exit(0))
 try:
  while True:time.sleep(1)
 finally:sock.close();nl.close();ipr.close()
if __name__=='__main__':serve(sys.argv[1])
