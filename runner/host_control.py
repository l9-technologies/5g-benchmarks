"""Shared bounded host reservations. Host commands use the saved lab contract."""
import contextlib
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from .evidence import digest,read,write

ROOT=Path(__file__).resolve().parents[1]
ACTIONS={'snapshot','acquire','sample','restore','verify_restored'}

def validate(config, *, check_files=False):
    allowed={'schema_version','locks','max_seconds','restore_seconds','sample_seconds','minimum_free_bytes',
             'minimum_available_memory_bytes','inputs','commands'}
    if not isinstance(config,dict) or set(config)-allowed or config.get('schema_version')!=1:raise ValueError('invalid host control schema')
    for field,low,high in [('max_seconds',10,1200),('restore_seconds',5,180),('sample_seconds',.05,30),
                           ('minimum_free_bytes',1,2**60),('minimum_available_memory_bytes',1,2**60)]:
        value=config.get(field)
        if type(value) not in (int,float) or not low<=value<=high:raise ValueError('invalid host '+field)
    if config['restore_seconds']>=config['max_seconds']/2:raise ValueError('host restoration budget must be less than half the window')
    locks=config.get('locks')
    if not isinstance(locks,list) or not locks or len(set(locks))!=len(locks) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in locks):raise ValueError('host locks must be distinct absolute paths')
    commands=config.get('commands')
    if not isinstance(commands,dict) or set(commands)!=ACTIONS:raise ValueError('declare every host control command')
    inputs=config.get('inputs')
    if not isinstance(inputs,dict) or not inputs:raise ValueError('pin host command inputs')
    for argv in commands.values():
        if not isinstance(argv,list) or not argv or any(not isinstance(s,str) or not s or '\0' in s for s in argv):raise ValueError('host command must be an argument list')
        if not Path(argv[0]).is_absolute() or (check_files and not os.access(argv[0],os.X_OK)):raise ValueError('host command executable must be installed at an absolute path')
        if '{lease}' not in argv:raise ValueError('host command must receive {lease}')
    for name,expected in inputs.items():
        if not isinstance(name,str) or not Path(name).is_absolute() or not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('invalid host input hash')
        if check_files and digest(Path(name))!=expected:raise ValueError('host input changed: '+name)
    return config

@contextlib.contextmanager
def reservation(config):
    with contextlib.ExitStack() as stack:
        handles=[]
        for path in config['locks']:
            handle=stack.enter_context(Path(path).open('a'))
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);handles.append(handle)
        yield handles

def identity(pid):
    row=subprocess.run(['ps','-p',str(pid),'-o','stat=,lstart='],capture_output=True,text=True).stdout.strip().split(None,1)
    return row[1] if len(row)==2 and not row[0].startswith('Z') else ''

def launch(argv,cwd,env,stdout,stderr,path,deadline):
    request={'argv':list(map(str,argv)),'cwd':str(cwd),'owner_pid':os.getpid(),'owner_identity':identity(os.getpid()),
             'deadline_epoch':deadline,'released':False}
    write(path,request)
    proc=subprocess.Popen([sys.executable,'-m','runner.host_control','worker',str(path)],cwd=ROOT,env=env,
                          stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
    request.update(job_pid=proc.pid,job_identity=identity(proc.pid),released=True);write(path,request)
    return proc

def command(config,action,directory):
    argv=[str(directory) if s=='{lease}' else s for s in config['commands'][action]]
    target=directory/(action+'-'+str(time.time_ns())+'.json')
    # Restoration uses the saved recovery command even after input damage.
    proc=launch(argv,ROOT,os.environ,subprocess.PIPE,subprocess.PIPE,directory/'command-process.json',time.time()+config['restore_seconds']/2)
    row={'action':action,'argv':argv,'passed':False}
    try:
        stdout,stderr=proc.communicate(timeout=config['restore_seconds']/2)
        row.update(return_code=proc.returncode,stdout=stdout.decode(errors='replace'),stderr=stderr.decode(errors='replace'))
        try:row['result']=json.loads(row['stdout'])
        except ValueError:row['result']={}
        row['passed']=proc.returncode==0 and isinstance(row['result'],dict) and row['result'].get('passed') is True
    except BaseException as error:
        row['error']=str(error)
        raise
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGKILL);proc.communicate()
        write(target,row)
    if not row['passed']:raise ValueError('host '+action+' check failed; see '+str(target))
    return row['result']

def resources(config,directory):
    free=shutil.disk_usage(directory).free
    # Linux admission uses available memory, not total memory or swap.
    memory=None
    if Path('/proc/meminfo').exists():
        memory=int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:')))*1024
    elif sys.platform=='darwin':
        text=subprocess.check_output(['/usr/bin/vm_stat'],text=True)
        page_size=int(re.search(r'page size of (\d+) bytes',text)[1])
        pages={k:int(v) for k,v in re.findall(r'^(Pages [^:]+):\s+(\d+)\.',text,re.M)}
        memory=page_size*sum(pages.get(k,0) for k in ('Pages free','Pages inactive','Pages speculative'))
    result={'free_bytes':free,'available_memory_bytes':memory,
            'passed':free>=config['minimum_free_bytes'] and memory is not None and memory>=config['minimum_available_memory_bytes']}
    if not result['passed']:raise ValueError('host disk or available-memory admission failed: '+json.dumps(result))
    return result

def stop_job(lease):
    pid=lease.get('job_pid')
    if not pid or identity(pid)!=lease.get('job_identity'):return
    try:os.killpg(pid,signal.SIGTERM)
    except ProcessLookupError:return
    end=time.monotonic()+2
    while time.monotonic()<end:
        if identity(pid)!=lease['job_identity']:return
        time.sleep(.05)
    if identity(pid)==lease['job_identity']:
        try:os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError:pass

