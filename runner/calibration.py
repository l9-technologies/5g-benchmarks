"""Direct tool calibration on the native lab path. Never a core comparison."""
import fcntl
import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import time
from .contracts import number,packet_sizes
from .evidence import digest,read,write

ROOT=Path(__file__).resolve().parents[1]

def validate(plan):
    keys={'schema_version','rates_mbps','packet_bytes','streams','duration_seconds','warmup_seconds',
          'generator_cpus','observer_cpus','loss_percent','headroom_multiple'}
    if not isinstance(plan,dict) or not keys<=plan.keys() or set(plan)-keys-{'receive_steering','packet_patterns','iperf_bin','iperf_build_receipt'} or plan['schema_version']!=1:raise ValueError('invalid calibration schema')
    try:
        for field in ['iperf_bin','iperf_build_receipt']:
            if field in plan and (not isinstance(plan[field],str) or not Path(plan[field]).is_absolute()):raise ValueError(field+' must be an absolute path')
        if 'iperf_build_receipt' in plan and 'iperf_bin' not in plan:raise ValueError('select the receipt binary explicitly')
        if type(plan.get('receive_steering',True)) is not bool:raise ValueError('receive_steering must be boolean')
        for key,low,high in [('streams',1,100),('duration_seconds',2,300),('warmup_seconds',1,30),('loss_percent',0,5),('headroom_multiple',2,10)]:number(plan[key],key,low,high)
        for key in ['streams','duration_seconds','warmup_seconds']:
            if type(plan[key]) is not int:raise ValueError(key+' must be an integer')
        for key,low,high in [('rates_mbps',.01,10000),('packet_bytes',64,1400),('generator_cpus',0,1023),('observer_cpus',0,1023)]:
            values=plan[key]
            if not isinstance(values,list) or (not values and not (key=='packet_bytes' and plan.get('packet_patterns'))) or len(set(values))!=len(values):raise ValueError(key+' must have distinct values')
            for value in values:
                number(value,key,low,high)
                if key!='rates_mbps' and type(value) is not int:raise ValueError(key+' must use integers')
        if not isinstance(plan.get('packet_patterns',[]),list):raise ValueError('packet_patterns must be a list')
        for pattern in plan.get('packet_patterns',[]):packet_sizes({'packet_bytes_by_stream':pattern,'traffic_modes':['udp_ul','udp_dl']},plan['streams'])
        if set(plan['generator_cpus'])&set(plan['observer_cpus']):raise ValueError('CPU roles overlap')
    except (ValueError,TypeError) as error:raise ValueError('invalid calibration profile: '+str(error)) from error
    return plan

def patterns(plan):
    return [(str(size),[size]*plan['streams']) for size in plan['packet_bytes']]+[('mixed-'+str(i),sizes) for i,sizes in enumerate(plan.get('packet_patterns',[]))]

