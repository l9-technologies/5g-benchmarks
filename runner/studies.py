"""Compile declarative studies into the existing pinned plans and executor."""
import contextlib
import csv
import datetime
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from .contracts import METRICS, FEATURES, USER_PLANES, balanced_repetitions, number, packet_sizes, schedule, validate_plan
from .evidence import check_inputs, digest, object_hash, read, verified_trials, write
from .execution import run
from .reporting import make_report
from .snapshot import save_report_tool

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_KEYS = {'duration_seconds', 'warmup_seconds', 'packet_bytes', 'offered_mbps_per_ue',
                 'user_plane','cpu_clock','resource_accounting','probe_timestamp_precision','traffic_modes','readiness_timeout_seconds','idle_seconds','packet_bytes_by_stream', 'probe_interval_seconds', 'resource_sample_seconds','load_tolerance_percent'}

def keys(value, allowed, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(name + ' has unknown keys: ' + ', '.join(sorted(unknown)))

def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,79}', value):
        raise ValueError(name + ' must use letters, digits and hyphens')

def validate_profile(profile):
    keys(profile, {'schema_version','name','cores','repetitions','seed','equivalence_percent',
                   'timeout_seconds','defaults','cases','confirmation','required_features','verification'}, 'profile')
    if profile.get('schema_version') != 1:
        raise ValueError('unknown study profile version')
    identifier(profile.get('name'), 'profile name')
    for field,low,high in [('repetitions',6,100),('seed',0,2**32-1),('equivalence_percent',.1,50)]:
        number(profile.get(field),field,low,high)
    if type(profile['repetitions']) is not int or type(profile['seed']) is not int:
        raise ValueError('repetitions and seed must be integers')
    if 'timeout_seconds' in profile:number(profile['timeout_seconds'],'timeout',1,86400)
    if 'cores' in profile:
        if not isinstance(profile['cores'], list) or not profile['cores'] or len(set(profile['cores'])) != len(profile['cores']):
            raise ValueError('cores must be a nonempty list of distinct adapter names')
        for core in profile['cores']:identifier(core,'core')
    verification=profile.get('verification',{})
    keys(verification,{'offered_mbps_per_ue','duration_seconds','warmup_seconds'},'verification')
    for field,low,high in [('offered_mbps_per_ue',.01,10000),('duration_seconds',2,300),('warmup_seconds',1,30)]:
        if field in verification:number(verification[field],'verification '+field,low,high)
    for field in ('duration_seconds','warmup_seconds'):
        if field in verification and type(verification[field]) is not int:raise ValueError('verification '+field+' must be an integer')
    keys(profile.get('defaults', {}), PROTOCOL_KEYS, 'defaults')
    cases=profile.get('cases')
    if not isinstance(cases,list) or not cases:
        raise ValueError('declare at least one case')
    names=set()
    for case in cases:
        keys(case, {'id','ue_counts','metrics'} | PROTOCOL_KEYS, 'case')
        identifier(case.get('id'),'case id')
        if case['id'] in names:raise ValueError('duplicate case id')
        names.add(case['id'])
        counts=case.get('ue_counts')
        if not isinstance(counts,list) or not counts or len(set(counts))!=len(counts) or any(type(n) is not int or not 1<=n<=253 for n in counts):
            raise ValueError('case UE counts must be distinct integers from 1 through 253')
        metrics=case.get('metrics')
        if not isinstance(metrics,list) or not metrics or len(set(metrics))!=len(metrics) or set(metrics)-METRICS.keys():
            raise ValueError('case metrics must be distinct known metric names')
        protocol={**profile.get('defaults',{}),**{k:v for k,v in case.items() if k in PROTOCOL_KEYS}}
        for count in counts:packet_sizes(protocol,count)
        if protocol.get('probe_timestamp_precision','micro') not in ('micro','nano'):raise ValueError('probe timestamp precision must be micro or nano')
        if protocol.get('resource_accounting') not in (None,'selected-core-processes'):raise ValueError('unknown resource accounting boundary')
        if protocol.get('cpu_clock') not in (None,'process-nanoseconds'):raise ValueError('unknown process CPU clock')
        if 'readiness_timeout_seconds' in protocol:number(protocol['readiness_timeout_seconds'],'readiness timeout',1,10)
        if 'idle_seconds' in protocol:number(protocol['idle_seconds'],'idle seconds',1,300)
        if 'traffic_modes' in protocol and (not isinstance(protocol['traffic_modes'],list) or
                len(set(protocol['traffic_modes']))!=len(protocol['traffic_modes']) or set(protocol['traffic_modes'])-{'tcp_ul','tcp_dl','udp_ul','udp_dl'}):
            raise ValueError('unknown or duplicate traffic mode')
        for field,low,high in [('duration_seconds',2,3600),('warmup_seconds',1,300),('packet_bytes',64,1400),('offered_mbps_per_ue',.01,10000)]:
            if field in protocol:number(protocol[field],field,low,high)
        if 'user_plane' in protocol and protocol['user_plane'] not in USER_PLANES:raise ValueError('unknown user-plane implementation')
        if 'load_tolerance_percent' in protocol:number(protocol['load_tolerance_percent'],'load tolerance',0,10)
        if 'probe_interval_seconds' in protocol:number(protocol['probe_interval_seconds'],'probe interval',.001,1)
        if 'resource_sample_seconds' in protocol:number(protocol['resource_sample_seconds'],'resource sample interval',.01,5)
    confirmation=profile.get('confirmation',[])
    if not isinstance(confirmation,list) or len(set(confirmation))!=len(confirmation) or set(confirmation)-names:
        raise ValueError('confirmation must name distinct declared cases')
    if 'required_features' in profile:
        features=profile['required_features']
        if not isinstance(features,list) or not features or len(set(features))!=len(features) or set(features)-set(FEATURES):
            raise ValueError('required features must be distinct known feature names')
    return profile

