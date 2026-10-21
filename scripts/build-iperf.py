#!/usr/bin/env python3
"""Build and check the pinned traffic generator once, before timed collection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
URL='https://downloads.es.net/pub/iperf/iperf-3.16.tar.gz'
SHA256='cc740c6bbea104398cc3e466befc515a25896ec85e44a662d5f4a767b9cf713e'

def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def build(output,source_archive,jobs):
    output=output.resolve();output.mkdir(parents=True,exist_ok=False)
    archive=output/'iperf-3.16.tar.gz'
    if source_archive:shutil.copyfile(source_archive,archive)
    else:
        with urllib.request.urlopen(URL,timeout=60) as response,archive.open('xb') as target:shutil.copyfileobj(response,target)
    if digest(archive)!=SHA256:raise ValueError('traffic generator source archive hash changed')
    with tarfile.open(archive) as bundle:bundle.extractall(output,filter='data')
    source=output/'iperf-3.16';patch=ROOT/'patches/iperf-3.16-phase-clock.patch'
    pacing=ROOT/'patches/iperf-3.16-pacing.patch'
    with (output/'build.log').open('wb') as log:
        for argv in [['patch','-p1','-i',str(patch)],['patch','-p1','-i',str(pacing)],['autoreconf','-fi'],['./configure','--disable-shared','--prefix='+str(output/'install')],['make','-j'+str(jobs)],['make','install']]:
            subprocess.run(argv,cwd=source,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
    binary=output/'install/bin/iperf3';checks=[]
    for mode in ('tcp_ul','tcp_dl','udp_ul','udp_dl'):
        with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
        server_log=output/(mode+'-server.log');events=output/(mode+'-phase.jsonl')
        before=resource.getrusage(resource.RUSAGE_CHILDREN);started=time.monotonic()
        with server_log.open('wb') as log:
            server=subprocess.Popen([str(binary),'-s','-B','127.0.0.1','-p',str(port),'--forceflush'],stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+5
                while 'Server listening' not in server_log.read_text():
                    if server.poll() is not None or time.monotonic()>deadline:raise ValueError('generator self-check server did not start')
                    time.sleep(.05)
                argv=[str(binary),'-c','127.0.0.1','-p',str(port),'-t','2','-O','1','-b','1M','-J']
                if mode.startswith('udp'):argv+=['-u','-l','1200']
                if mode.endswith('dl'):argv+=['-R']
                result=subprocess.run(argv,env={**os.environ,'BENCH_PHASE_EVENTS':str(events)},capture_output=True,text=True,timeout=15)
                (output/(mode+'-client.json')).write_text(result.stdout)
                (output/(mode+'-client.err')).write_text(result.stderr)
                result.check_returncode();data=json.loads(result.stdout)
                rows=[json.loads(line) for line in events.read_text().splitlines()]
                if [r['event'] for r in rows]!=['warmup_end','test_end']:raise ValueError('generator timer event coverage failed')
                if not 1<(rows[-1]['monotonic_before_ns']-rows[0]['monotonic_after_ns'])/1e9<4:raise ValueError('generator timer interval failed')
                if data['end']['sum_received']['bytes']<=0:raise ValueError('generator self-check delivered no data')
            finally:
                server.terminate()
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:server.kill();server.wait()
        after=resource.getrusage(resource.RUSAGE_CHILDREN);wall=time.monotonic()-started
        cpu_seconds=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime
        cpu_percent=100*cpu_seconds/wall
        check={'mode':mode,'passed':cpu_percent<=25,'kind':'1-Mbit/s loopback functional and CPU check',
               'combined_cpu_seconds':cpu_seconds,'wall_seconds':wall,'combined_cpu_percent':cpu_percent,'cpu_limit_percent':25}
        checks.append(check)
        (output/(mode+'-cpu.json')).write_text(json.dumps(check,indent=2)+'\n')
        if not check['passed']:raise ValueError('generator low-rate CPU check exceeded 25% of one CPU: '+mode)
    receipt={'schema_version':1,'instrumentation':'traffic-phase-v1','binary':str(binary),'binary_sha256':digest(binary),
             'source_url':URL,'source_archive_sha256':SHA256,'patch_sha256':digest(patch),'pacing_patch_sha256':digest(pacing),
             'pacing_upstream_commit':'783095e138b7257dfcbf4e9aa2a48685d14c7f16','checks':checks,
             'platform':platform.platform(),'version':subprocess.check_output([str(binary),'--version'],text=True)}
    (output/'build-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--source-archive',type=Path)
    parser.add_argument('--jobs',type=int,choices=range(1,5),default=1);args=parser.parse_args()
    try:print(json.dumps(build(args.output,args.source_archive,args.jobs)));return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print('generator build: '+str(error),file=sys.stderr);return 2
if __name__=='__main__':sys.exit(main())