def restore(directory):
    config=read(directory/'config.json')
    with (directory/'restore.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        lease=read(directory/'lease.json')
        if lease.get('restored'):return lease
        errors=[]
        for name in ['command-process.json','driver-process.json']:
            if (directory/name).exists():stop_job(read(directory/name))
        if lease.get('acquire_intent'):
            for action in ['restore','verify_restored']:
                try:command(config,action,directory)
                except Exception as error:errors.append(str(error))
        lease.update(restored=not errors,restoration_complete=True,restore_errors=errors,finished_epoch=time.time())
        lease['within_deadline']=time.time()<=lease['deadline_epoch']
        write(directory/'lease.json',lease)
        return lease

def guard(directory):
    lease=read(directory/'lease.json')
    (directory/'guard-ready').touch()
    while not read(directory/'lease.json').get('restoration_complete'):
        if identity(lease['controller_pid'])!=lease['controller_identity'] or time.time()>=lease['work_deadline_epoch']:
            restore(directory);return
        time.sleep(.1)

def worker(path):
    # The owner records identity before it releases either a host command or a
    # driver. A guard can therefore stop a partial acquisition before restore.
    request=read(path)
    while True:
        if identity(request['owner_pid'])!=request['owner_identity'] or time.time()>=request['deadline_epoch']:return 130
        request=read(path)
        if request['released']:break
        time.sleep(.02)
    os.chdir(request['cwd'])
    os.execvpe(request['argv'][0],request['argv'],os.environ)

def invoke(config,argv,cwd,env,timeout,log,directory):
    validate(config,check_files=True);directory.mkdir()
    write(directory/'config.json',config)
    started=time.time();lease={'controller_pid':os.getpid(),'controller_identity':identity(os.getpid()),
        'started_epoch':started,'deadline_epoch':started+config['max_seconds'],
        'work_deadline_epoch':started+config['max_seconds']-config['restore_seconds'],
        'acquire_intent':False,'restored':False,'restore_errors':[],'quality_errors':[]}
    write(directory/'lease.json',lease)
    watcher=None;proc=None;code=125;errors=[]
    with contextlib.ExitStack() as stack:
        try:
            handles=stack.enter_context(reservation(config))
            write(directory/'resources.json',resources(config,directory))
            command(config,'snapshot',directory)
            with (directory/'guard.log').open('wb') as guard_log:
                watcher=subprocess.Popen([sys.executable,'-m','runner.host_control','guard',str(directory)],cwd=ROOT,
                    pass_fds=tuple(h.fileno() for h in handles),stdin=subprocess.DEVNULL,stdout=guard_log,stderr=subprocess.STDOUT,start_new_session=True)
            ready=time.monotonic()+2
            while not (directory/'guard-ready').exists():
                if watcher.poll() is not None or time.monotonic()>=ready:raise ValueError('restoration guard did not start')
                time.sleep(.02)
            lease['acquire_intent']=True;write(directory/'lease.json',lease)
            command(config,'acquire',directory);command(config,'sample',directory)
            if time.time()>=lease['work_deadline_epoch']:raise ValueError('host admission used the work budget')
            with Path(log).open('wb') as output:
                proc=launch(argv,cwd,env,output,subprocess.STDOUT,directory/'driver-process.json',lease['work_deadline_epoch'])
                lease.update(job_pid=proc.pid,job_identity=identity(proc.pid));write(directory/'lease.json',lease)
                end=min(time.monotonic()+timeout,time.monotonic()+lease['work_deadline_epoch']-time.time())
                while proc.poll() is None:
                    if time.monotonic()>=end:raise ValueError('host work deadline exceeded')
                    time.sleep(min(config['sample_seconds'],max(0,end-time.monotonic())))
                    command(config,'sample',directory)
                code=proc.wait();command(config,'sample',directory)
        except (Exception,KeyboardInterrupt) as error:
            errors.append(str(error) or 'host run interrupted');code=130 if isinstance(error,KeyboardInterrupt) else 125
        finally:
            # The guard owns restoration if this process dies. The main path
            # waits for the child before it releases the reservation.
            try:final=restore(directory)
            finally:
                if proc is not None:
                    try:proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:errors.append('owned job did not exit')
            if watcher is not None:
                try:watcher.wait(timeout=config['restore_seconds']+4)
                except subprocess.TimeoutExpired:
                    # Failed restore leaves locks with the guard until it exits.
                    errors.append('restoration guard did not exit')
            final=read(directory/'lease.json');errors+=final.get('restore_errors',[])
            if not final.get('within_deadline'):errors.append('host reservation deadline exceeded')
            final['quality_errors']=errors;write(directory/'lease.json',final)
    return code

def annotate(directory,measurement):
    host=directory/'host';lease=read(host/'lease.json')
    files=[str(p.relative_to(directory)) for p in host.rglob('*') if p.is_file()]
    # Keep the driver's original result before adding controller evidence.
    shutil.copyfile(directory/'measurement.json',directory/'driver-host-measurement.json')
    measurement['evidence']+=['driver-host-measurement.json',*files]
    measurement['quality_errors']+=lease['quality_errors']
    if not lease.get('restored') or lease.get('restore_errors'):
        measurement['cleanup_ok']=False
    write(directory/'measurement.json',measurement)

if __name__=='__main__':
    action=sys.argv[1];directory=Path(sys.argv[2])
    if action=='guard':guard(directory)
    elif action=='worker':sys.exit(worker(directory))