def _base_plan(profile, lab, output):
    keys(lab, {'prepared_plan','native','host_control'}, 'lab')
    if ('prepared_plan' in lab)==('native' in lab):
        raise ValueError('lab must select exactly one of prepared_plan or native')
    if 'prepared_plan' in lab:
        plan=validate_plan(read(Path(lab['prepared_plan']).resolve()))
        check_inputs(plan)
        return plan
    native=dict(lab['native'])
    native.setdefault('minimum_subscribers',1)
    if any(case.get('user_plane',profile.get('defaults',{}).get('user_plane'))=='linux-gtp' for case in profile['cases']):native['user_plane']='linux-gtp'
    allowed={'ueransim','open5gs_bin','free5gc','extension','extension_config',
             'iperf_bin','iperf_build_receipt','runtime_environment','requirements','mongo_image','max_host_busy_percent','ella_bin','qcore_bin','layout',
             'minimum_subscribers','user_plane','kernel_gtp_module','cipher_algorithm','slice_sd','provisioned_sqn','core_cpus','gnb_cpus','generator_cpus','observer_cpus'}
    keys(native,allowed,'native lab')
    if not native.get('ueransim'):raise ValueError('native lab needs the built UERANSIM path')
    argv=[sys.executable,str(ROOT/'native_lab.py'),'prepare','--output',str(output/'prepared')]
    for key,value in native.items():argv += ['--'+key.replace('_','-'),*map(str,value if isinstance(value,list) else [value])]
    if profile.get('cores'):argv += ['--cores',*profile['cores']]
    argv += ['--repetitions',str(profile['repetitions']),'--seed',str(profile['seed'])]
    result=subprocess.run(argv,capture_output=True,text=True)
    if result.returncode:raise ValueError('native preflight failed: '+result.stderr.strip())
    return validate_plan(read(output/'prepared/plan.json'))

