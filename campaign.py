#!/usr/bin/env python3
"""Prepare and repeat the complete software benchmark campaign."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from benchmark import balanced_repetitions, check_inputs, digest, read, run, schedule, validate_plan, write

ROOT=Path(__file__).resolve().parent


def prepare(base_path,output,soak_seconds):
    base=validate_plan(read(base_path));check_inputs(base)
    if len(base['driver'])<4 or Path(base['driver'][1]).name!='native_lab.py':
        raise ValueError('campaign requires a prepared native lab plan')
    base['driver'][1]=str(ROOT/'native_lab.py')
    base['inputs']={p:v for p,v in base['inputs'].items() if Path(p).name not in ('benchmark.py','native_lab.py')}
    base['inputs'].update({str((ROOT/name).resolve()):digest(ROOT/name) for name in ('native_lab.py','benchmark.py')})
    check_inputs(base)
    output.mkdir(parents=True,exist_ok=False)
    jobs=[]
    write(output/'matrix-plan.json',base)
    jobs.append({'id':'matrix','lane':'matrix','plan':'matrix-plan.json'})
    low=json.loads(json.dumps(base))
    low['protocol'].update(ue_counts=sorted({min(base['protocol']['ue_counts']),max(base['protocol']['ue_counts'])}),offered_mbps_per_ue=1)
    low['candidates']=[c for c in low['candidates'] if c['radio']=='ueransim']
    if low.get('method_version',1)==2:low['repetitions']=balanced_repetitions(len(low['candidates']),base.get('minimum_repetitions',6))
    validate_plan(low);write(output/'capacity-low-rate-plan.json',low)
    jobs.append({'id':'capacity-low-rate','lane':'capacity','plan':'capacity-low-rate-plan.json'})
    for name,mbps,packet in [('rate-25',25,1200),('rate-100',100,1200),('rate-500',500,1200),
                             ('packet-64',25,64),('packet-256',25,256),('packet-1400',25,1400)]:
        plan=json.loads(json.dumps(base))
        plan['protocol'].update(ue_counts=[1],offered_mbps_per_ue=mbps,packet_bytes=packet)
        plan['candidates']=[c for c in plan['candidates'] if c['radio']=='ueransim']
        if plan.get('method_version',1)==2:plan['repetitions']=balanced_repetitions(len(plan['candidates']),base.get('minimum_repetitions',6))
        validate_plan(plan);write(output/f'{name}-plan.json',plan)
        jobs.append({'id':name,'lane':'sweeps','plan':f'{name}-plan.json'})
    cores=sorted({c['core'] for c in base['candidates']})
    for core in cores:
        jobs.append({'id':f'fresh-session-control-{core}','lane':'experiments','case':'fresh-session','core':core,'args':['--seconds','120']})
        for nf in ('amf','smf','upf'):
            for repetition in range(1,4):
                jobs.append({'id':f'recovery-{core}-{nf}-r{repetition}','lane':'experiments','case':'recovery','core':core,'args':['--nf',nf,'--seconds','120']})
                jobs.append({'id':f'fresh-recovery-{core}-{nf}-r{repetition}','lane':'experiments','case':'fresh-recovery','core':core,'args':['--nf',nf,'--seconds','120']})
        jobs.append({'id':f'churn-{core}','lane':'experiments','case':'churn','core':core,'args':['--cycles','20']})
        for variant in ('bad-key','unknown-subscriber','bad-dnn','bad-slice'):
            jobs.append({'id':f'reject-{core}-{variant}','lane':'experiments','case':'reject','core':core,'args':['--variant',variant]})
        jobs.append({'id':f'stability-{core}','lane':'stability','case':'soak','core':core,'args':['--seconds',str(soak_seconds),'--ue-count','10','--mbps','1']})
    write(output/'campaign.json',{'schema_version':1,'base_plan':str((output/'matrix-plan.json').resolve()),'jobs':jobs,
          'inputs':{str(base_path.resolve()):digest(base_path),**{str(p.resolve()):digest(p) for p in output.glob('*-plan.json')},
                    **{str((ROOT/name).resolve()):digest(ROOT/name) for name in ('campaign.py','experiments.py','native_lab.py','benchmark.py')}}})
    print(output/'campaign.json')


def execute(path,lane,worker,count):
    campaign=read(path)
    if not 1<=count<=6 or not 0<=worker<count:raise ValueError('invalid worker index or count')
    for name,expected in campaign['inputs'].items():
        if digest(name)!=expected:raise ValueError(f'campaign input changed: {name}')
    jobs=[j for j in campaign['jobs'] if j['lane']==lane]
    paired=lane in ('matrix','capacity')
    if not paired:jobs=[j for i,j in enumerate(jobs) if i%count==worker]
    root=path.parent/f'{lane}-worker-{worker}';root.mkdir(exist_ok=False)
    results=[]
    for job in jobs:
        output=root/job['id']
        if 'plan' in job:
            report=run(path.parent/job['plan'],output,worker if paired else 0,count if paired else 1)
            row={'job':job['id'],'status':report['status'],'trial_count':report['trial_count']}
            expected=[r for r in schedule(read(path.parent/job['plan'])) if not paired or (r['repetition']-1)%count==worker]
            if report['trial_count']!=len(expected) or any(s['features']['cleanup']['passed']!=s['attempts'] for s in report['summaries']):
                raise ValueError(f"incomplete run or cleanup failed: {job['id']}")
        else:
            argv=[sys.executable,str(ROOT/'experiments.py'),campaign['base_plan'],'--core',job['core'],'--case',job['case'],'--output',str(output),*job['args']]
            with (root/f"{job['id']}.log").open('w') as log:
                result=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT)
            row={'job':job['id'],'exit_code':result.returncode,'receipt':str(output/'experiment.json')}
            if not (output/'experiment.json').exists():raise ValueError(f"missing experiment receipt: {job['id']}")
            if not read(output/'experiment.json')['cleanup_ok']:raise ValueError(f"cleanup failed: {job['id']}")
        results.append(row);write(root/'jobs.json',results)
        print(json.dumps(row),flush=True)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('base_plan',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--soak-seconds',type=int,default=3600)
    p=sub.add_parser('run');p.add_argument('campaign',type=Path);p.add_argument('--lane',choices=('matrix','capacity','sweeps','experiments','stability'),required=True);p.add_argument('--worker-index',type=int,default=0);p.add_argument('--worker-count',type=int,default=1)
    args=parser.parse_args()
    if args.action=='prepare':
        if not 60<=args.soak_seconds<=86400:parser.error('stability duration must be 60 through 86400 seconds')
        prepare(args.base_plan,args.output,args.soak_seconds)
    else:execute(args.campaign,args.lane,args.worker_index,args.worker_count)
