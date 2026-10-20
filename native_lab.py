#!/usr/bin/env python3
"""Prepare and run an isolated native Linux 5G comparison lab."""
from __future__ import annotations

import argparse
import bisect
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from benchmark import balanced_repetitions, FEATURES, METRICS, digest, number, object_hash, percentile, read, stop_group, validate_plan, write

from adapters import open5gs,free5gc,ueransim,ella,qcore,kernel_gtp,session_ambr,steering,probe_window
from adapters.common import NFS,OPEN_NFS,FREE_NFS,KEY,OPC,UE_ROUTE_TABLE_BASE,initial_sqn,command,module,service_profile,subscriber_sqn

ROOT = Path(__file__).resolve().parent


def udp_counters(text):
    rows = [line.split()[1:] for line in text.splitlines() if line.startswith('Udp:')]
    if len(rows) != 2 or len(rows[0]) != len(rows[1]):
        raise ValueError('UDP counter sample is incomplete')
    result = dict(zip(rows[0], map(int, rows[1])))
    if not {'RcvbufErrors', 'SndbufErrors'} <= result.keys():
        raise ValueError('UDP drop counters are missing')
    return result


def receiver_metrics(data, udp):
    if "error" in data or "sum_received" not in data.get("end", {}):
        raise ValueError("iperf3 receiver result is missing")
    row = data["end"]["sum_received"]
    return row["bits_per_second"] / 1e6, row["lost_percent"] if udp else None


def ping_metrics(text):
    match = re.search(r"(\d+) packets transmitted, (\d+) received", text)
    if not match:
        raise ValueError("ping counters missing")
    sent, received = map(int, match.groups())
    values = [float(x) for x in re.findall(r"time[=<]([0-9.]+) ms", text)]
    if sent <= 0 or received != len(values):
        raise ValueError("ping replies and counters differ")
    return {"sent": sent, "received": received, "loss_percent": 100 * (sent - received) / sent,
            **{f"p{p}_ms": percentile(values, p / 100) for p in (50, 95, 99) if values}}


def procedure_counts(text, count):
    """Count observed UE outcomes separately from successful timing samples."""
    result = {}
    markers = {
        'registration': ('Sending Initial Registration', 'Initial Registration is successful', 'Registration Reject received'),
        'session': ('Sending PDU Session Establishment Request', 'PDU Session establishment is successful', 'PDU Session Establishment Reject received'),
    }
    for procedure, names in markers.items():
        events = [set(), set(), set()]
        requests = 0
        for line in text.splitlines():
            match = re.match(r'\[([^]]+)\] \[([^]]+)\]', line)
            if not match:
                continue
            identity = match[2].split('|')[0]
            for index, marker in enumerate(names):
                if marker in line:
                    events[index].add(identity)
                    requests += index == 0
        attempted, successful, rejected = events
        if len(set.union(*events)) > count or not (successful | rejected) <= attempted:
            raise ValueError('UE outcomes do not match the observed requests')
        result.update({procedure+'_attempted_ues': len(attempted), procedure+'_successful_ues': len(successful),
                       procedure+'_rejected_ues': len(rejected), procedure+'_pending_ues': len(attempted - successful - rejected),
                       procedure+'_request_messages': requests})
        if attempted:
            result[procedure+'_success_percent'] = 100 * len(successful) / len(attempted)
    return result


def procedure_metrics(text, count):
    events = {}
    markers = {"registration_start": "Sending Initial Registration",
               "registration_end": "Initial Registration is successful",
               "session_start": "Sending PDU Session Establishment Request",
               "session_end": "PDU Session establishment is successful"}
    for line in text.splitlines():
        match = re.match(r"\[([^]]+)\] \[([^]]+)\]", line)
        if not match:
            continue
        for name, marker in markers.items():
            if marker in line:
                identity = match[2].split("|")[0]
                events.setdefault(identity, {}).setdefault(name, datetime.fromisoformat(match[1]).timestamp())
    if len(events) != count or any(set(row) != set(markers) for row in events.values()):
        raise ValueError("per-UE procedure events incomplete")
    result = {}
    for procedure in ("registration", "session"):
        values = [(r[f"{procedure}_end"] - r[f"{procedure}_start"]) * 1000 for r in events.values()]
        if min(values) < 0:
            raise ValueError("procedure timestamps reversed")
        result.update({f"{procedure}_p{p}_ms": percentile(values, p / 100) for p in (50, 95, 99)})
        window = max(r[f"{procedure}_end"] for r in events.values()) - min(r[f"{procedure}_start"] for r in events.values())
        if window <= 0:
            raise ValueError("procedure timing below log resolution")
        result[f"{procedure}_per_second"] = count / window
    return result


def generator_headroom(samples, role, physical_capacity, assigned_cpus=None):
    """Check each thread and the total load against the assigned physical CPUs."""
    if physical_capacity <= 0:
        raise ValueError("Physical CPU capacity must be positive")
    peak_thread = peak_allocation = 0.0
    peak_cpu = peak_physical_allocation = 0.0
    physical_windows = 0
    windows = 0
    times = [sample['time'] for sample in samples]
    for i, a in enumerate(samples):
        j = bisect.bisect_left(times, a['time'] + 1, lo=i + 1)
        if j == len(samples):
            break
        b = samples[j]
        elapsed = b['time'] - a['time']
        measured_roles = (role, 'traffic') if role == 'ue' else (role,)
        before = {f'{r}:{t}':v for r in measured_roles for t,v in a.get('threads', {}).get(r, {}).items()}
        after = {f'{r}:{t}':v for r in measured_roles for t,v in b.get('threads', {}).get(r, {}).items()}
        if not after or role not in a.get("threads", {}) or role not in a.get("roles", {}) or role not in b.get("roles", {}):
            continue
        windows += 1
        peak_thread = max(peak_thread, max((100*(after[t]-before.get(t,0))/elapsed for t in after), default=0))
        used = sum(b['roles'].get(r, {}).get('cpu', 0) - a['roles'].get(r, {}).get('cpu', 0) for r in measured_roles)
        peak_allocation = max(peak_allocation, 100*used/elapsed/physical_capacity)
        if assigned_cpus:
            percentages=[]
            for cpu in assigned_cpus:
                first=a.get('cpu_times',{}).get(str(cpu));last=b.get('cpu_times',{}).get(str(cpu))
                if first is None or last is None:break
                delta=[y-x for x,y in zip(first,last)]
                if len(delta)!=8 or sum(delta)<=0:break
                percentages.append(100*sum(delta[i] for i in (0,1,2,5,6))/sum(delta))
            if len(percentages)==len(assigned_cpus):
                physical_windows+=1;peak_cpu=max(peak_cpu,max(percentages));peak_physical_allocation=max(peak_physical_allocation,sum(percentages)/len(percentages))
    physical_ok=assigned_cpus is None or (physical_windows==windows and physical_windows>0 and peak_cpu<=90 and peak_physical_allocation<=80)
    return {"passed": windows > 0 and peak_thread <= 90 and peak_allocation <= 80 and physical_ok,
            "windows": windows, "physical_capacity": physical_capacity,
            "peak_thread_percent": peak_thread, "peak_allocation_percent": peak_allocation,
            'physical_windows':physical_windows,'peak_cpu_percent':peak_cpu,'peak_physical_allocation_percent':peak_physical_allocation}


def generator_saturated(samples, role, physical_capacity=None, assigned_cpus=None):
    if physical_capacity is not None:
        return not generator_headroom(samples, role, physical_capacity, assigned_cpus)["passed"]
    for a, b in zip(samples, samples[10:]):
        elapsed = b["time"] - a["time"]
        if elapsed >= 1:
            before, after = [s.get("threads", {role: {"aggregate": s["roles"][role]["cpu"]}})[role] for s in (a, b)]
            if any((after[tid] - before[tid]) / elapsed > .9 for tid in before.keys() & after.keys()):
                return True
    return False