def compile_plans(profile,base):
    candidates=base['candidates']
    if profile.get('cores'):
        available={c['core'] for c in candidates}
        if set(profile['cores'])-available:raise ValueError('adapter not prepared: '+', '.join(sorted(set(profile['cores'])-available)))
        candidates=[c for c in candidates if c['core'] in profile['cores']]
    repetitions=balanced_repetitions(len(candidates),profile['repetitions'])
    steps=[]; plans={}
    for case in profile['cases']:
        if base['mode']=='live' and any(metric.startswith(('tcp_ul_rtt_','tcp_dl_rtt_','udp_ul_rtt_','udp_dl_rtt_')) for metric in case['metrics']) and base['protocol'].get('probe_window_contract')!='timer-and-ue-pcap-v1':
            raise ValueError('loaded RTT metrics need the instrumented traffic generator and UE packet capture contract')
        protocol={**base['protocol'],**profile.get('defaults',{}),**{k:v for k,v in case.items() if k in PROTOCOL_KEYS},'ue_counts':case['ue_counts']}
        if base['mode']=='live' and protocol.get('user_plane')=='linux-gtp' and not base['protocol'].get('kernel_gtp_available'):raise ValueError('the prepared lab has no pinned Linux GTP generator')
        if 'readiness_timeout_seconds' in protocol:protocol['startup_boundary']='core launch through the last first N6 reply within the declared readiness window; wall-to-monotonic clock alignment at most 1 ms'
        for cohort in ['main']+(['confirmation'] if case['id'] in profile.get('confirmation',[]) else []):
            plan={**base,'candidates':candidates,'protocol':protocol,'metrics':case['metrics'],
                  'seed':profile['seed'],'repetitions':repetitions,'equivalence_percent':profile['equivalence_percent'],
                  'timeout_seconds':profile.get('timeout_seconds',base['timeout_seconds'])}
            if 'required_features' in profile:
                plan['required_features']=profile['required_features']
                if 'requirements' in plan:
                    registry={r['feature']:r for r in plan['requirements']['requirements']}
                    if set(plan['required_features'])-registry.keys():raise ValueError('required feature missing from pinned requirement registry')
                    plan['requirements']={**plan['requirements'],'requirements':[registry[f] for f in plan['required_features']]}
            validate_plan(plan)
            relative=Path('cases')/case['id']/cohort/'plan.json'
            plans[str(relative)]=plan;steps.append({'case':case['id'],'cohort':cohort,'plan':str(relative),
                 'plan_sha256':object_hash(plan),'run':str(Path('runs')/case['id']/cohort),'expected':len(schedule(plan))})
    return steps,plans,repetitions

def prepare(profile_path,lab_path,output):
    profile=validate_profile(read(profile_path));lab=read(lab_path)
    # Validate the lab shape before creating an output or touching the host.
    keys(lab,{'prepared_plan','native','host_control'},'lab')
    if 'host_control' in lab:
        from .host_control import validate
        config=validate(lab['host_control'],check_files=True)
    output.mkdir(parents=True,exist_ok=False)
    write(output/'profile.json',profile);write(output/'lab.json',lab)
    base=_base_plan(profile,lab,output)
    if 'host_control' in lab:
        paths={arg for argv in config['commands'].values() for arg in argv if Path(arg).is_absolute() and Path(arg).is_file()}
        config={**config,'inputs':{**config['inputs'],**{p:digest(Path(p)) for p in paths}}}
        base={**base,'host_control':config,'inputs':{**base['inputs'],**config['inputs']}}
    write(output/'base-plan.json',base)
    steps,plans,repetitions=compile_plans(profile,base)
    for name,plan in plans.items():
        path=output/name;path.parent.mkdir(parents=True);write(path,plan)
    save_report_tool(output/'report-tool.py')
    manifest={'schema_version':1,'profile_sha256':digest(output/'profile.json'),'lab_sha256':digest(output/'lab.json'),
              'report_tool_sha256':digest(output/'report-tool.py'),'base_plan_sha256':object_hash(base),'steps':steps,'expected':sum(s['expected'] for s in steps),
              'minimum_repetitions':profile['repetitions'],'actual_repetitions':repetitions}
    write(output/'study.json',manifest)
    return manifest

def check_profile(profile):
    # A health check uses the same adapters and feature requirements. It has
    # fresh disposable state and one short observation per candidate and repeat.
    settings={'duration_seconds':5,'warmup_seconds':1,**profile.get('verification',{})}
    cases=[{**case,**settings,'ue_counts':[max(case['ue_counts'])]} for case in profile['cases']]
    return {**profile,'name':'check-'+profile['name'][:70],'cases':cases,'confirmation':[]}