def run(profile,output):
    plan=validate(read(profile))
    if output.exists() or output.with_name(output.name+'.tgz').exists() or output.with_name(output.name+'.archive.json').exists():raise ValueError('calibration output or archive already exists')
    if sys.platform!='linux' or os.geteuid()!=0:raise ValueError('calibration requires root on Linux')
    from native_lab import Lab,generator_headroom,udp_counters
    from adapters.common import command
    from adapters import steering,probe_window
    binary=Path(plan.get('iperf_bin') or __import__('shutil').which('iperf3') or '')
    if not binary.is_file() or not os.access(binary,os.X_OK):raise ValueError('install the calibration traffic generator')
    contract=probe_window.generator_contract(binary,plan.get('iperf_build_receipt'))
    available=os.sched_getaffinity(0)
    required=set(plan['generator_cpus']+plan['observer_cpus'])
    if not required<=available:raise ValueError('calibration CPU roles are outside the effective service allocation')
    with Path('/var/lock/5g-benchmark.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if command(['ip','netns','list']).strip():raise ValueError('foreign namespaces block calibration')
        output.mkdir(parents=True,exist_ok=False);write(output/'profile.json',plan)
        from bundle import export
        with contextlib.redirect_stdout(io.StringIO()):export(output/'source')
        write(output/'source.json',{str(p.relative_to(ROOT)):digest(p) for p in sorted(ROOT.rglob('*')) if p.is_file() and p.suffix in ('.py','.c') and '.git' not in p.parts and '__pycache__' not in p.parts})
        write(output/'host.json',{'available_cpus':sorted(available),'iperf_version':command([binary,'--version']),
            'iperf_sha256':digest(binary),'probe_window_contract':contract,'kernel':command(['uname','-a']),
            'memory':Path('/proc/meminfo').read_text()})
        result={'kind':'direct-tool-calibration','comparison_admitted':False,'scope':'Linux veth; no core or radio',
                'started_epoch':time.time(),'expected_points':len(plan['rates_mbps'])*len(patterns(plan)),
                'cases':[],'cleanup_ok':True,'status':'running','host_isolation':'not established; diagnostic only'}
        write(output/'calibration.json',result)
        objects=None
        lab=None
        def interrupt(signum,frame):
            if lab is not None:lab.interrupt(signum)
            else:raise InterruptedError('calibration interrupted')
        previous={s:signal.signal(s,interrupt) for s in (signal.SIGINT,signal.SIGTERM)}
        original_affinity=os.sched_getaffinity(0);os.sched_setaffinity(0,plan['observer_cpus'])
        try:
            if plan.get('receive_steering',True):
                prepared=output/'prepared';prepared.mkdir()
                objects=steering.prepare(prepared,[len(plan['generator_cpus'])])
            for label,sizes in patterns(plan):
                for rate in plan['rates_mbps']:
                    folder=output/('udp-'+label+'-'+str(rate));folder.mkdir()
                    config={'candidate':'calibration','extension':None,'core_cpus':plan['generator_cpus'],
                            'generator_cpus':plan['generator_cpus'],'observer_cpus':plan['observer_cpus'],
                            'maximum_host_busy_percent':80,'flow_hash_objects':objects,'iperf3':str(binary),'probe_window_contract':contract}
                    lab=Lab(config,folder);point={'packet_pattern':label,'payload_bytes':sizes[0] if len(set(sizes))==1 else None,'payload_bytes_by_stream':sizes,'offered_mbps':rate,'passed':False,'errors':[]}
                    try:
                        lab.setup();lab.ue_ns=lab.core_ns
                        for ns,device in [(lab.core_ns,'dn0'),(lab.dn_ns,'dn1')]:
                            lab.ns(ns,['ethtool','-K',device,'rx','off','tx','off','tso','off','gso','off','gro','off'])
                            (folder/(device+'-offloads.txt')).write_text(lab.ns(ns,['ethtool','-k',device]))
                        if objects:
                            proof=[steering.apply(lab,ns,device,plan['generator_cpus'],len(plan['generator_cpus'])) for ns,device in [(lab.core_ns,'dn0'),(lab.dn_ns,'dn1')]]
                            write(folder/'receive-steering.json',proof)
                        interfaces=[]
                        for n in range(plan['streams']):
                            ip='10.45.0.'+str(n+1);lab.ns(lab.core_ns,['ip','addr','add',ip+'/24','dev','dn0']);interfaces.append(('dn0',ip))
                        before={ns:lab.ns(ns,['cat','/proc/net/snmp']) for ns in lab.namespaces};write(folder/'udp-before.json',before)
                        protocol={'duration_seconds':plan['duration_seconds'],'warmup_seconds':plan['warmup_seconds'],
                            'packet_bytes':max(sizes),'packet_bytes_by_stream':sizes,'offered_mbps_per_ue':rate/plan['streams'],'traffic_modes':['udp_ul','udp_dl']}
                        lab.startup_started=time.monotonic()
                        metrics,quality,missing=lab.traffic(interfaces,protocol)
                        after={ns:lab.ns(ns,['cat','/proc/net/snmp']) for ns in lab.namespaces};write(folder/'udp-after.json',after)
                        deltas={ns:{key:udp_counters(after[ns])[key]-udp_counters(before[ns])[key] for key in ['InErrors','RcvbufErrors','SndbufErrors']} for ns in before}
                        headroom=generator_headroom(lab.samples,'traffic',len(plan['generator_cpus']),plan['generator_cpus'])
                        rows={}
                        for mode in protocol['traffic_modes']:
                            counts=[]
                            for n in range(plan['streams']):
                                raw=read(folder/(mode+'-'+str(n)+'.log'))['end'];received=raw['sum_received'];sent=raw['sum_sent']
                                counts.append({'sent_mbps':sent['bits_per_second']/1e6,'received_mbps':received['bits_per_second']/1e6,
                                    'received_pps':(received['packets']-received['lost_packets'])/received['seconds'],
                                    'receiver_packets':received['packets'],'lost_packets':received['lost_packets']})
                            sender=sum(c['sent_mbps'] for c in counts);receiver=sum(c['received_mbps'] for c in counts)
                            rows[mode]={'sender_mbps':sender,'receiver_mbps':receiver,'receiver_pps':sum(c['received_pps'] for c in counts),
                                'loss_percent':metrics[mode+'_loss_percent'],'load_matched':abs(sender-rate)<=.05*rate and abs(receiver-rate)<=.05*rate,
                                'streams':counts}
                        point.update(directions=rows,quality_errors=quality,metric_errors=missing,headroom=headroom,socket_deltas=deltas,
                            metrics=metrics,metric_notes=lab.metric_notes)
                        point['passed']=not quality and not missing and headroom['passed'] and all(v==0 for delta in deltas.values() for v in delta.values()) and all(r['load_matched'] and r['loss_percent']<=plan['loss_percent'] for r in rows.values())
                    except Exception as error:point['errors'].append(repr(error))
                    finally:
                        try:point['cleanup_ok']=lab.cleanup()
                        except Exception as error:point['cleanup_ok']=False;point['errors'].append('cleanup: '+repr(error))
                        lab=None
                        point['passed']=point['passed'] and point['cleanup_ok']
                        result['cleanup_ok']=result['cleanup_ok'] and point['cleanup_ok']
                        write(folder/'calibration.json',point);result['cases'].append(point);write(output/'calibration.json',result)
                    if not point['cleanup_ok'] or point['errors']:raise ValueError('calibration stopped after a tool or cleanup error; preserve this output')
            result['status']='completed_diagnostic'
        except BaseException as error:
            result.update(status='stopped',error=repr(error));raise
        finally:
            os.sched_setaffinity(0,original_affinity)
            for s,handler in previous.items():signal.signal(s,handler)
            result.update(finished_epoch=time.time(),passed_points=sum(c['passed'] for c in result['cases']))
            result['tool_supported_core_rates_mbps']={label:[c['offered_mbps']/plan['headroom_multiple'] for c in result['cases'] if c['packet_pattern']==label and c['passed']] for label,_ in patterns(plan)}
            write(output/'calibration.json',result)
            write(output/'evidence.json',{str(p.relative_to(output)):digest(p) for p in output.rglob('*') if p.is_file() and p!=output/'evidence.json'})
            from . import archives
            archive=archives.create(output,output.with_name(output.name+'.tgz'))
            write(output.with_name(output.name+'.archive.json'),archive)
            result={**result,'archive':archive}
        return result

def report(directory,output):
    """Replay direct observations only, including every failed or missing point."""
    import csv,json
    if output.resolve().is_relative_to(directory.resolve()):raise ValueError('keep calibration reports outside raw evidence')
    from .evidence import evidence_hashes
    index=read(directory/'evidence.json')
    if evidence_hashes(directory,list(index))!=index:raise ValueError('calibration evidence hash changed')
    if not {'profile.json','calibration.json'}<=index.keys():raise ValueError('calibration input hashes are missing')
    plan=validate(read(directory/'profile.json'));result=read(directory/'calibration.json')
    if result.get('kind')!='direct-tool-calibration' or result.get('comparison_admitted') is not False:raise ValueError('not a diagnostic calibration')
    expected={(label,rate) for label,_ in patterns(plan) for rate in plan['rates_mbps']};seen={}
    for case in result['cases']:
        key=(case.get('packet_pattern',str(case['payload_bytes'])),case['offered_mbps'])
        if key not in expected or key in seen:raise ValueError('unexpected or duplicate calibration point')
        if case['passed'] and (not case['cleanup_ok'] or not case.get('headroom',{}).get('passed') or set(case.get('directions',{}))!={'udp_ul','udp_dl'}):raise ValueError('passed point lacks required outcomes')
        seen[key]=case
    rows=[];metrics=[]
    for label,sizes in patterns(plan):
        for rate in plan['rates_mbps']:
            case=seen.get((label,rate));status='missing' if case is None else 'passed' if case['passed'] else 'failed'
            for mode in ['udp_ul','udp_dl']:
                direction=(case or {}).get('directions',{}).get(mode,{})
                rows.append({'scope':'direct tool test; no core','pattern':label,'payload_bytes_by_stream':json.dumps(sizes),'offered_mbps':rate,'direction':mode,'status':status,
                    **{key:direction.get(key,'') for key in ['sender_mbps','receiver_mbps','receiver_pps','loss_percent','load_matched']},
                    'headroom_passed':(case or {}).get('headroom',{}).get('passed',''),'cleanup_ok':(case or {}).get('cleanup_ok',''),
                    'errors':json.dumps((case or {}).get('errors',[])),'quality_errors':json.dumps((case or {}).get('quality_errors',[])),
                    'socket_deltas':json.dumps((case or {}).get('socket_deltas',{}),sort_keys=True)})
            for metric,value in (case or {}).get('metrics',{}).items():metrics.append({'pattern':label,'offered_mbps':rate,'status':status,'metric':metric,'value':value,'note':(case or {}).get('metric_notes',{}).get(metric,'')})
    output.mkdir(parents=True,exist_ok=False)
    for name,values,fields in [('points.csv',rows,list(rows[0])),('all-metrics.csv',metrics,['pattern','offered_mbps','status','metric','value','note'])]:
        with (output/name).open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(values)
    summary={'scope':'direct tool test; no core','comparison_admitted':False,'expected_points':len(expected),'recorded_points':len(seen),
        'passed_points':sum(c['passed'] for c in seen.values()),'failed_points':sum(not c['passed'] for c in seen.values()),'missing_points':len(expected-seen.keys()),
        'collection_status':result['status'],'cleanup_ok':result['cleanup_ok'],'source_evidence_sha256':digest(directory/'evidence.json')}
    write(output/'summary.json',summary)
    return {'report':str(output),**summary}
