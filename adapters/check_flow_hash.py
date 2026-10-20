"""Run the compiled packet classifier against both directions and bad lengths."""
import pathlib,subprocess,json,struct,tempfile,ipaddress,sys,hashlib
count=int(sys.argv[1]) if len(sys.argv)>1 else 8;assert count in [1,2,4,8,16]
T=pathlib.Path(sys.argv[2]);source=pathlib.Path(sys.argv[3]);pin='/sys/fs/bpf/benchmark_hash_'+str(__import__('os').getpid())
subprocess.run(['bpftool','prog','load',str(T/f'flow_hash-{count}-check.o'),pin,'type','classifier'],check=True)
def ip(source,dest,payload,proto):
 return struct.pack('!BBHHHBBH4s4s',0x45,0,len(payload)+20,1,0,64,proto,0,ipaddress.ip_address(source).packed,ipaddress.ip_address(dest).packed)+payload
eth=bytes.fromhex('0200000000010200000000020800')
def packet(ue,down=False,tunnel=False):
 body=ip('192.0.2.1' if down else ue,ue if down else '192.0.2.1',b'\x14\x51\xa0\x00'+b'\0'*16,6)
 if tunnel:
  gtp=struct.pack('!BBHI',0x34,255,len(body)+8,17)+bytes.fromhex('0000008501100100')+body
  body=ip('10.3.0.2' if down else '10.3.0.3','10.3.0.3' if down else '10.3.0.2',struct.pack('!HHHH',2152,2152,len(gtp)+8,0)+gtp,17)
 return eth+body
def nat_packet(number,down=False,proto=6,server='192.0.2.1'):
 sport,dport=(5200+number,5207) if down else (5207,5200+number)
 l4=struct.pack('!HHIIBBHHH',sport,dport,0,0,0x50,0x10,65535,0,0) if proto==6 else struct.pack('!HHHH',sport,dport,8,0)
 return eth+ip(server if down else '192.0.2.2','192.0.2.2' if down else server,l4,proto)
rows=[]
try:
 with tempfile.TemporaryDirectory() as temp:
  d=pathlib.Path(temp)
  def run(data):
   (d/'in').write_bytes(data);(d/'ctx-in').write_bytes(bytes(192));p=subprocess.run(['bpftool','-j','prog','run','pinned',pin,'data_in',str(d/'in'),'data_out',str(d/'out'),'ctx_in',str(d/'ctx-in'),'ctx_out',str(d/'ctx')],check=True,capture_output=True,text=True)
   assert (d/'out').read_bytes()==data,'Classifier changed packet bytes'
   assert json.loads(p.stdout)['retval']==0,'Classifier rejected packet'
   return struct.unpack_from('=I',(d/'ctx').read_bytes(),48)[0]
  for number in range(1,12):
   ue=f'10.45.0.{number}';expected=((2*(int(ipaddress.ip_address(ue))%count)+1)*0x80000000)//count
   for down in [False,True]:
    for tunnel in [False,True]:
     actual=run(packet(ue,down,tunnel));assert actual==expected,(ue,down,tunnel,actual,expected);rows.append({'ue':ue,'downlink':down,'gtp':tunnel,'hash':actual})
  for number in range(1,33):
   expected=((2*(number%count)+1)*0x80000000)//count
   for down in [False,True]:
    for proto in [6,17]:
     actual=run(nat_packet(number,down,proto));assert actual==expected,('NAT',number,down,proto,actual,expected);rows.append({'nat_server_index':number,'downlink':down,'protocol':proto,'hash':actual})
  bad_nat=[nat_packet(0),nat_packet(201),nat_packet(1,server='203.0.113.1'),nat_packet(1)[:34],nat_packet(1)[:35],nat_packet(1)[:37],nat_packet(1,proto=1)]
  for bad in bad_nat:assert run(bad)==0
  assert run(packet('10.45.0.1',False,True)[:54])==0
  for index,value in [(14,0x65),(54,0),(57,0x40)]:
   bad=bytearray(packet('10.45.0.1',False,True));bad[index]=value;assert run(bad)==0
 result={'source_sha256':hashlib.file_digest((source).open('rb'),'sha256').hexdigest(),'runtime_object_sha256':hashlib.file_digest((T/f'flow_hash-{count}.o').open('rb'),'sha256').hexdigest(),'check_object_sha256':hashlib.file_digest((T/f'flow_hash-{count}-check.o').open('rb'),'sha256').hexdigest(),'passed':True,'receive_cpus':count,'cases':len(rows)+11,'checked':'Both directions, plain IPv4/N3 GTP/NAT TCP and UDP, server-side ports despite overlapping client ports, unchanged packet bytes, unsupported peers/ports/protocols and malformed lengths','check_source_sha256':hashlib.file_digest(pathlib.Path(__file__).open('rb'),'sha256').hexdigest(),'rows':rows};(T/f'flow-hash-{count}-check.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'passed':True,'receive_cpus':count,'cases':len(rows)+11}))
finally:pathlib.Path(pin).unlink(missing_ok=True)
