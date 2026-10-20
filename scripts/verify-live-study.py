#!/usr/bin/env python3
"""Verify a clean export, real adapters, resume, report replay, and archives.

Run stages in separate reserved windows on a shared host. These are
verification trials. They cannot support performance comparisons.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bundle import export
from runner.evidence import digest,object_hash,read,write


def initialize(profile_path,lab_path,output):
    if output.exists():
        method=read(output/'verification-inputs.json')
        if digest(profile_path)!=method['profile_sha256'] or digest(lab_path)!=method['lab_sha256']:
            raise ValueError('verification profile or lab changed; use a new output')
        if digest(Path(__file__))!=method['verifier_sha256']:raise ValueError('verification tool changed; use a new output')
        for name,expected in method['source_files'].items():
            if digest(output/'source'/name)!=expected:raise ValueError('verification export source changed: '+name)
        return
    output.mkdir(parents=True)
    with contextlib.redirect_stdout(io.StringIO()):export(output/'source')
    shutil.copyfile(profile_path,output/'profile.json');shutil.copyfile(lab_path,output/'lab.json')
    files=read(output/'source/bundle.json')['files']
    write(output/'verification-inputs.json',{'schema_version':1,'profile_sha256':digest(profile_path),'lab_sha256':digest(lab_path),
          'verifier_sha256':digest(Path(__file__)),'source_files':files,'source_tree_sha256':object_hash(files),
          'scope':'Functional and measurement verification only; not a core performance campaign'})


def command(output,label,*args,expected=(0,)):
    result=subprocess.run([sys.executable,str(output/'source/benchmark.py'),*map(str,args)],capture_output=True,text=True,timeout=900)
    (output/(label+'.stdout')).write_text(result.stdout);(output/(label+'.stderr')).write_text(result.stderr)
    if result.returncode not in expected:raise ValueError(label+' failed: '+result.stderr[-3000:])
    return json.loads(result.stdout)


def stage_interoperability(output):
    profile=read(output/'profile.json')
    metrics=['attach_success_percent','registration_success_percent','session_success_percent',
             *[mode+'_'+name for mode in ('tcp_ul','tcp_dl','udp_ul','udp_dl') for name in ('mbps','rtt_p95_ms','ping_loss_percent')]]
    case={'id':'interop','ue_counts':[min(10,max(n for c in profile['cases'] for n in c['ue_counts']))],
          'duration_seconds':5,'warmup_seconds':1,'offered_mbps_per_ue':1,'traffic_modes':['tcp_ul','tcp_dl','udp_ul','udp_dl'],'metrics':metrics}
    selected={**profile,'name':'live-interop-check','cases':[case],'confirmation':[]}
    write(output/'interop-profile.json',selected)
    result=command(output,'interoperability','study','check',output/'interop-profile.json','--lab',output/'lab.json','--output',output/'interoperability')
    if not result['check_passed']:raise ValueError('live interoperability check did not pass')
    return result


def stage_profile(output):
    if not (output/'interoperability/check-receipt.json').exists():raise ValueError('complete interoperability before profile admission')
    write(output/'prepared-lab.json',{'prepared_plan':str(output/'interoperability/base-plan.json')})
    return command(output,'profile-admission','study','check',output/'profile.json','--lab',output/'prepared-lab.json','--output',output/'profile-admission')


def result_bytes(directory):
    return {str(p.relative_to(directory)):p.read_bytes() for p in directory.glob('runs/*/*/trials/*/result.json')}


def stage_resume(output):
    base=read(output/'interoperability/base-plan.json')
    core=next(c['core'] for c in base['candidates'] if c['core']=='open5gs') if any(c['core']=='open5gs' for c in base['candidates']) else base['candidates'][0]['core']
    source=read(output/'profile.json')
    profile={**source,'name':'live-resume-verification','cores':[core],'repetitions':6,'confirmation':[],
             'cases':[{'id':'live-resume','ue_counts':[1],'duration_seconds':2,'warmup_seconds':1,
                       'traffic_modes':['udp_ul'],'offered_mbps_per_ue':1,'metrics':['attach_success_percent','udp_ul_mbps','udp_ul_rtt_p95_ms']}]}
    write(output/'resume-profile.json',profile);write(output/'prepared-lab.json',{'prepared_plan':str(output/'interoperability/base-plan.json')})
    directory=output/'resume-study'
    partial=command(output,'run-partial','study','run',output/'resume-profile.json','--lab',output/'prepared-lab.json','--output',directory,'--max-trials','3')
    before=result_bytes(directory)
    if partial['collected']!=3 or len(before)!=3 or partial['complete']:raise ValueError('partial checkpoint coverage differs')
    final=command(output,'run-resumed','study','resume',directory)
    if not final['complete'] or final['failed'] or final['collected']!=final['expected']:raise ValueError('resume did not complete cleanly')
    if any((directory/name).read_bytes()!=data for name,data in before.items()):raise ValueError('resume rewrote a saved trial')
    selected=set(profile['cases'][0]['metrics'])
    for data in result_bytes(directory).values():
        measurement=json.loads(data)['measurement']
        if selected-measurement['metrics'].keys() or selected & measurement.get('metric_notes',{}).keys():raise ValueError('resumed study has an unverified selected observation')
    return {'passed':True,'collected':final['collected'],'checkpoint_results_unchanged':len(before),'report':final['report']}


def stage_interrupt(output):
    directory=output/'interrupt-study'
    argv=[sys.executable,str(output/'source/benchmark.py'),'study','run',str(output/'resume-profile.json'),
          '--lab',str(output/'prepared-lab.json'),'--output',str(directory),'--max-trials','1']
    with (output/'interrupt.stdout').open('w') as stdout,(output/'interrupt.stderr').open('w') as stderr:
        child=subprocess.Popen(argv,stdout=stdout,stderr=stderr,start_new_session=True)
        try:
            deadline=time.monotonic()+60
            while not list(directory.glob('runs/*/*/trials/*/core-amf.log')):
                if child.poll() is not None or time.monotonic()>deadline:raise ValueError('interruption test did not reach core startup')
                time.sleep(.05)
            child.send_signal(signal.SIGINT);code=child.wait(timeout=60)
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:child.wait(timeout=60)
                except subprocess.TimeoutExpired:child.kill();child.wait()
    rows=result_bytes(directory)
    if code!=2 or len(rows)!=1:raise ValueError('interrupted result was not preserved')
    original=next(iter(rows.values()));record=json.loads(original)
    if record['status']!='failed' or record.get('driver_return_code')!=130 or not record['measurement'].get('cleanup_ok'):
        raise ValueError('interrupted driver has no verified failure and cleanup')
    resumed=command(output,'interrupt-resumed','study','resume',directory,'--max-trials','1',expected=(2,))
    if resumed['collected']!=2 or resumed['failed']!=1:raise ValueError('resume did not retain the interrupted failure')
    if any((directory/name).read_bytes()!=data for name,data in rows.items()):raise ValueError('resume rewrote interrupted evidence')
    return {'passed':True,'intentional_failed_trials':1,'cleanup_verified':True,'next_trial_completed':True}


def stage_replay(output):
    directory=output/'resume-study'
    first=command(output,'report-first','study','report',directory)
    report=Path(first['report']);before={p.name:p.read_bytes() for p in report.iterdir() if p.is_file()}
    source=output/'source';hidden=output/'source-hidden-for-replay'
    source.rename(hidden)
    try:
        result=subprocess.run([sys.executable,'-I',str(directory/'report-tool.py'),'study','report',str(directory)],cwd=output,capture_output=True,text=True,timeout=120)
        (output/'standalone-replay.stdout').write_text(result.stdout);(output/'standalone-replay.stderr').write_text(result.stderr)
        if result.returncode:raise ValueError('standalone report replay failed: '+result.stderr)
        replay=Path(json.loads(result.stdout)['report'])
        if before!={p.name:p.read_bytes() for p in replay.iterdir() if p.is_file()}:raise ValueError('standalone report bytes differ')
    finally:hidden.rename(source)
    archive=output/'resume-evidence.tgz'
    archived=command(output,'archive','study','archive',directory,'--output',archive)
    verified=command(output,'archive-readback','study','verify-archive',archive)
    if archived['sha256']!=verified['sha256']:raise ValueError('archive full readback differs')
    return {'passed':True,'report_files_identical':len(before),'archive_sha256':verified['sha256'],'archive_verified_files':verified['verified_files']}


STAGES={'interoperability':stage_interoperability,'profile':stage_profile,'resume':stage_resume,'interrupt':stage_interrupt,'replay':stage_replay}
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--lab',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stage',choices=['all',*STAGES],default='all');args=parser.parse_args()
    output=args.output.resolve()
    try:
        initialize(args.profile.resolve(),args.lab.resolve(),output)
        frozen=output/'source/scripts/verify-live-study.py'
        if Path(__file__).resolve()!=frozen.resolve():
            return subprocess.run([sys.executable,str(frozen),'--profile',str(args.profile.resolve()),'--lab',str(args.lab.resolve()),'--output',str(output),'--stage',args.stage]).returncode
        progress=read(output/'verification-progress.json') if (output/'verification-progress.json').exists() else {}
        for name,run in STAGES.items():
            if args.stage not in ('all',name) or name in progress:continue
            result=run(output);progress[name]=result;write(output/'verification-progress.json',progress)
        receipt={'passed':all(name in progress for name in STAGES),'stages':progress,'scope':'Live tool verification only; no performance comparison claims'}
        write(output/'verification-receipt.json',receipt);print(json.dumps(receipt));return 0
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        print('live verification: '+str(error),file=sys.stderr);return 2
if __name__=='__main__':sys.exit(main())