def prepare_check(profile_path,lab_path,output):
    profile=check_profile(validate_profile(read(profile_path)))
    with tempfile.TemporaryDirectory() as temporary:
        path=Path(temporary)/'profile.json';write(path,profile)
        manifest=prepare(path,lab_path,output)
    write(output/'check-method.json',{'source_profile_sha256':digest(profile_path)})
    return manifest

def check_status(output):
    manifest,_=verified_study(output);rows=[];failed_observations=[];failed=0
    count=len(read(output/manifest['steps'][0]['plan'])['candidates'])
    for step in manifest['steps']:
        batch,_=collected(output,step)
        if len(batch)>count:raise ValueError('verification contains unplanned trials')
        rows+=batch;selected=set(read(output/step['plan'])['metrics'])
        for row in batch:
            missing=selected-row['measurement'].get('metrics',{}).keys()
            unverified=selected & row['measurement'].get('metric_notes',{}).keys()
            if missing or unverified:failed_observations.append({'case':step['case'],'trial':row['trial']['id'],'missing_metrics':sorted(missing),'unverified_metrics':sorted(unverified)})
            failed+=bool(row['status']!='passed' or not row['measurement'].get('cleanup_ok') or missing or unverified)
    expected=count*len(manifest['steps'])
    return {'check_passed':len(rows)==expected and not failed,'verification_only':True,'failed':failed,
            'checked_candidates':len({r['trial']['candidate'] for r in rows}),'expected_candidates':count,
            'checked_trials':len(rows),'expected_checks':expected,
            'source_profile_sha256':read(output/'check-method.json')['source_profile_sha256'],
            'study':str(output),'failed_observations':failed_observations}