def prepare(args):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("prepare requires root on Linux")
    if any(not 1 <= n <= 253 for n in args.ue_counts):
        raise ValueError("this lab's IPv4 subnet supports 1 through 253 UEs")
    number(args.max_host_busy_percent, "maximum host busy percent", 1, 95)
    tools = ("ip", "ss", "sysctl", "ping", "tcpdump", "tshark", "podman", "iptables", "taskset", "ethtool", "tc")
    for tool in tools:
        if not shutil.which(tool):
            raise ValueError(f"install {tool} before prepare")
    extension=module(args.extension.resolve(),'lab_extension') if args.extension else None
    extension_config=read(args.extension_config) if args.extension_config else {}
    added=extension.prepare(extension_config) if extension else {'paths':[],'identities':{},'cores':[],'radios':[],'config':{}}
    ueran = args.ueransim.resolve()
    available=['open5gs']+(['free5gc'] if args.free5gc else [])+(['ella'] if args.ella_bin else [])+(['qcore'] if args.qcore_bin else [])+added['cores']
    cores=args.cores or available
    if len(set(cores))!=len(cores) or set(cores)-set(available):raise ValueError('unknown or unavailable core adapter')
    service=service_profile(vars(args))
    iperf=Path(args.iperf_bin or shutil.which('iperf3') or '').resolve()
    probe_contract=None
    if args.iperf_build_receipt:
        receipt=read(args.iperf_build_receipt)
        if receipt.get('instrumentation')!='traffic-phase-v1' or receipt.get('binary_sha256')!=digest(iperf) or receipt.get('patch_sha256')!=digest(ROOT/'patches/iperf-3.16-phase-clock.patch'):
            raise ValueError('traffic generator build receipt differs from its binary or clock patch')
        if {c.get('mode') for c in receipt.get('checks',[]) if c.get('passed')}!={'tcp_ul','tcp_dl','udp_ul','udp_dl'}:raise ValueError('traffic generator checks are incomplete')
        probe_contract='timer-and-ue-pcap-v1'
    runtime_environment=read(args.runtime_environment) if args.runtime_environment else {'cores':{},'processes':{}}
    if not isinstance(runtime_environment,dict) or set(runtime_environment)-{'cores','processes'}:raise ValueError('runtime environment must declare cores and processes')
    for group in runtime_environment.values():
        if not isinstance(group,dict):raise ValueError('runtime environment group must be an object')
        for name,env in group.items():
            if not isinstance(env,dict) or any(not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',k) or not isinstance(v,str) or '\0' in v for k,v in env.items()):raise ValueError('invalid runtime environment entry')
    if set(cores)&{'ella','qcore'} and args.layout!='split':raise ValueError('Ella Core and QCore require the split namespace layout')
    if 'qcore' in cores and (args.cipher_algorithm!=0 or args.slice_sd!='000000' or args.provisioned_sqn!=32):
        raise ValueError('QCore requires NIA2/NEA0, slice SD 000000, and first challenge SQN 32')
    if args.user_plane=='linux-gtp':
        if args.layout!='split' or args.cipher_algorithm!=0 or not args.kernel_gtp_module:
            raise ValueError('Linux GTP needs split namespaces, NEA0, and the pinned generator module')
        import pyroute2
        kernel=Path(args.kernel_gtp_module).resolve()
        if command(['modinfo','-F','vermagic',kernel]).split()[0]!=os.uname().release:raise ValueError('generator module kernel version differs')
    allocations={k:getattr(args,k) for k in ('core_cpus','gnb_cpus','generator_cpus','observer_cpus')}
    if any(allocations.values()):
        if not all(allocations.values()):raise ValueError('declare every CPU role')
        assigned=[c for cpus in allocations.values() for c in cpus]
        if len(set(assigned))!=len(assigned) or not set(assigned)<=os.sched_getaffinity(0):raise ValueError('CPU roles must be disjoint and available')
    paths=[iperf]
    if args.iperf_build_receipt:paths += [args.iperf_build_receipt.resolve(),ROOT/'patches/iperf-3.16-phase-clock.patch',ROOT/'scripts/build-iperf.py']
    if args.runtime_environment:paths.append(args.runtime_environment.resolve())
    if args.user_plane=='linux-gtp':
        paths += [kernel]+sorted(Path(pyroute2.__file__).resolve().parent.rglob('*.py'))+sorted(Path(pyroute2.__file__).resolve().parent.rglob('*.so'))
    paths += [ueran / "build" / f"nr-{node}" for node in ("ue", "gnb")]
    if args.free5gc and "free5gc" in cores:
        import yaml
        paths += sorted(Path(yaml.__file__).resolve().parent.rglob("*.py")) + sorted(Path(yaml.__file__).resolve().parent.rglob("*.so"))
        paths += [args.free5gc / "bin" / nf for nf in FREE_NFS]
    if "open5gs" in cores:paths += [Path(args.open5gs_bin).resolve() / f"open5gs-{nf}d" for nf in OPEN_NFS]
    for name in ("ella","qcore"):
        if name in cores:paths.append(Path(getattr(args,name+"_bin")).resolve())
    paths += [Path(shutil.which(tool)).resolve() for tool in tools] + [Path(sys.executable).resolve()]
    for p in paths:
        if not p.is_file() or not os.access(p, os.X_OK):
            raise ValueError(f"build or install the executable: {p}")
    paths+=list(added['paths'])
    if args.extension:paths.append(args.extension.resolve())
    if args.extension_config:paths.append(args.extension_config.resolve())
    if args.free5gc and "free5gc" in cores:
        paths += [args.free5gc / "source/config" / (nf+"cfg.yaml") for nf in FREE_NFS] + list((args.free5gc / "source/cert").glob("*"))
        paths += [Path(command(["modinfo", "-n", "gtp5g"]).strip())]
    config_tool=ROOT/'open5gs_config.py'
    profile=ROOT/'configs/profile.toml'
    radio_configs=ROOT/'configs'
    for p in (config_tool, profile):
        if not p.is_file():
            raise ValueError(f"missing repository input: {p}")
    image_id = json.loads(command(["podman", "image", "inspect", args.mongo_image]))[0]["Id"] if set(cores)&{"open5gs","free5gc"} else None
    paths += [config_tool, profile, ROOT / "benchmark.py", Path(__file__).resolve()]
    paths += sorted((ROOT/"runner").glob("*.py"))+sorted((ROOT/"adapters").glob("*.py"))+sorted((ROOT/"adapters").glob("*.c"))
    paths+=list(radio_configs.glob('*.yaml'))
    # Pin runtime libraries as well as programs: an OS update changes the trial.
    libraries = set()
    for path in paths:
        if os.access(path, os.X_OK) and path.read_bytes()[:4] == b"\x7fELF":
            libraries.update(Path(p).resolve() for p in re.findall(r"(?:=>\s+|^\s*)(/\S+)", command(["ldd", path], check=False), re.M))
    paths += sorted(libraries)
    requirements_path=Path(args.requirements).resolve()
    requirements=read(requirements_path)
    if not requirements.get('requirements'):
        raise ValueError('3GPP requirement registry is empty')
    for row in requirements['requirements']:
        if row.get('feature') not in FEATURES or any(not row.get(k) for k in ('release','specification','clause','source','test')):
            raise ValueError('each required feature needs a 3GPP reference and test')
    paths.append(requirements_path)
    for path in paths:
        if not path.is_file():raise ValueError('missing runtime input: '+str(path))
    inputs = {str(p): digest(p) for p in paths}
    identity = {
        "open5gs": object_hash({"binaries": {p: v for p, v in inputs.items() if "/open5gs-" in p and p.endswith("d")}, "mongo": image_id}),
        "ueransim": object_hash({p: v for p, v in inputs.items() if "/build/nr-" in p}),
    }
    if args.free5gc and "free5gc" in cores:
        identity["free5gc"] = object_hash({p: v for p, v in inputs.items() if str(args.free5gc) in p or "gtp5g.ko" in p})
    for name in ('ella','qcore'):
        if name in cores:identity[name]=digest(Path(getattr(args,name+'_bin')).resolve())
    identity.update(added['identities'])
    args.output.mkdir(parents=True, exist_ok=False)
    lab = {'extension':str(args.extension.resolve()) if args.extension else None,'extension_config':added['config'], "ueransim": str(ueran), "radio_configs": str(radio_configs),
           "free5gc": str(args.free5gc.resolve()) if args.free5gc else None,
           "open5gs_bin": str(Path(args.open5gs_bin).resolve()), "mongo_image": image_id,
           "configuration_tool": str(config_tool), "profile": str(profile),
           "maximum_host_busy_percent": args.max_host_busy_percent,"method_version":2}
    if args.user_plane=='linux-gtp' and all(allocations.values()):
        lab['flow_hash_objects']=steering.prepare(args.output,[len(args.core_cpus),len(args.gnb_cpus),len(args.generator_cpus)])
        inputs.update({str(p.resolve()):digest(p) for p in args.output.iterdir() if p.is_file()})
    lab.update({'iperf3':str(iperf),'probe_window_contract':probe_contract,'runtime_environment':runtime_environment,'user_plane':args.user_plane,'kernel_gtp_module':str(Path(args.kernel_gtp_module).resolve()) if args.kernel_gtp_module else None,**allocations,'layout':args.layout,'cipher_algorithm':args.cipher_algorithm,'slice_sd':args.slice_sd,
                'provisioned_sqn':args.provisioned_sqn,'n2_ip':'10.3.0.2' if args.layout=='split' else '127.0.0.1',
                'n3_ip':'10.3.0.2' if args.layout=='split' else '127.0.0.1',
                'gnb_ip':'10.3.0.3' if args.layout=='split' else '127.0.0.2',
                'ella_bin':str(Path(args.ella_bin).resolve()) if args.ella_bin else None,
                'qcore_bin':str(Path(args.qcore_bin).resolve()) if args.qcore_bin else None})
    if all(allocations.values()):
        lab['generator_physical_capacity']={'gnb':len(args.gnb_cpus),'ue':len(args.generator_cpus)}
    write(args.output / "lab.json", lab)
    inputs[str((args.output / "lab.json").resolve())] = digest(args.output / "lab.json")
    candidates = [{"id": f"{c}-{r}", "core": c, "radio": r,
                   "identity": {"core": identity[c], "radio": identity[r]}}
                  for c in cores
                  for r in (['ueransim']+added['radios'])]
    candidates += [{**c,'id':c['id']+'-repeat'} for c in list(candidates)]
    plan = {"schema_version": 1,"method_version":2,'minimum_repetitions':args.repetitions,
            'required_features':[r['feature'] for r in requirements['requirements']],'requirements':requirements,
            "mode": "live", "seed": args.seed, "repetitions": balanced_repetitions(len(candidates),args.repetitions),
            "timeout_seconds": args.timeout, "equivalence_percent": args.equivalence_percent,
            "protocol": {"ue_counts": args.ue_counts, "duration_seconds": args.seconds,
                "warmup_seconds": args.warmup, "packet_bytes": args.packet_bytes,
                "offered_mbps_per_ue": args.mbps, "security": service["security"], "plmn": "00101",
                "slice": service["slice"], "dnn": "internet", "session_ambr_mbps": 1000,
                "ambr_scope": "subscription configuration and N2 signalling target; NAS rate conformance and enforcement are untested",
                "transport": "IPv4", "probe_window_contract":probe_contract,"user_plane":args.user_plane,"path": "UE TUN -> gNB -> N3 -> UPF TUN -> N6 veth -> receiver",
                "n6_nat": True,
                "network_functions": {"open5gs": OPEN_NFS, "free5gc": FREE_NFS},
                "discovery": "direct NRF; Open5GS requires BSF for its PCF session path",
                "build": "release", "traffic": "one simultaneous client per UE; TCP and UDP in both directions",
                "attach_boundary": "UE process start to all UE IPv4 TUN interfaces ready; includes radio setup",
                "resources": "process CPU delta and sampled RSS/PSS sums; core includes MongoDB; radio has separate UE/gNB values",
                "resource_sample_seconds": 0.1, "maximum_cpu_steal_percent": 0.5,
                "sbi_authorization": "upstream configuration preserved; authorization conformance must be verified",
                "startup_boundary": "core launch through UE IPv4 setup and at least one external N6 reply per UE; three initial probes per UE; includes provisioning, radio setup and routing; binaries and images already cached"},
            "driver": [sys.executable, str(Path(__file__).resolve()), "trial", str((args.output / "lab.json").resolve())],
            "cwd": str(args.output.resolve()), "inputs": inputs, "candidates": candidates}
    validate_plan(plan)
    write(args.output / "plan.json", plan)
    print(args.output / "plan.json")


class Lab:
    def __init__(self, config, output):
        self.config, self.output = config, output
        self.extension=module(Path(config['extension']),'lab_extension') if config.get('extension') else None
        if self.extension:
            extra=config['extension_config']
            if any(key in self.config and self.config[key]!=value for key,value in extra.items()):raise ValueError('external adapter cannot replace a shared lab setting')
            self.config.update(extra)
        self.prefix = f"b5g-{os.getpid()}"
        self.core_ns, self.dn_ns, self.ue_ns = [f"{self.prefix}-{s}" for s in ("core", "dn", "ue")]
        self.namespaces, self.processes = [], []
        self.mongo = None
        self.mongo_volumes = []
        self.samples = []
        self.commands = {}
        self.critical_pids=set()
        self.metric_notes={}
        self.monitor_ready = threading.Event()
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.owns_gtp_module=False

    def ns(self, namespace, args, **kwargs):
        return command(["ip", "netns", "exec", namespace, *args], **kwargs)

    def start(self, name, args, namespace=None, env=None, role=None, required=None):
        runtime=self.config.get('runtime_environment',{})
        env={**(env or {}),**(runtime.get('cores',{}).get(self.config.get('candidate'),{}) if role=='core' else {}),
             **runtime.get('processes',{}).get(name,{})}
        if self.extension and hasattr(self.extension,'configure_start'):
            args,env=self.extension.configure_start(self, name, args, env, role)
        cpus=self.config.get('core_cpus' if role=='core' else 'gnb_cpus' if role=='gnb' else 'generator_cpus' if role in ('ue','traffic') else 'observer_cpus')
        if cpus:args=['taskset','-c',','.join(map(str,cpus)),*map(str,args)]
        if role == "ue" and self.config.get("enable_radio_commands"):
            args = [a for a in args if a != "-l"]
        argv = ["ip", "netns", "exec", namespace or self.core_ns, *map(str, args)]
        base_env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','TMPDIR')}
        with (self.output / f"{name}.log").open("wb") as handle:
            p = subprocess.Popen(argv, env={**base_env, "RUST_LOG": "info", "NO_COLOR": "1", **(env or {})},
                                 stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((p, role, name))
        if required is True or (required is None and role in ('core','ue','gnb')):self.critical_pids.add(p.pid)
        self.commands[name] = (list(args), namespace, env, role)
        return p

    def restart(self, name):
        original = next(p for p, _, label in self.processes if label == name)
        if original.poll() is None:
            raise ValueError("stop the named process before restart")
        args, namespace, env, role = self.commands[name]
        self.processes = [row for row in self.processes if row[0] is not original]
        return self.start(name + "-restart", args, namespace=namespace, env=env, role=role)

    def setup(self):
        if self.config.get('user_plane')=='linux-gtp':
            module_path=self.config['kernel_gtp_module']
            wanted=command(['modinfo','-F','srcversion',module_path]).strip()
            loaded=Path('/sys/module/gtp/srcversion')
            if loaded.exists():
                if loaded.read_text().strip()!=wanted:raise ValueError('a different GTP module is already loaded')
            else:
                command(['insmod',module_path]);self.owns_gtp_module=True
                if loaded.read_text().strip()!=wanted:raise ValueError('loaded generator module hash differs')
        if any(line.startswith("b5g-") for line in command(["ip", "netns", "list"]).splitlines()):
            raise RuntimeError("a benchmark namespace remains; inspect its cleanup receipt first")
        for ns in (self.core_ns, self.dn_ns, self.ue_ns):
            command(["ip", "netns", "add", ns])
            self.namespaces.append(ns)
            self.ns(ns, ["ip", "link", "set", "lo", "up"])
        self.ns(self.core_ns, ["ip", "link", "add", "dn0", "type", "veth", "peer", "name", "dn1"])
        self.ns(self.core_ns, ["ip", "link", "set", "dn1", "netns", self.dn_ns])
        for ns, name, address in ((self.core_ns, "dn0", "192.0.2.2/24"), (self.dn_ns, "dn1", "192.0.2.1/24")):
            self.ns(ns, ["ip", "addr", "add", address, "dev", name])
            self.ns(ns, ["ip", "link", "set", name, "up"])
        self.ns(self.dn_ns, ["ip", "route", "add", "10.45.0.0/24", "via", "192.0.2.2"])
        self.ns(self.core_ns, ["sysctl", "-w", "net.ipv4.ip_forward=1"])
        self.ns(self.core_ns, ["sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"])

        if self.config.get('layout')=='split':
            self.radio_ns=self.prefix+'-ran'
            command(['ip','netns','add',self.radio_ns]);self.namespaces.append(self.radio_ns)
            self.ns(self.radio_ns,['ip','link','set','lo','up'])
            self.ns(self.core_ns,['ip','link','add','n3','type','veth','peer','name','ran0'])
            self.ns(self.core_ns,['ip','link','set','ran0','netns',self.radio_ns])
            for ns,device,address in [(self.core_ns,'n3','10.3.0.2/24'),(self.radio_ns,'ran0','10.3.0.3/24')]:
                self.ns(ns,['ip','addr','add',address,'dev',device]);self.ns(ns,['ip','link','set',device,'up'])
            for ns,devices in [(self.core_ns,['n3','dn0']),(self.radio_ns,['ran0']),(self.dn_ns,['dn1'])]:
                for device in devices:self.ns(ns,['ethtool','-K',device,'rx','off','tx','off','tso','off','gso','off','gro','off'])
            if self.config.get('core_cpus'):
                rows=[]
                for ns,device,receive,peer in [(self.core_ns,'n3','core_cpus','gnb_cpus'),(self.core_ns,'dn0','core_cpus','generator_cpus'),(self.radio_ns,'ran0','gnb_cpus','core_cpus'),(self.dn_ns,'dn1','generator_cpus','core_cpus')]:
                    rows.append(steering.apply(self,ns,device,self.config[receive],len(self.config[peer])))
                write(self.output/'receive-steering.json',rows)
        else:self.radio_ns=self.core_ns

    def wait(self, predicate, seconds=45):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            dead = [name for p, role, name in self.processes if p.pid in self.critical_pids and p.poll() is not None]
            if dead:
                raise RuntimeError(f"process stopped: {dead}")
            if predicate():
                return
            time.sleep(.05)
        raise TimeoutError("readiness deadline expired")

    def start_open5gs(self,count):
        return open5gs.start(self,count)

    def start_core(self,core,count):
        if core=='open5gs':return self.start_open5gs(count)
        if core=='free5gc':return self.start_free5gc(count)
        if core=='ella':return ella.start(self,count)
        if core=='qcore':return qcore.start(self,count)
        if not self.extension:raise ValueError('unknown core adapter')
        return self.extension.start_core(self,core,count)

    def start_database(self):
        self.mongo = f"{self.prefix}-mongo"
        command(["podman", "run", "-d", "--name", self.mongo, "--network", f"ns:/run/netns/{self.core_ns}",
                 *(["--cpuset-cpus",",".join(map(str,self.config["core_cpus"]))] if self.config.get("core_cpus") else []), self.config["mongo_image"], "--bind_ip", "127.0.0.1"], timeout=90)
        container = json.loads(command(["podman", "inspect", self.mongo]))[0]
        self.mongo_volumes = [m["Name"] for m in container["Mounts"] if m["Type"] == "volume"]
        write(self.output / "database-volumes.json", {"container": self.mongo, "volumes": self.mongo_volumes})
        self.wait(lambda: "27017" in self.ns(self.core_ns, ["ss", "-lntH"]))

    def provision_database(self, database, script):
        path = self.output / "subscribers.js"
        path.write_text(script)
        command(["podman", "cp", path, f"{self.mongo}:/tmp/subscribers.js"])
        command(["podman", "exec", self.mongo, "mongosh", "--quiet", database, "/tmp/subscribers.js"])

    def start_free5gc(self,count):
        return free5gc.start(self,count)

    def radio(self, kind, count, core="open5gs"):
        if kind == "ueransim":
            elapsed_ms,interfaces=ueransim.start(self,count,core)
        else:
            if not self.extension:raise ValueError('unknown radio adapter')
            elapsed_ms,interfaces=self.extension.start_radio(self,kind,count,core)
        # Both simulators reach the same timed boundary before this shared setup.
        before_move = json.loads(self.ns(self.radio_ns, ["ip", "-j", "-4", "addr"]))
        assigned = {r["ifname"]: next((a["local"] for a in r["addr_info"] if a["family"] == "inet"), None) for r in before_move}
        for interface in interfaces:
            self.ns(self.radio_ns, ["ip", "link", "set", interface, "netns", self.ue_ns])
            self.ns(self.ue_ns, ["ip", "addr", "replace", f"{assigned[interface]}/32", "dev", interface])
            self.ns(self.ue_ns, ["ip", "link", "set", interface, "up"])
        addresses = json.loads(self.ns(self.ue_ns, ["ip", "-j", "-4", "addr"]))
        ips = {r["ifname"]: next((a["local"] for a in r["addr_info"] if a["family"] == "inet"), None) for r in addresses}
        if len({ips.get(i) for i in interfaces} - {None}) != count:
            raise ValueError("UE interfaces do not have distinct IPv4 addresses")
        for n, interface in enumerate(interfaces, 1):
            self.ns(self.ue_ns, ["ip", "rule", "add", "from", ips[interface], "table", str(UE_ROUTE_TABLE_BASE + n)])
            self.ns(self.ue_ns, ["ip", "route", "add", "default", "dev", interface, "table", str(UE_ROUTE_TABLE_BASE + n)])
        write(self.output / "interfaces.json", {i: ips[i] for i in interfaces})
        write(self.output / "routes.json", {ns: json.loads(self.ns(ns, ["ip", "-j", "route", "show", "table", "all"])) for ns in (self.core_ns, self.ue_ns, self.dn_ns)})
        return elapsed_ms, [(i, ips[i]) for i in interfaces]

    def prepare_user_plane(self,interfaces,capture):
        if self.config.get('user_plane')!='linux-gtp':return None
        tunnels=[]
        def observe():
            nonlocal tunnels
            os.killpg(capture.pid,signal.SIGUSR2);time.sleep(.2)
            pdml=command(['tshark','-r',self.output/'n2-n3.pcap','-o','nas-5gs.null_decipher:TRUE','-Y','ngap','-T','pdml'])
            (self.output/'live-tunnels.pdml').write_text(pdml)
            try:tunnels=kernel_gtp.tunnels_from_pdml(pdml,[ip for _,ip in interfaces])
            except ValueError as error:
                if str(error)=='Live tunnel state incomplete':return False
                raise
            write(self.output/'signalled-session-ambr.json',session_ambr.verify(session_ambr.rows_from_pdml(pdml),len(interfaces),1000000000))
            write(self.output/'signalled-ngap-ambr.json',session_ambr.verify_ngap_limits(pdml,1000000000))
            return True
        self.wait(observe,seconds=10);write(self.output/'live-tunnels.json',tunnels)
        if not self.config.get('flow_hash_objects'):self.ns(self.radio_ns,['tc','qdisc','add','dev','ran0','clsact'])
        self.ns(self.radio_ns,['tc','filter','add','dev','ran0','ingress','protocol','ip','flower','ip_proto','udp','src_ip','10.3.0.2','dst_port','2152','action','pedit','ex','munge','udp','dport','set','32152','pipe','action','csum','udp'])
        endpoint=self.start('kernel-gtp-endpoint',[sys.executable,ROOT/'adapters/kernel_gtp.py',self.output/'live-tunnels.json'],namespace=self.radio_ns,role='traffic',required=True)
        self.wait(lambda:endpoint.poll() is not None or 'GTP_ENDPOINT_READY' in (self.output/'kernel-gtp-endpoint.log').read_text())
        if endpoint.poll() is not None:raise ValueError('Linux GTP endpoint failed')
        self.ns(self.radio_ns,['ip','link','set','benchgtp','netns',self.ue_ns])
        self.ns(self.ue_ns,['ip','link','set','benchgtp','up'])
        self.ns(self.ue_ns,['sysctl','-w','net.ipv4.conf.benchgtp.rp_filter=0'])
        if self.config.get('generator_cpus'):
            write(self.output/'kernel-gtp-inner-steering.json',steering.apply(self,self.ue_ns,'benchgtp',self.config['generator_cpus'],len(self.config['core_cpus'])))
        for n,_ in enumerate(interfaces,1):self.ns(self.ue_ns,['ip','route','replace','default','dev','benchgtp','table',str(UE_ROUTE_TABLE_BASE+n)])
        write(self.output/'kernel-gtp-before.json',json.loads(self.ns(self.ue_ns,['ip','-s','-j','link','show','benchgtp'])))
        return tunnels

    def pids(self):
        result = {p.pid: role for p, role, name in self.processes if role and p.poll() is None}
        if self.mongo:
            pid = command(["podman", "inspect", "--format", "{{.State.Pid}}", self.mongo], check=False).strip()
            if pid.isdigit() and int(pid):
                result[int(pid)] = "core"
        return result

    def start_monitor(self):
        if self.monitor_thread is None:
            self.monitor_thread = threading.Thread(target=self.monitor)
            self.monitor_thread.start()
        if not self.monitor_ready.wait(timeout=5) or not self.samples or 'error' in self.samples[0]:
            raise RuntimeError('Resource monitor did not start')

    def monitor(self):
        ticks_per_second, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        cpu_totals, thread_totals = {}, {}
        pids = {pid: role for pid, role in self.pids().items() if role != 'traffic'}
        while not self.monitor_stop.is_set():
            row = {"time": time.monotonic(), "roles": {role: {"cpu": 0, "rss": 0, "pss": 0} for role in ("core", "ue", "gnb", "traffic")}}
            row["threads"] = {role: {} for role in ("core", "ue", "gnb", "traffic")}
            try:
                pids.update({p.pid: role for p, role, _ in list(self.processes) if role == 'ue'})
                live_traffic = {p.pid: role for p, role, _ in list(self.processes) if role == 'traffic' and p.poll() is None}
                for pid, role in {**pids, **live_traffic}.items():
                    try:
                        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                        cpu_totals[(pid, role)] = (int(stat[11]) + int(stat[12])) / ticks_per_second
                        row["roles"][role]["rss"] += int(stat[21]) * page
                        memory = Path(f"/proc/{pid}/smaps_rollup").read_text()
                        row["roles"][role]["pss"] += int(re.search(r"^Pss:\s+(\d+)", memory, re.M)[1]) * 1024
                        if role in ("ue", "gnb", "traffic"):
                            for task in Path(f"/proc/{pid}/task").iterdir():
                                try:
                                    thread = (task / "stat").read_text().rsplit(") ", 1)[1].split()
                                    thread_totals[(role, task.name)] = (int(thread[11]) + int(thread[12])) / ticks_per_second
                                except FileNotFoundError:
                                    pass
                    except OSError as error:
                        if role != 'traffic' or error.errno not in (errno.ENOENT, errno.ESRCH):raise
                # Keep completed traffic counters so a normal exit cannot reduce total CPU.
                for (_, role), cpu in cpu_totals.items():row['roles'][role]['cpu'] += cpu
                for (role, tid), cpu in thread_totals.items():row['threads'][role][tid] = cpu
                cpu_lines=Path('/proc/stat').read_text().splitlines()
                row['host']=list(map(int,cpu_lines[0].split()[1:9]))
                row['cpu_times']={line.split()[0][3:]:list(map(int,line.split()[1:9])) for line in cpu_lines if re.match(r'^cpu\d+\s',line)}
                memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
                row['host_memory_used_bytes']=memory['MemTotal']-memory['MemAvailable']
                self.samples.append(row)
                self.monitor_ready.set()
            except (OSError, ValueError):
                self.samples.append({"error": "a measured process disappeared"})
                self.monitor_ready.set()
                return
            self.monitor_stop.wait(self.config.get("resource_sample_seconds", .1))

    def traffic(self, interfaces, protocol):
        metrics, quality, metric_errors = {}, [], {}
        self.config["resource_sample_seconds"] = protocol.get("resource_sample_seconds", .1)
        self.start_monitor()
        idle_start = len(self.samples)
        def probes(label, count, interval):
            clients = [self.start(f"{label}-ping-{n}", ["ping", "-D", "-n", "-c", str(count), "-i", str(interval), "-W", "2", "-I", ip, "192.0.2.1"], namespace=self.ue_ns, role='traffic')
                       for n, (_, ip) in enumerate(interfaces)]
            return clients

        def collect_probes(label, clients):
            values, sent, received = [], 0, 0
            for n, p in enumerate(clients):
                p.wait(timeout=protocol["duration_seconds"] + protocol["warmup_seconds"] + 10)
                text = (self.output / f"{label}-ping-{n}.log").read_text()
                row = ping_metrics(text)
                sent += row["sent"]; received += row["received"]
                values.extend(float(x) for x in re.findall(r"time[=<]([0-9.]+) ms", text))
            metrics[f"{label}_ping_loss_percent" if label != "idle" else "ping_loss_percent"] = 100 * (sent - received) / sent
            for p in (50, 95, 99):
                name = f"{label}_rtt_p{p}_ms" if label != "idle" else f"rtt_p{p}_ms"
                if values:
                    metrics[name] = percentile(values, p / 100)
                else:
                    metric_errors[name] = "no probe replies"

        ready_clients = probes("ready", 3, .05)
        ready_rows = []
        for n, p in enumerate(ready_clients):
            p.wait(timeout=10)
            ready_rows.append(ping_metrics((self.output / f"ready-ping-{n}.log").read_text()))
        write(self.output / "traffic-readiness.json", ready_rows)
        if all(row["received"] > 0 for row in ready_rows) and hasattr(self, "startup_started"):
            metrics["stack_traffic_ready_ms"] = (time.monotonic() - self.startup_started) * 1000
        else:
            metric_errors["stack_traffic_ready_ms"] = "at least one UE has no initial external reply"
            quality.append("at least one UE has no initial external N6 reply")
        for n, _ in enumerate(interfaces):
            self.start(f"server-{n}", [self.config.get("iperf3","iperf3"), "-s", "-B", "192.0.2.1", "-p", str(5201 + n)], namespace=self.dn_ns, role='traffic', required=True)
        collect_probes("idle", probes("idle", 20, .05))
        idle_samples = self.samples[idle_start:]
        if idle_samples:
            for role in ("core", "ue", "gnb"):
                for name in ("rss", "pss"):
                    metrics[f"{role}_idle_{name}_bytes"] = percentile([s["roles"][role][name] for s in idle_samples if "roles" in s], .5)
            metrics['host_idle_memory_used_bytes']=percentile([s['host_memory_used_bytes'] for s in idle_samples if 'host_memory_used_bytes' in s],.5)
        for mode in protocol.get("traffic_modes", ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl")):
            phase_start = len(self.samples)
            phase_started = time.monotonic()
            phase_epoch = time.time()
            processes = []
            timer_paths=[]
            probe_capture=None
            for n, (interface, ip) in enumerate(interfaces):
                args = [self.config.get("iperf3","iperf3"), "-c", "192.0.2.1", "-p", str(5201 + n), "-B", ip,
                        "-t", str(protocol["duration_seconds"]), "-O", str(protocol["warmup_seconds"]), "-J", "--get-server-output",
                        "-b", f"{protocol['offered_mbps_per_ue']}M"]
                if mode.startswith("udp"):
                    args += ["-u", "-l", str(protocol["packet_bytes"])]
                else:
                    args += ["-M", str(protocol["packet_bytes"]), "-l", "131072"]
                if mode.endswith("dl"):
                    args += ["-R"]
                timer_path=self.output/f'{mode}-{n}-phase.jsonl';timer_paths.append(timer_path)
                processes.append(self.start(f"{mode}-{n}", args, namespace=self.ue_ns, role='traffic',env={'BENCH_PHASE_EVENTS':str(timer_path)}))
            if self.config.get('probe_window_contract'):
                def warm():
                    try:return all(any(json.loads(line)['event']=='warmup_end' for line in path.read_text().splitlines()) for path in timer_paths)
                    except (OSError,ValueError):return False
                self.wait(warm,seconds=protocol['warmup_seconds']+15)
                probe_capture=self.start(mode+'-probe-capture',['tcpdump','-i','any','-s','128','-U','-w',self.output/(mode+'-probes.pcap'),'icmp'],namespace=self.ue_ns)
                self.wait(lambda:probe_capture.poll() is not None or 'listening on' in (self.output/(mode+'-probe-capture.log')).read_text())
                if probe_capture.poll() is not None:raise ValueError('probe capture failed')
                time.sleep(.1)
            else:time.sleep(protocol["warmup_seconds"])
            interval = protocol.get("probe_interval_seconds", .2)
            loaded_probes = probes(mode, max(1, int((protocol["duration_seconds"]-2) / interval)), interval)
            try:
                for p in processes:
                    if p.wait(timeout=protocol["duration_seconds"] + protocol["warmup_seconds"] + 20):
                        raise ValueError(f"{mode} client failed; see its log")
                phase_ended = time.monotonic()
                rates, lost, packets, bytes_received, jitters, retransmits = [], 0, 0, 0, [], 0
                for n in range(len(interfaces)):
                    data = read(self.output / f"{mode}-{n}.log")
                    rate, loss = receiver_metrics(data, mode.startswith("udp"))
                    rates.append(rate)
                    bytes_received += data["end"]["sum_received"]["bytes"]
                    if mode.startswith("tcp"):
                        sender = data["end"].get("sum_sent", {})
                        if "retransmits" not in sender:
                            raise ValueError("TCP sender retransmit counter missing")
                        retransmits += sender["retransmits"]
                    if loss is not None:
                        row = data["end"]["sum_received"]
                        packets += row["packets"]
                        lost += row["lost_packets"]
                        jitters.append((row["jitter_ms"], row["packets"]))
                if mode.startswith("udp") and packets <= 0:
                    raise ValueError("UDP receiver packet count is zero")
                metrics[f"{mode}_mbps"] = sum(rates)
                if mode.startswith("udp"):
                    metrics[f"{mode}_loss_percent"] = 100 * lost / packets
                    metrics[f"{mode}_jitter_ms"] = sum(j * p for j, p in jitters) / packets
                else:
                    metrics[f"{mode}_retransmits"] = retransmits
                phase = [s for s in self.samples[phase_start:] if "roles" in s and phase_started + protocol["warmup_seconds"] <= s["time"] <= phase_ended]
                write(self.output / f"{mode}-window.json", {"launch_epoch": phase_epoch, "launch_monotonic": phase_started, "warmup_seconds": protocol["warmup_seconds"], "end_monotonic": phase_ended, "samples": len(phase), "boundary": "shared aggregate window; clients launch in sequence"})
                if len(phase) < 2 or bytes_received <= 0:
                    raise ValueError("CPU efficiency samples missing")
                host_delta=[b-a for a,b in zip(phase[0]['host'],phase[-1]['host'])]
                host_cpu=sum(host_delta[i] for i in (0,1,2,5,6))/os.sysconf('SC_CLK_TCK')
                metrics[f'{mode}_host_cpu_seconds_per_gbit']=host_cpu/(bytes_received*8/1e9)
                metrics[f"{mode}_core_cpu_seconds_per_gbit"] = (phase[-1]["roles"]["core"]["cpu"] - phase[0]["roles"]["core"]["cpu"]) / (bytes_received * 8 / 1e9)
            except (ValueError, KeyError, subprocess.TimeoutExpired) as error:
                metric_errors[f"{mode}_mbps"] = str(error)
                if mode.startswith("udp"):
                    metric_errors[f"{mode}_loss_percent"] = str(error)
            finally:
                try:
                    collect_probes(mode, loaded_probes)
                    if probe_capture is None:
                        for name in metrics:
                            if name.startswith(mode+'_rtt_') or name==mode+'_ping_loss_percent':self.metric_notes[name]='traffic timer and UE packet boundary are unverified'
                    else:
                        try:
                            os.killpg(probe_capture.pid,signal.SIGINT);probe_capture.wait(timeout=10)
                            capture_log=(self.output/(mode+'-probe-capture.log')).read_text()
                            if not re.search(r'(?m)^0 packets dropped by kernel$',capture_log):raise ValueError('probe capture drop count is missing or nonzero')
                            fields=command(['tshark','-r',self.output/(mode+'-probes.pcap'),'-Y','icmp.type == 8 || icmp.type == 0','-T','fields','-E','occurrence=f',*sum((['-e',field] for field in ['frame.time_epoch','ip.src','ip.dst','icmp.type','icmp.ident','icmp.seq']),[])])
                            (self.output/(mode+'-probe-fields.tsv')).write_text(fields)
                            counts={ip:ping_metrics((self.output/f'{mode}-ping-{n}.log').read_text()) for n,(_,ip) in enumerate(interfaces)}
                            events=[[json.loads(line) for line in path.read_text().splitlines()] for path in timer_paths]
                            proof=probe_window.verify(events,fields,counts);write(self.output/(mode+'-probe-window.json'),proof)
                            for quantile in (50,95,99):
                                name=f'{mode}_rtt_p{quantile}_ms'
                                if proof['rtts_ms']:metrics[name]=percentile(proof['rtts_ms'],quantile/100)
                                else:metric_errors[name]='no captured probe replies'
                        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
                            write(self.output/(mode+'-probe-window-error.json'),{'error':str(error)})
                            for name in metrics:
                                if name.startswith(mode+'_rtt_') or name==mode+'_ping_loss_percent':self.metric_notes[name]=str(error)
                except (ValueError, subprocess.TimeoutExpired) as error:
                    for name in [f"{mode}_ping_loss_percent", *[f"{mode}_rtt_p{p}_ms" for p in (50, 95, 99)]]:
                        metric_errors[name] = str(error)
                for p in processes:
                    stop_group(p)
        if self.samples and 'time' in self.samples[0]:
            time.sleep(max(0,1.2-(time.monotonic()-self.samples[0]['time'])))
        self.monitor_stop.set()
        self.monitor_thread.join()
        if self.config.get("generator_physical_capacity"):
            write(self.output / "generator-headroom.json", {role: generator_headroom(self.samples, role, capacity, self.config.get('gnb_cpus' if role=='gnb' else 'generator_cpus'))
                  for role, capacity in self.config["generator_physical_capacity"].items()})
        write(self.output / "resources.json", self.samples)
        if len(self.samples) < 2 or any("error" in s for s in self.samples):
            raise ValueError("resource samples incomplete")
        for role in ("core", "ue", "gnb"):
            if metric_errors:
                for name in ("cpu_seconds", "rss_peak_bytes", "pss_peak_bytes"):
                    metric_errors[f"{role}_{name}"] = "traffic did not complete; resource load is not comparable"
            else:
                metrics[f"{role}_cpu_seconds"] = self.samples[-1]["roles"][role]["cpu"] - self.samples[0]["roles"][role]["cpu"]
                metrics[f"{role}_rss_peak_bytes"] = max(s["roles"][role]["rss"] for s in self.samples)
                metrics[f"{role}_pss_peak_bytes"] = max(s["roles"][role]["pss"] for s in self.samples)
        metrics['host_memory_used_peak_bytes']=max(s['host_memory_used_bytes'] for s in self.samples if 'host_memory_used_bytes' in s)
        # This native lab shares one host. Reject attribution at saturation.
        before, after = self.samples[0]["host"], self.samples[-1]["host"]
        delta = [b - a for a, b in zip(before, after)]
        busy = 100 * (sum(delta) - delta[3] - delta[4]) / max(1, sum(delta))
        if busy > self.config["maximum_host_busy_percent"]:
            quality.append(f"shared host busy {busy:.1f}% exceeds {self.config['maximum_host_busy_percent']}%")
        steal = 100 * delta[7] / max(1, sum(delta))
        if steal > .5:
            quality.append(f"host CPU steal {steal:.3f}% exceeds 0.5%")
        for role in ("ue", "gnb"):
            if generator_saturated(self.samples, role, self.config.get('generator_physical_capacity', {}).get(role), self.config.get('gnb_cpus' if role=='gnb' else 'generator_cpus') if self.config.get('generator_physical_capacity') else None):
                quality.append(f"{role} generator CPU headroom failed: thread limit 90%, allocated physical capacity limit 80%")
        return metrics, quality, metric_errors

    def cleanup(self):
        write(self.output / "commands.json", {name: {"argv": list(map(str, args)), "namespace": namespace or self.core_ns, "role": role} for name, (args, namespace, env, role) in self.commands.items()})
        write(self.output / 'process-exits-before-cleanup.json', [{'name': name, 'role': role, 'pid': p.pid, 'returncode': p.poll()} for p, role, name in self.processes])
        for namespace in self.namespaces:
            (self.output / ('udp-counters-'+namespace+'.txt')).write_text(self.ns(namespace,['cat','/proc/net/snmp'],check=False))
            (self.output / ('udp-sockets-'+namespace+'.txt')).write_text(self.ns(namespace,['ss','-u','-a','-n','-m'],check=False))
        errors = []
        self.monitor_stop.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        for p, role, name in self.processes:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for p, role, name in reversed(self.processes):
            stop_group(p)
        if self.mongo:
            try:
                command(["podman", "rm", "-f", "--volumes", self.mongo])
            except Exception as error:
                errors.append(str(error))
        for ns in reversed(self.namespaces):
            try:
                remaining = command(["ip", "netns", "pids", ns]).split()
                for pid in remaining:
                    os.kill(int(pid), signal.SIGKILL)
                command(["ip", "netns", "del", ns])
            except Exception as error:
                errors.append(str(error))
        if any(ns in command(["ip", "netns", "list"]) for ns in self.namespaces):
            errors.append("namespace remains")
        if self.mongo and command(["podman", "ps", "-a", "--filter", f"name=^{self.mongo}$", "--format", "{{.Names}}" ]).strip():
            errors.append("database container remains")
        if self.mongo_volumes and set(self.mongo_volumes) & set(command(["podman", "volume", "ls", "--format", "{{.Name}}" ]).split()):
            errors.append("database volume remains")
        if self.owns_gtp_module:
            try:command(['rmmod','gtp']);self.owns_gtp_module=False
            except Exception as error:errors.append('generator module cleanup: '+str(error))
        write(self.output / "cleanup.json", {"ok": not errors, "errors": errors, "namespaces": self.namespaces,
                                             "volumes": self.mongo_volumes})
        return not errors


def trial(config_path):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("trial requires root on Linux")
    output = Path(os.environ["BENCH_RESULT_DIR"])
    plan = read(os.environ["BENCH_PLAN"])
    candidate = json.loads(os.environ["BENCH_CANDIDATE"])
    count = int(os.environ["BENCH_UE_COUNT"])
    config=read(config_path)
    config["candidate"]=candidate["core"]
    if config.get("observer_cpus"):os.sched_setaffinity(0,config["observer_cpus"])
    lab = Lab(config, output)
    result = {"kind": "live", "cleanup_ok": False, "quality_errors": [], "metrics": {},
              "features": {f: "untested" for f in FEATURES}, "evidence": []}
    def interrupted(signum, frame):
        raise InterruptedError(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        lab.setup()
        startup = time.monotonic()
        lab.startup_started = startup
        lab.start_core(candidate['core'],count)
        write(output / "listener-readiness.json", {"elapsed_ms": (time.monotonic() - startup) * 1000, "boundary": "implementation-specific listener and association checks; not a common startup metric"})
        capture = lab.start("capture", ["tcpdump", "-i", "any", "-s", "0", "-c", str(max(2000, count * 128)), "-U", "-w", output / "n2-n3.pcap", "sctp or udp port 2152"], namespace=lab.radio_ns)
        lab.wait(lambda: "listening on" in (output / "capture.log").read_text())
        attach, interfaces = lab.radio(candidate["radio"], count, candidate["core"])
        result["metrics"].update(attach_ready_ms=attach, attach_success_percent=100)
        selections=re.findall(r'Selected integrity\[(\d+)\] ciphering\[(\d+)\]',(output/'ue.log').read_text())
        expected=service_profile(lab.config)
        if len(selections)!=count or any(list(row)!=['2',str(expected['cipher_algorithm'])] for row in selections):
            raise ValueError('selected NAS algorithms differ from the declared profile')
        ue_log=(output/'ue.log').read_text()
        resync=bool(re.search(r'(?i)AUTS|synchroni[sz]ation failure|SQN.*(failure|out of)',ue_log))
        profile={k:plan['protocol'][k] for k in ('plmn','slice','dnn','security','session_ambr_mbps','transport','sbi_authorization')}
        result['comparison_state']={'service_profile':object_hash(profile),
            'authentication_profile':None if resync else object_hash({'initial_sqn':[subscriber_sqn(lab.config,i) for i in range(1,count+1)],'ue_state':'fresh','resynchronization':False}),
            'durability_profile':None,'radio_capability_profile':None,'resource_scope':'whole-host'}
        write(output/'comparison-state.json',result['comparison_state'])
        if candidate["radio"] == "ueransim":
            try:
                result["metrics"].update(procedure_metrics((output / "ue.log").read_text(), count))
            except ValueError as error:
                result.setdefault("metric_errors", {}).update({name: str(error) for name in METRICS if name.startswith(("registration_", "session_"))})
        for f in ("registration", "pdu_session"):
            result["features"][f] = "passed"
        result["features"]["distinct_ue_addresses"] = "passed" if count > 1 else "untested"
        tunnels=lab.prepare_user_plane(interfaces,capture)
        metrics, errors, metric_errors = lab.traffic(interfaces, plan["protocol"])
        result["metrics"].update(metrics)
        result.setdefault("metric_errors", {}).update(metric_errors)
        result["quality_errors"].extend(errors)
        result["metric_notes"]=lab.metric_notes
        for f in ("external_n6", "tcp", "udp"):
            result["features"][f] = "passed"
        for transport in ("tcp", "udp"):
            if any(f"{transport}_{d}_mbps" in metric_errors or metrics.get(f"{transport}_{d}_mbps", 0) == 0 for d in ("ul", "dl")):
                result["features"][transport] = "failed"
        if capture.poll() is None:
            os.killpg(capture.pid, signal.SIGINT)
        capture.wait(timeout=10)
        # Independent protocol dissector. Preserve captures for review.
        for name, display_filter in (("n2", "ngap"), ("n3_ul", f"gtp.message == 255 && ip.src == {lab.config.get('gnb_ip','127.0.0.2')}"), ("n3_dl", f"gtp.message == 255 && ip.dst == {lab.config.get('gnb_ip','127.0.0.2')}")):
            decoded = subprocess.run(["tshark", "-r", "-", "-Y", display_filter, "-T", "fields", "-e", "frame.number"],
                input=(output / "n2-n3.pcap").read_bytes(), capture_output=True, timeout=30)
            if decoded.returncode:
                raise ValueError(f"packet decode failed: {decoded.stderr.decode(errors='replace')[-1000:]}")
            data = decoded.stdout.decode()
            (output / f"{name}.txt").write_text(data)
            if not data.strip():
                raise ValueError(f"missing decoded {name} packets")
        result["features"]["n2_ngap"] = result["features"]["n3_gtpu"] = "passed"
        if tunnels is not None:
            wire=command(['tshark','-r',output/'n2-n3.pcap','-Y','gtp.message == 255 && ip.src == 10.3.0.3','-T','fields','-e','gtp.teid','-e','gtp.ext_hdr.pdu_ses_con.pdu_type','-e','gtp.ext_hdr.pdu_ses_con.qos_flow_id'])
            (output/'kernel-gtp-wire.txt').write_text(wire)
            write(output/'kernel-gtp-wire-proof.json',kernel_gtp.verify_wire_tunnels(wire,tunnels))
            counters=json.loads(lab.ns(lab.ue_ns,['ip','-s','-j','link','show','benchgtp']))
            write(output/'kernel-gtp-after.json',counters)
            if any(counters[0]['stats64']['rx'][key] for key in ['dropped','errors']):raise ValueError('Linux GTP generator receive drops')
        core_bins=[Path(lab.config['open5gs_bin'])/f'open5gs-{nf}d' for nf in OPEN_NFS] if candidate['core']=='open5gs' else [Path(lab.config['free5gc'])/'bin'/nf for nf in FREE_NFS] if candidate['core']=='free5gc' else []
        radio_bins={r:Path(lab.config['ueransim'])/'build'/f'nr-{r}' for r in ('ue','gnb')} if candidate['radio']=='ueransim' else {}
        if candidate['core'] in ('ella','qcore'):core_bins=[Path(lab.config[candidate['core']+'_bin'])]
        if lab.extension:
            external_core,external_radio=lab.extension.binaries(lab.config,candidate['core'],candidate['radio'])
            core_bins=core_bins or external_core;radio_bins=radio_bins or external_radio
        result['metrics']['core_binary_bytes']=sum(p.stat().st_size for p in core_bins)
        for role,p in radio_bins.items():result['metrics'][f'{role}_binary_bytes']=p.stat().st_size
    except Exception as error:
        result["quality_errors"].append(str(error))
    finally:
        # A second termination request must not interrupt cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if (output/'ue.log').exists() and candidate['radio']=='ueransim':
            try:result['metrics'].update(procedure_counts((output/'ue.log').read_text(),count))
            except ValueError as error:result['quality_errors'].append(str(error))
        result["cleanup_ok"] = lab.cleanup()
        result["features"]["cleanup"] = "passed" if result["cleanup_ok"] else "failed"
        result["evidence"] = sorted(str(p.relative_to(output)) for p in output.rglob("*") if p.is_file() and p.name not in ("driver.log", "measurement.json"))
        for name in METRICS.keys() - result["metrics"].keys():
            result.setdefault("metric_errors", {}).setdefault(name, "collector has no value at this boundary; see raw evidence")
        for name in result["metrics"]:
            result.get("metric_errors", {}).pop(name, None)
        write(output / "measurement.json", result)
    return 0 if result["cleanup_ok"] and not result["quality_errors"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    for name in ("ueransim", "output"):
        p.add_argument(f"--{name}", type=Path, required=name in ("ueransim", "output"))
    p.add_argument('--extension',type=Path)
    p.add_argument('--extension-config',type=Path)
    p.add_argument('--requirements',type=Path,default=ROOT/'requirements.json')
    p.add_argument('--user-plane',choices=('ueransim','linux-gtp'),default='ueransim')
    p.add_argument('--kernel-gtp-module',type=Path)
    p.add_argument("--iperf-bin", type=Path)
    p.add_argument("--iperf-build-receipt", type=Path)
    p.add_argument("--runtime-environment", type=Path)
    p.add_argument("--ella-bin", type=Path)
    p.add_argument("--qcore-bin", type=Path)
    p.add_argument("--cores", nargs='+')
    p.add_argument("--layout", choices=('loopback','split'), default='loopback')
    p.add_argument("--cipher-algorithm", type=int, choices=(0,2), default=2)
    p.add_argument("--slice-sd", default='010203')
    p.add_argument("--provisioned-sqn", type=int)
    for role in ('core','gnb','generator','observer'):
        p.add_argument('--'+role+'-cpus',type=int,nargs='+')
    p.add_argument("--free5gc", type=Path)
    p.add_argument("--open5gs-bin", default="/usr/bin")
    p.add_argument("--mongo-image", default="docker.io/library/mongo:8.0.12")
    p.add_argument("--ue-counts", nargs="+", type=int, default=[1, 10])
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--warmup", type=int, default=5)
    p.add_argument("--packet-bytes", type=int, default=1200)
    p.add_argument("--mbps", type=float, default=5)
    p.add_argument("--repetitions", type=int, default=6)
    p.add_argument("--seed", type=int, default=1729)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--equivalence-percent", type=float, default=10)
    p.add_argument("--max-host-busy-percent", type=float, default=80)
    sub.add_parser("trial").add_argument("lab", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args)
            return 0
        return trial(args.lab)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"native lab: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
