"""Prepare and verify the shared receive-CPU classifier before collection."""
from pathlib import Path
import shutil
import sys
from .common import command
from runner.evidence import digest, write


def prepare(output, capacities):
    compiler=shutil.which('clang-18') or shutil.which('clang')
    if not compiler:raise ValueError('install clang with the BPF target before prepare')
    source=Path(__file__).with_name('flow_hash.c')
    arch=command(['gcc','-dumpmachine']).strip()
    result={}
    for count in sorted(set(capacities)):
        if count not in (1,2,4,8,16):raise ValueError('receive steering supports 1, 2, 4, 8, or 16 CPUs')
        runtime=output/f'flow_hash-{count}.o';check=output/f'flow_hash-{count}-check.o'
        for path,test in [(runtime,False),(check,True)]:
            argv=[compiler,'-DRECEIVE_CPUS='+str(count),'-O2','-g','-target','bpf','-I/usr/include/'+arch,'-c',source,'-o',path]
            if test:argv.insert(1,'-DTEST_STEERING')
            command(argv)
        command([sys.executable,Path(__file__).with_name('check_flow_hash.py'),str(count),output,source])
        result[str(count)]=str(runtime)
    write(output/'steering-build.json',{'compiler':compiler,'version':command([compiler,'--version']),
                                      'source_sha256':digest(source),'objects':{k:digest(v) for k,v in result.items()}})
    return result


def cpu_mask(cpus):
    value=sum(1<<cpu for cpu in cpus)
    text=format(value,'x');text='0'*((-len(text))%8)+text
    return ','.join(text[i:i+8] for i in range(0,len(text),8))


def apply(lab,namespace,device,receive_cpus,peer_capacity):
    mask=cpu_mask(receive_cpus)
    # Device names are fixed by Lab.setup; masks contain hexadecimal digits only.
    script=f'for q in /sys/class/net/{device}/queues/rx-*; do echo {mask} > "$q/rps_cpus"; cat "$q/rps_cpus"; done'
    actual=lab.ns(namespace,['sh','-c',script]).split()
    if not actual or any(int(x.replace(',',''),16)!=int(mask.replace(',',''),16) for x in actual):
        raise ValueError('receive CPU steering did not apply')
    objects=lab.config.get('flow_hash_objects')
    if objects:
        lab.ns(namespace,['tc','qdisc','add','dev',device,'clsact'])
        lab.ns(namespace,['tc','filter','add','dev',device,'egress','bpf','da','obj',objects[str(peer_capacity)],'sec','classifier'])
    return {'namespace':namespace,'device':device,'receive_cpus':receive_cpus,'rps_readback':actual,'peer_capacity':peer_capacity}