def execute_check(output,max_trials=None):
    if max_trials is not None and (type(max_trials) is not int or max_trials<1):raise ValueError('max trials must be a positive integer')
    manifest,_=verified_study(output)
    count=len(read(output/manifest['steps'][0]['plan'])['candidates'])
    with (output/'.study.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        recover(output)
        remaining=max_trials;new_trials=0
        for step in manifest['steps']:
            before,_=collected(output,step)
            if check_status(output)['failed'] or remaining==0:break
            if len(before)==count:continue
            target=output/step['run'];target.parent.mkdir(parents=True,exist_ok=True)
            bound=count-len(before)
            if remaining is not None:bound=min(bound,remaining)
            with contextlib.redirect_stdout(sys.stderr):result=run(output/step['plan'],target,resume=target.exists(),max_trials=bound,report_on_finish=False,max_total_trials=count)
            after,_=collected(output,step)
            new_trials+=result['new_trials']
            if remaining is not None:remaining-=result['new_trials']
    receipt=check_status(output)
    write(output/'check-receipt.json',receipt)
    return {**receipt,'new_trials':new_trials}

def check(profile_path,lab_path,output):
    prepare_check(profile_path,lab_path,output)
    return execute_check(output)

def verified_study(directory):
    manifest=read(directory/'study.json')
    if manifest.get('schema_version')!=1:raise ValueError('unknown study version')
    for name,key in [('profile.json','profile_sha256'),('lab.json','lab_sha256'),('report-tool.py','report_tool_sha256')]:
        if digest(directory/name)!=manifest[key]:raise ValueError('study input hash changed: '+name)
    profile=validate_profile(read(directory/'profile.json'))
    base=validate_plan(read(directory/'base-plan.json'))
    if object_hash(base)!=manifest['base_plan_sha256']:raise ValueError('base plan hash changed')
    expected,plans,repetitions=compile_plans(profile,base)
    if manifest['steps']!=expected or manifest['actual_repetitions']!=repetitions:
        raise ValueError('study steps differ from the declared profile')
    for step in expected:
        for name in ('plan','run'):
            if not (directory/step[name]).resolve().is_relative_to(directory.resolve()):raise ValueError('study path escapes output')
        path=directory/step['plan']
        if not path.resolve().is_relative_to(directory.resolve()):raise ValueError('study path escapes output')
        if read(path)!=plans[step['plan']]:raise ValueError('study plan hash changed')
    if sum(s['expected'] for s in expected)!=manifest['expected']:raise ValueError('study total coverage changed')
    return manifest,profile

def collected(directory,step):
    target=directory/step['run']
    if not (target/'manifest.json').exists():return [],[]
    plan=validate_plan(read(target/'plan.json'))
    if plan!=read(directory/step['plan']):raise ValueError('collected plan differs from the study')
    return verified_trials(target,plan,read(target/'manifest.json'))

def recover(directory):
    """Adopt verified results left before a manifest checkpoint; run no trial."""
    manifest,_=verified_study(directory)
    for step in manifest['steps']:
        target=directory/step['run']
        if not (target/'manifest.json').exists():continue
        saved=read(target/'manifest.json')
        if not any(entry['id'] not in saved['result_hashes'] and (target/'trials'/entry['id']/'result.json').exists() for entry in saved['schedule']):continue
        with contextlib.redirect_stdout(sys.stderr):run(directory/step['plan'],target,resume=True,recover_only=True,report_on_finish=False)

def status(directory):
    manifest,_=verified_study(directory);steps=[]
    for step in manifest['steps']:
        rows,problems=collected(directory,step)
        steps.append({**step,'collected':len(rows),'failed':sum(r['status']!='passed' for r in rows),
                      'cleanup_review_required':any(not r['measurement'].get('cleanup_ok',False) for r in rows),'problems':problems})
    count=sum(s['collected'] for s in steps)
    return {'expected':manifest['expected'],'collected':count,'failed':sum(s['failed'] for s in steps),
            'complete':count==manifest['expected'],'cleanup_review_required':any(s['cleanup_review_required'] for s in steps),'steps':steps}

def execute(directory,max_trials=None):
    manifest,_=verified_study(directory)
    if max_trials is not None and (type(max_trials) is not int or max_trials<1):raise ValueError('max trials must be a positive integer')
    with (directory/'.study.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);remaining=max_trials
        for step in manifest['steps']:
            target=directory/step['run']
            before=len(read(target/'manifest.json')['result_hashes']) if (target/'manifest.json').exists() else 0
            if before==step['expected']:
                collected(directory,step)
                continue
            if remaining==0:break
            target=directory/step['run'];target.parent.mkdir(parents=True,exist_ok=True)
            with contextlib.redirect_stdout(sys.stderr):
                result=run(directory/step['plan'],target,resume=target.exists(),max_trials=remaining,report_on_finish=False)
            rows,_=collected(directory,step);count=result['new_trials']
            write(directory/'progress.json',status(directory))
            if remaining is not None:remaining-=count
            if any(not r['measurement'].get('cleanup_ok',False) for r in rows):
                raise ValueError('study stopped: cleanup review required; failed evidence retained')
            if len(rows)<step['expected']:break
    return status(directory)

def _csv(path,rows,default):
    fields=list(dict.fromkeys(k for row in rows for k in row)) or default
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        writer.writerows({k:json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in rows)

def report(directory):
    with (directory/'.study.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _report(directory)

def _report(directory):
    manifest,profile=verified_study(directory);outcomes=[];metrics=[];comparisons=[];coverage=[]
    verification=(directory/'check-method.json').exists()
    if verification:check_status(directory)
    for step in manifest['steps']:
        rows,problems=collected(directory,step);plan=read(directory/step['plan']);by_id={r['trial']['id']:r for r in rows}
        entries=schedule(plan)[:len(plan['candidates'])] if verification else schedule(plan)
        if rows and not verification:
            # Use the shared comparison code; this writes only the compatible case report.
            child=make_report(directory/step['run'])
            canonical={}
            for c in plan['candidates']:
                identity=object_hash(c['identity'])
                canonical.setdefault(identity,c['id'])
            for c in child['comparisons']:
                if c['a'] in canonical.values() and c['b'] in canonical.values():
                    comparisons.append({'case':step['case'],'cohort':step['cohort'],**c})
        for entry in entries:
            row=by_id.get(entry['id']);m=row['measurement'] if row else {}
            candidate=next(c for c in plan['candidates'] if c['id']==entry['candidate'])
            common={**{k:plan['protocol'].get(k,'') for k in ('duration_seconds','warmup_seconds','packet_bytes','offered_mbps_per_ue','security','user_plane')},
                    'core_identity':candidate['identity']['core'],'radio_identity':candidate['identity']['radio'],
                    'provisioned_subscribers':m.get('provisioned_subscribers','not recorded'),'features':m.get('features',{}),'quality_errors':m.get('quality_errors',[]),
                    'case':step['case'],'cohort':step['cohort'],'trial':entry['id'],'candidate':entry['candidate'],
                    'ue_count':entry['ue_count'],'repetition':entry['repetition'],'kind':m.get('kind',plan['mode']),
                    'status':row['status'] if row else 'missing','cleanup_ok':m.get('cleanup_ok',False),
                    'driver_return_code':row.get('driver_return_code','not recorded') if row else '',
                    'error':row['error'] if row else 'not collected'}
            outcomes.append(common)
            for name in sorted(set(m.get('metrics',{}))|set(m.get('metric_errors',{}))|set(plan.get('metrics',METRICS))):
                metrics.append({**common,'metric':name,'unit':METRICS[name][0],'value':m.get('metrics',{}).get(name,''),
                    'metric_note':m.get('metric_notes',{}).get(name,''),'missing_reason':m.get('metric_errors',{}).get(name,'' if name in m.get('metrics',{}) else 'not collected')})
        coverage.append({'case':step['case'],'cohort':step['cohort'],'expected':len(entries),'collected':len(rows),
                         'failed':sum(r['status']!='passed' for r in rows),'missing':len(entries)-len(rows)})
    confirms={(c['case'],c['ue_count'],c['metric'],c['a'],c['b']):c for c in comparisons if c['cohort']=='confirmation'}
    decisions={'a_better','b_better','equivalent','no_clear_difference'}
    for c in comparisons:
        confirm=confirms.get((c['case'],c['ue_count'],c['metric'],c['a'],c['b']))
        c['claim_eligible']=c['cohort']=='main' and c['decision'] in decisions and bool(confirm and confirm['decision']==c['decision'])
        c['confirmation_decision']=confirm['decision'] if confirm else 'not_collected'
    out=directory/'reports'/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]);out.mkdir(parents=True)
    _csv(out/'trial-outcomes.csv',outcomes,['case','trial','status']);_csv(out/'all-metrics.csv',metrics,['case','trial','metric','value'])
    _csv(out/'coverage.csv',coverage,['case','expected','collected']);_csv(out/'comparisons.csv',comparisons,['case','metric','claim_eligible']);write(out/'comparisons.json',comparisons)
    summary={'expected':sum(c['expected'] for c in coverage),'verification_only':verification,'collected':sum(c['collected'] for c in coverage),'failed':sum(c['failed'] for c in coverage),
             'missing':sum(c['missing'] for c in coverage),'confirmed_comparisons':sum(c['claim_eligible'] for c in comparisons),
             'scope':'Declared software cases only. Nominal pairwise intervals; no overall rank or physical-radio certification.'}
    write(out/'report.json',summary)
    lines=['# Benchmark study','',f"Collected {summary['collected']}/{summary['expected']}; failed {summary['failed']}; missing {summary['missing']}.",'',summary['scope'],'',
           'All failures and missing measurements remain in the CSV files. Claim eligibility requires a valid repeated baseline and the same decision in the preselected confirmation set. Fixture and short runs cannot supply performance claims.','',
           '| Case | Cohort | Expected | Collected | Failed | Missing |','|---|---|---:|---:|---:|---:|']
    lines += [f"| {c['case']} | {c['cohort']} | {c['expected']} | {c['collected']} | {c['failed']} | {c['missing']} |" for c in coverage]
    lines += ['', '[All metrics](all-metrics.csv) · [All outcomes](trial-outcomes.csv) · [All comparisons](comparisons.csv)', '',
              'Resource values keep the declared boundary. Process PSS is not whole-system memory. A fixed offered rate is not maximum throughput. Missing required features block comparisons.']
    (out/'report.md').write_text('\n'.join(lines)+'\n');write(out/'artifact-index.json',{p.name:digest(p) for p in sorted(out.iterdir()) if p.is_file()})
    return out
