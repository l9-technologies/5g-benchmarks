#!/usr/bin/env python3
"""Measure real traffic during process recovery, session churn, and long runs."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import shutil
import time

from benchmark import check_inputs, digest, host_identity, read, stop_group, write
from native_lab import Lab, UE_ROUTE_TABLE_BASE, command, ping_metrics, procedure_metrics


def start_core(lab, core, count):
    lab.start_core(core,count)


def ping(lab, ip, name, count=10, interval=.1):
    text = lab.ns(lab.ue_ns, ["ping", "-D", "-n", "-I", ip, "-c", str(count), "-i", str(interval), "-W", "2", "192.0.2.1"], check=False)
    (lab.output / f"{name}.log").write_text(text)
    result = ping_metrics(text)
    if result['received'] != count:
        raise ValueError(f"{name}: not all external probes returned")
    return result


def recovery(lab, interfaces, nf, seconds):
    for n, (_, ip) in enumerate(interfaces):
        ping(lab, ip, f"baseline-{n}")
    original = next(p for p, _, name in lab.processes if name == f"core-{nf}")
    radio_pids = {name: p.pid for p, role, name in lab.processes if role in ("ue", "gnb")}
    clients = [lab.start(f"recovery-ping-{n}", ["ping", "-D", "-n", "-I", ip, "-i", ".1", "-w", str(seconds + 10), "-W", "1", "192.0.2.1"], namespace=lab.ue_ns)
               for n, (_, ip) in enumerate(interfaces)]
    time.sleep(2)
    at = time.time()
    os.killpg(original.pid, signal.SIGKILL)
    original.wait(timeout=5)
    restarted = lab.restart(f"core-{nf}")
    event = {"nf": nf, "signal": "SIGKILL", "failed_at": at, "restarted_at": time.time(),
             "old_pid": original.pid, "new_pid": restarted.pid, "radio_pids": radio_pids,
             "radio_restart": False, "deadline_seconds": seconds}
    write(lab.output / 'fault.json', event)
    deadline = time.monotonic() + seconds
    outcomes = {}
    while time.monotonic() < deadline:
        outcomes = {}
        for n in range(len(interfaces)):
            text = (lab.output / f"recovery-ping-{n}.log").read_text()
            replies = [(float(ts), int(seq)) for ts, seq in re.findall(r'\[([0-9.]+)\].*icmp_seq=(\d+).*time=', text)]
            after = [(ts, seq) for ts, seq in replies if ts >= at]
            # Ten successive real replies after the crash are the recovery boundary.
            window = next((after[i:i+10] for i in range(max(0, len(after)-9))
                           if after[i+9][1] - after[i][1] == 9), None)
            if window:
                outcomes[str(n)] = {"first_reply_seconds": window[0][0] - at, "confirmed_seconds": window[-1][0] - at}
        if len(outcomes) == len(interfaces):
            break
        time.sleep(.2)
    for p in clients:
        stop_group(p)
    sockets=lab.ns(lab.core_ns,['ss','-lnp','--tcp','--udp','--sctp'])
    (lab.output/'restart-sockets.txt').write_text(sockets)
    event['new_process_listening']=f'pid={restarted.pid},' in sockets
    event['ues'] = outcomes
    event['recovered'] = len(outcomes) == len(interfaces) and restarted.poll() is None and event['new_process_listening']
    event['radio_alive'] = all(p.poll() is None for p, role, _ in lab.processes if role in ('ue','gnb'))
    if event['recovered']:
        for n, (_, ip) in enumerate(interfaces):
            ping(lab, ip, f"after-recovery-{n}")
    write(lab.output / 'recovery.json', event)
    return event['recovered'] and event['radio_alive']


def route_fresh_tun(lab, target_name='uesimtun0', table=UE_ROUTE_TABLE_BASE+1):
    lab.wait(lambda: 'uesimtun0' in lab.ns(lab.core_ns, ['ip','-o','-4','addr']))
    address = next(a['local'] for r in json.loads(lab.ns(lab.core_ns,['ip','-j','-4','addr'])) if r['ifname']=='uesimtun0' for a in r['addr_info'] if a['family']=='inet')
    if target_name!='uesimtun0':lab.ns(lab.core_ns,['ip','link','set','uesimtun0','name',target_name])
    lab.ns(lab.core_ns,['ip','link','set',target_name,'netns',lab.ue_ns])
    lab.ns(lab.ue_ns,['ip','addr','replace',address+'/32','dev',target_name])
    lab.ns(lab.ue_ns,['ip','link','set',target_name,'up'])
    lab.ns(lab.ue_ns,['ip','rule','add','from',address,'table',str(table)])
    lab.ns(lab.ue_ns,['ip','route','replace','default','dev',target_name,'table',str(table)])
    return address


def fresh_recovery(lab, interfaces, nf, seconds):
    old_ip=interfaces[0][1]
    ping(lab,old_ip,'existing-before')
    original=next(p for p,_,name in lab.processes if name==f'core-{nf or "amf"}')
    radio={name:p.pid for p,role,name in lab.processes if role in ('ue','gnb')}
    event={'nf':nf,'signal':'SIGKILL' if nf else None,'fault_injected':bool(nf),'old_pid':original.pid,'radio_pids':radio,
           'radio_restart':False,'fresh_session_passed':False,'existing_flow_passed':False}
    started=time.monotonic();event['failed_at']=time.time()
    if nf:
        os.killpg(original.pid,signal.SIGKILL);original.wait(timeout=5)
        restarted=lab.restart(f'core-{nf}')
    else:restarted=original
    event['new_pid']=restarted.pid
    write(lab.output/'fresh-recovery.json',event)
    try:
        lab.wait(lambda:f'pid={restarted.pid},' in lab.ns(lab.core_ns,['ss','-lnp','--tcp','--udp','--sctp']),seconds=seconds)
        text=(lab.output/'ue.yaml').read_text()
        before="supi: 'imsi-001010000000001'"
        if before not in text:raise ValueError('fresh UE fixture identity changed')
        path=lab.output/'ue-new.yaml';path.write_text(text.replace(before,"supi: 'imsi-001010000000002'"))
        lab.start('ue-new',[Path(lab.config['ueransim'])/'build/nr-ue','-c',path,'-r','-l'],role='ue')
        remaining=seconds-(time.monotonic()-started)
        if remaining<=0:raise TimeoutError('fresh-session deadline expired')
        lab.wait(lambda:'uesimtun0' in lab.ns(lab.core_ns,['ip','-o','-4','addr']),seconds=remaining)
        address=route_fresh_tun(lab,'freshue0',UE_ROUTE_TABLE_BASE+2);event['fresh_ip']=address;event['existing_ip']=old_ip
        if address==old_ip:raise ValueError('fresh UE address duplicates the existing UE address')
        text=(lab.output/'ue-new.log').read_text()
        event['procedure_metrics']=procedure_metrics(text,1)
        if 'Selected integrity[2] ciphering[2]' not in text:raise ValueError('fresh UE has no matched NAS security event')
        event['fresh_ping']=ping(lab,address,'fresh-external')
        event['fresh_ping_complete_seconds']=time.monotonic()-started
        for n,reverse in enumerate((False,True)):
            lab.start(f'fresh-server-{n}',['iperf3','-s','-B','192.0.2.1','-p',str(6201+n)],namespace=lab.dn_ns)
            lab.wait(lambda:str(6201+n) in lab.ns(lab.dn_ns,['ss','-lntH']))
            args=['iperf3','-c','192.0.2.1','-B',address,'-p',str(6201+n),'-u','-b','1M','-l','1200','-t','3','-J','--get-server-output']
            if reverse:args.append('-R')
            data=json.loads(lab.ns(lab.ue_ns,args));write(lab.output/f'fresh-traffic-{n}.json',data)
            received=data['end']['sum_received']
            if received['bytes']<=0 or received['lost_percent']>1:raise ValueError('fresh external receiver traffic failed')
        event['fresh_session_passed']=True
        event['bidirectional_confirmation_seconds']=time.monotonic()-started
    except Exception as error:event['fresh_error']=str(error)
    text=lab.ns(lab.ue_ns,['ping','-D','-n','-I',old_ip,'-c','10','-i','.1','-W','1','192.0.2.1'],check=False)
    (lab.output/'existing-after.log').write_text(text)
    event['existing_ping']=ping_metrics(text);event['existing_flow_passed']=event['existing_ping']['received']==10
    event['original_radio_alive']=all(p.poll() is None for p,_,name in lab.processes if name in radio)
    event['new_process_alive']=restarted.poll() is None
    write(lab.output/'fresh-recovery.json',event)
    return event['fresh_session_passed'] and event['original_radio_alive'] and event['new_process_alive']


def churn(lab, interfaces, cycles):
    binary = Path(lab.config['ueransim']) / 'build/nr-cli'
    ip = interfaces[0][1]
    rows = []
    def cli(label, text):
        reply = lab.ns(lab.core_ns,[str(binary),'imsi-001010000000001','--exec',text])
        (lab.output/f'{label}.txt').write_text(reply)
    for i in range(cycles):
        ping(lab,ip,f'cycle-{i}-before',3)
        sessions=re.findall(r'PDU Session establishment is successful PSI\[(\d+)\]',(lab.output/'ue.log').read_text())
        if not sessions:raise ValueError('active session ID has no protocol event')
        session_id=int(sessions[-1])
        start=time.monotonic()
        cli(f'cycle-{i}-release',f'ps-release {session_id}')
        lab.wait(lambda:'uesimtun0' not in lab.ns(lab.ue_ns,['ip','-o','link']))
        release_ms=(time.monotonic()-start)*1000
        lab.ns(lab.ue_ns,['ip','rule','del','from',ip,'table',str(UE_ROUTE_TABLE_BASE+1)])
        start=time.monotonic()
        cli(f'cycle-{i}-establish','ps-establish IPv4 --sst 1 --sd 66051 --dnn internet')
        ip=route_fresh_tun(lab)
        ping(lab,ip,f'cycle-{i}-after',3)
        rows.append({'cycle':i,'released_session_id':session_id,'release_ready_ms':release_ms,'session_traffic_ready_ms':(time.monotonic()-start)*1000,'ip':ip})
        write(lab.output/'churn.json',{'cycles':rows,'requested_cycles':cycles,'complete':False})
    cli('deregister','deregister normal')
    lab.wait(lambda:'De-registration is successful' in (lab.output/'ue.log').read_text())
    lab.wait(lambda:'uesimtun0' not in lab.ns(lab.ue_ns,['ip','-o','link']))
    write(lab.output/'churn.json',{'cycles':rows,'requested_cycles':cycles,'complete':True,'deregistration':True})
    return True


def soak(lab, interfaces, seconds, mbps):
    lab.monitor_thread = __import__('threading').Thread(target=lab.monitor)
    lab.monitor_thread.start()
    started=time.monotonic()
    clients=[];probes=[]
    for n,(_,ip) in enumerate(interfaces):
        lab.start(f'server-{n}',['iperf3','-s','-B','192.0.2.1','-p',str(5201+n)],namespace=lab.dn_ns)
        lab.wait(lambda:str(5201+n) in lab.ns(lab.dn_ns,['ss','-lntH']))
        clients.append(lab.start(f'soak-{n}',['iperf3','-c','192.0.2.1','-B',ip,'-p',str(5201+n),'-u','-b',f'{mbps}M','-l','1200','-t',str(seconds),'-i','60','-J','--get-server-output'],namespace=lab.ue_ns))
        probes.append(lab.start(f'soak-ping-{n}',['ping','-D','-n','-I',ip,'-i','1','-w',str(seconds),'-W','2','192.0.2.1'],namespace=lab.ue_ns))
    failures=[]
    for p in clients:
        if p.wait(timeout=seconds+30):failures.append(f'traffic process {p.pid} failed')
    for p in probes:p.wait(timeout=10)
    elapsed=time.monotonic()-started
    lab.monitor_stop.set();lab.monitor_thread.join()
    write(lab.output/'resources.json',lab.samples)
    rows=[]
    for n in range(len(interfaces)):
        try:
            data=read(lab.output/f'soak-{n}.log')
            received=data['end']['sum_received']
            probes=ping_metrics((lab.output/f'soak-ping-{n}.log').read_text())
            rows.append({'ue':n,'receiver':received,'ping':probes})
            if received['seconds'] < seconds*.99:failures.append(f'UE {n}: measured duration too short')
        except (ValueError,KeyError) as error:failures.append(str(error))
    if any('error' in r for r in lab.samples):failures.append('resource process disappeared')
    roles={role:{'rss_start_bytes':lab.samples[0]['roles'][role]['rss'],
                 'rss_end_bytes':lab.samples[-1]['roles'][role]['rss'],
                 'rss_peak_bytes':max(r['roles'][role]['rss'] for r in lab.samples if 'roles' in r),
                 'cpu_seconds':lab.samples[-1]['roles'][role]['cpu']-lab.samples[0]['roles'][role]['cpu']}
           for role in ('core','ue','gnb') if lab.samples and 'roles' in lab.samples[0] and 'roles' in lab.samples[-1]}
    write(lab.output/'soak.json',{'requested_seconds':seconds,'elapsed_seconds':elapsed,'ues':rows,'resources':roles,'failures':failures})
    return not failures


def rejected(lab, core, variant):
    source=Path(lab.config['radio_configs']);target=lab.output/'variant';target.mkdir()
    shutil.copyfile(source/'gnb.yaml',target/'gnb.yaml')
    text=(source/'ue.yaml').read_text()
    changes={'bad-key':("key: '00112233445566778899aabbccddeeff'", "key: '10112233445566778899aabbccddeeff'"),
             'unknown-subscriber':("supi: 'imsi-001010000000001'", "supi: 'imsi-001010000000099'"),
             'bad-dnn':("apn: 'internet'", "apn: 'not-allowed.example'"),
             'bad-slice':('sd: 66051','sd: 66052')}
    before,after=changes[variant]
    if before not in text:raise ValueError('variant input no longer matches public fixture')
    (target/'ue.yaml').write_text(text.replace(before,after))
    lab.config['radio_configs']=str(target)
    unexpected=False
    try:
        lab.radio('ueransim',1,core);unexpected=True
    except TimeoutError:
        pass
    text=(lab.output/'ue.log').read_text()
    markers=('MAC mismatch','MAC validation failed','Registration is rejected','Initial Registration failed [','Authentication Reject',
             'PDU Session establishment is rejected','PDU Session Establishment Reject','5GMM cause','5GSM cause',
             'MM status received with cause [DNN_NOT_SUPPORTED_OR_NOT_SUBSCRIBED]',
             'SM forwarding failure for message type[193] with cause[DNN_NOT_SUPPORTED_OR_NOT_SUBSCRIBED]')
    observed=[m for m in markers if m.lower() in text.lower()]
    passed=not unexpected and bool(observed) and all(p.poll() is None for p,role,_ in lab.processes if role)
    write(lab.output/'rejection.json',{'variant':variant,'explicit_events':observed,'unexpected_session':unexpected,'passed':passed})
    return passed


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--core',required=True)
    parser.add_argument('--radio',default='ueransim')
    parser.add_argument('--case',choices=('recovery','fresh-recovery','fresh-session','churn','soak','reject'),required=True)
    parser.add_argument('--variant',choices=('bad-key','unknown-subscriber','bad-dnn','bad-slice'),default='bad-key')
    parser.add_argument('--nf',choices=('amf','smf','upf'),default='upf')
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--cycles',type=int,default=20)
    parser.add_argument('--ue-count',type=int,default=1)
    parser.add_argument('--mbps',type=float,default=1)
    args=parser.parse_args()
    if not 10<=args.seconds<=86400 or not 1<=args.cycles<=1000 or not 1<=args.ue_count<=253 or not .01<=args.mbps<=1000:
        parser.error('parameters out of bounds')
    if args.case in ('churn','fresh-recovery','fresh-session') and (args.radio!='ueransim' or args.ue_count!=1):parser.error('this case requires one initial UERANSIM UE')
    def interrupted(signum,frame):raise InterruptedError(f'signal {signum}')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    plan=read(args.plan);check_inputs(plan)
    args.output.mkdir(parents=True,exist_ok=False)
    config=read(Path(plan['driver'][-1]));config['resource_sample_seconds']=1 if args.case=='soak' else .1
    config['enable_radio_commands']=args.case=='churn'
    lab=Lab(config,args.output)
    receipt={'case':args.case,'core':args.core,'radio':args.radio,'host':host_identity(),'plan_sha256':digest(args.plan),
             'runner_sha256':digest(__file__),'adapter_sha256':digest(Path(__file__).with_name('native_lab.py')),'parameters':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},'passed':False}
    write(args.output/'plan.json',plan)
    with Path('/var/lock/5g-benchmark.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            lab.setup();start_core(lab,args.core,2 if args.case in ('fresh-recovery','fresh-session') else args.ue_count)
            capture=lab.start('capture',['tcpdump','-i','lo','-s','0','-c','4000','-U','-w',args.output/'n2-n3.pcap','sctp or udp port 2152'])
            lab.wait(lambda:'listening on' in (args.output/'capture.log').read_text())
            if args.case=='reject':
                receipt['passed']=rejected(lab,args.core,args.variant)
                interfaces=[]
            else:
                _,interfaces=lab.radio(args.radio,args.ue_count,args.core)
            if args.case=='recovery':receipt['passed']=recovery(lab,interfaces,args.nf,args.seconds)
            elif args.case=='fresh-recovery':receipt['passed']=fresh_recovery(lab,interfaces,args.nf,args.seconds)
            elif args.case=='fresh-session':receipt['passed']=fresh_recovery(lab,interfaces,None,args.seconds)
            elif args.case=='churn':receipt['passed']=churn(lab,interfaces,args.cycles)
            elif args.case=='soak':receipt['passed']=soak(lab,interfaces,args.seconds,args.mbps)
            check_inputs(plan)
        except Exception as error:receipt['error']=str(error)
        finally:
            signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN)
            receipt['cleanup_ok']=lab.cleanup()
            receipt['passed'] &= receipt['cleanup_ok']
            if (args.output/'n2-n3.pcap').exists():
                decoded=command(['tshark','-r',args.output/'n2-n3.pcap','-Y','ngap or gtp','-T','fields','-e','frame.time_epoch','-e','ngap.procedureCode','-e','gtp.teid'],check=False)
                (args.output/'decoded.tsv').write_text(decoded)
            receipt['hashes']={str(p.relative_to(args.output)):digest(p) for p in args.output.rglob('*') if p.is_file()}
            write(args.output/'experiment.json',receipt)
    print(json.dumps(receipt,indent=2))
    return 0 if receipt['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
