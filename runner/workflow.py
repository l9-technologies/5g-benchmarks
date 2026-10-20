"""Run the existing study steps with one frozen source and resumable progress."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sys
from . import archives, studies
from .evidence import digest, read, write

ROOT=Path(__file__).resolve().parents[1]

def verify(directory):
    method=read(directory/'workflow.json')
    if method.get('schema_version')!=1:raise ValueError('unknown workflow version')
    for name in ('profile','lab'):
        if digest(directory/(name+'.json'))!=method[name+'_sha256']:raise ValueError('saved '+name+' changed')
    source=directory/'source'
    if digest(source/'bundle.json')!=method['source_index_sha256']:raise ValueError('saved source index changed')
    for name,expected in read(source/'bundle.json')['files'].items():
        path=source/name
        if not path.resolve().is_relative_to(source) or digest(path)!=expected:raise ValueError('saved source changed: '+name)
    return method

def start(profile,lab,directory,max_trials):
    studies.validate_profile(read(profile))
    # Import only for a new run. Saved report tools do not need the exporter.
    from bundle import export
    directory.mkdir(parents=True,exist_ok=False)
    with contextlib.redirect_stdout(io.StringIO()):export(directory/'source')
    shutil.copyfile(profile,directory/'profile.json');shutil.copyfile(lab,directory/'lab.json')
    write(directory/'workflow.json',{'schema_version':1,'profile_sha256':digest(directory/'profile.json'),
          'lab_sha256':digest(directory/'lab.json'),'source_index_sha256':digest(directory/'source/bundle.json')})
    return resume(directory,max_trials)

def status(directory):
    method=verify(directory)
    collection=directory/'collection';checks=directory/'checks'
    if not (collection/'study.json').exists():
        return {'phase':'preparing','failed':0,'checks':None,'collection':None}
    _,profile=studies.verified_study(collection)
    if profile!=read(directory/'profile.json'):raise ValueError('collection profile changed')
    result={'phase':'checking','failed':0,'checks':None,'collection':studies.status(collection)}
    if (checks/'study.json').exists():
        _,health_profile=studies.verified_study(checks)
        if health_profile!=studies.check_profile(profile) or read(checks/'base-plan.json')!=read(collection/'base-plan.json'):
            raise ValueError('verification differs from the collection contract')
        result['checks']=studies.check_status(checks)
        if result['checks']['source_profile_sha256']!=method['profile_sha256']:raise ValueError('verification source profile changed')
        if result['collection']['collected'] and not result['checks']['check_passed']:raise ValueError('collection started before verification passed')
        if result['checks']['failed']:result['phase']='check_failed'
        elif result['checks']['check_passed']:
            result['phase']='ready_to_report' if result['collection']['complete'] else 'collecting'
    elif result['collection']['collected']:raise ValueError('collection has no verification')
    result['failed']=result['collection']['failed']+(result['checks']['failed'] if result['checks'] else 0)
    if result['collection']['cleanup_review_required']:result['phase']='cleanup_failed'
    saved=directory/'finalization.json'
    if saved.exists():
        final=read(saved)
        for name,expected in final['sealed_files'].items():
            path=directory/name
            if not path.resolve().is_relative_to(directory) or digest(path)!=expected:raise ValueError('finalized evidence changed: '+name)
        if result['phase'] not in ('check_failed','cleanup_failed','ready_to_report'):raise ValueError('finalized workflow is incomplete')
        result.update(phase=final['phase'],report=str(directory/final['report']),checks_report=str(directory/final['checks_report']))
    receipt=directory.with_name(directory.name+'.archive.json')
    if receipt.exists():
        archive=read(receipt);path=directory.with_name(directory.name+'.tgz')
        if archive['archive']!=str(path) or digest(path)!=archive['sha256']:raise ValueError('saved archive changed')
        result['archive']=archive
    return result

def finalize(directory):
    result=status(directory)
    if not (directory/'finalization.json').exists():
        collection_report=studies.report(directory/'collection')
        checks_report=studies.report(directory/'checks')
        final={'phase':result['phase'] if result['phase'] in ('check_failed','cleanup_failed') else 'complete_with_failures' if result['failed'] else 'complete',
               'report':str(collection_report.relative_to(directory)),'checks_report':str(checks_report.relative_to(directory)),
               'sealed_files':{str(p.relative_to(directory)):digest(p) for p in directory.rglob('*') if p.is_file()}}
        write(directory/'finalization.json',final)
    archive=directory.with_name(directory.name+'.tgz');receipt=directory.with_name(directory.name+'.archive.json')
    if not receipt.exists():
        # Recover a completed archive if the process stopped before its receipt.
        if archive.exists():saved={'archive':str(archive),**archives.verify(archive,expected_directory=directory)}
        else:saved=archives.create(directory,archive)
        write(receipt,saved)
    return status(directory)

def execute(directory,max_trials=None):
    if max_trials is not None and (type(max_trials) is not int or max_trials<1):raise ValueError('max trials must be a positive integer')
    with (directory/'.workflow.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        verify(directory)
        if (directory/'finalization.json').exists():return finalize(directory)
        collection=directory/'collection';checks=directory/'checks'
        if not collection.exists():studies.prepare(directory/'profile.json',directory/'lab.json',collection)
        if not (collection/'study.json').exists():raise ValueError('incomplete preparation; preserve this output and use a new workflow')
        if not checks.exists():
            write(directory/'prepared-lab.json',{'prepared_plan':str(collection/'base-plan.json')})
            studies.prepare_check(directory/'profile.json',directory/'prepared-lab.json',checks)
        studies.recover(checks);studies.recover(collection)
        current=status(directory);remaining=max_trials
        if not current['checks']['check_passed'] and not current['checks']['failed']:
            result=studies.execute_check(checks,max_trials=remaining)
            if remaining is not None:remaining-=result['new_trials']
        current=status(directory)
        if current['checks']['check_passed'] and current['phase']!='cleanup_failed' and remaining!=0:
            try:studies.execute(collection,max_trials=remaining)
            except ValueError:
                if not status(directory)['collection']['cleanup_review_required']:raise
        current=status(directory)
        if max_trials is None and current['phase'] in ('check_failed','cleanup_failed','ready_to_report'):return finalize(directory)
        return current

def resume(directory,max_trials=None):
    verify(directory)
    frozen=directory/'source'
    if ROOT!=frozen:
        argv=[sys.executable,str(frozen/'benchmark.py'),'study','resume',str(directory)]
        if max_trials is not None:argv+=['--max-trials',str(max_trials)]
        # Replace this wrapper so interruption reaches the evidence owner.
        os.execv(sys.executable,argv)
    result=execute(directory,max_trials)
    print(json.dumps(result))
    return 2 if result['failed'] or result['phase']=='check_failed' else 0
