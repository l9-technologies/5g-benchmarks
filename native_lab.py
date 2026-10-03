#!/usr/bin/env python3
"""Prepare and run an isolated native Linux 5G comparison lab."""
from __future__ import annotations

import argparse
import importlib.util
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

ROOT = Path(__file__).resolve().parent
NFS = "nrf udr udm ausf pcf nssf amf smf upf".split()
OPEN_NFS = NFS + ["bsf"]
FREE_NFS = NFS + ["chf"]
KEY = "00112233445566778899aabbccddeeff"
OPC = "000102030405060708090a0b0c0d0e0f"
UE_ROUTE_TABLE_BASE = 1000  # Keep UE policy routes outside Linux tables 253 through 255.


def initial_sqn(index):
    return index << 5


def command(args, timeout=60, check=True):
    result = subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr[-1500:]}")
    return result.stdout


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


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


def generator_saturated(samples, role):
    # A busy thread can limit the generator even when other host CPUs are idle.
    for a, b in zip(samples, samples[10:]):
        elapsed = b["time"] - a["time"]
        if elapsed >= 1:
            # Older evidence has only process totals; keep its conservative bound.
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
    tools = ("ip", "ss", "sysctl", "iperf3", "ping", "tcpdump", "tshark", "podman", "iptables")
    for tool in tools:
        if not shutil.which(tool):
            raise ValueError(f"install {tool} before prepare")
    extension=module(args.extension.resolve(),'lab_extension') if args.extension else None
    extension_config=read(args.extension_config) if args.extension_config else {}
    added=extension.prepare(extension_config) if extension else {'paths':[],'identities':{},'cores':[],'radios':[],'config':{}}
    ueran = args.ueransim.resolve()
    paths=[]
    paths += [ueran / "build" / f"nr-{node}" for node in ("ue", "gnb")]
    if args.free5gc:
        paths += [args.free5gc / "bin" / nf for nf in FREE_NFS]
    paths += [Path(args.open5gs_bin).resolve() / f"open5gs-{nf}d" for nf in OPEN_NFS]
    paths += [Path(shutil.which(tool)).resolve() for tool in tools] + [Path(sys.executable).resolve()]
    for p in paths:
        if not p.is_file() or not os.access(p, os.X_OK):
            raise ValueError(f"build or install the executable: {p}")
    paths+=list(added['paths'])
    if args.extension:paths.append(args.extension.resolve())
    if args.extension_config:paths.append(args.extension_config.resolve())
    if args.free5gc:
        paths += list((args.free5gc / "source/config").glob("*.yaml")) + list((args.free5gc / "source/cert").glob("*"))
        paths += [Path(command(["modinfo", "-n", "gtp5g"]).strip())]
    config_tool=ROOT/'open5gs_config.py'
    profile=ROOT/'configs/profile.toml'
    radio_configs=ROOT/'configs'
    for p in (config_tool, profile):
        if not p.is_file():
            raise ValueError(f"missing repository input: {p}")
    mongo = json.loads(command(["podman", "image", "inspect", args.mongo_image]))[0]
    image_id = mongo["Id"]
    paths += [config_tool, profile, ROOT / "benchmark.py", Path(__file__).resolve()]
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
    inputs = {str(p): digest(p) for p in paths}
    identity = {
        "open5gs": object_hash({"binaries": {p: v for p, v in inputs.items() if "/open5gs-" in p and p.endswith("d")}, "mongo": image_id}),
        "ueransim": object_hash({p: v for p, v in inputs.items() if "/build/nr-" in p}),
    }
    if args.free5gc:
        identity["free5gc"] = object_hash({p: v for p, v in inputs.items() if str(args.free5gc) in p or "gtp5g.ko" in p})
    identity.update(added['identities'])
    args.output.mkdir(parents=True, exist_ok=False)
    lab = {'extension':str(args.extension.resolve()) if args.extension else None,'extension_config':added['config'], "ueransim": str(ueran), "radio_configs": str(radio_configs),
           "free5gc": str(args.free5gc.resolve()) if args.free5gc else None,
           "open5gs_bin": str(Path(args.open5gs_bin).resolve()), "mongo_image": image_id,
           "configuration_tool": str(config_tool), "profile": str(profile),
           "maximum_host_busy_percent": args.max_host_busy_percent,"method_version":2}
    write(args.output / "lab.json", lab)
    inputs[str((args.output / "lab.json").resolve())] = digest(args.output / "lab.json")
    candidates = [{"id": f"{c}-{r}", "core": c, "radio": r,
                   "identity": {"core": identity[c], "radio": identity[r]}}
                  for c in (['open5gs']+(['free5gc'] if args.free5gc else [])+added['cores'])
                  for r in (['ueransim']+added['radios'])]
    candidates += [{**c,'id':c['id']+'-repeat'} for c in list(candidates)]
    plan = {"schema_version": 1,"method_version":2,'minimum_repetitions':args.repetitions,
            'required_features':[r['feature'] for r in requirements['requirements']],'requirements':requirements,
            "mode": "live", "seed": args.seed, "repetitions": balanced_repetitions(len(candidates),args.repetitions),
            "timeout_seconds": args.timeout, "equivalence_percent": args.equivalence_percent,
            "protocol": {"ue_counts": args.ue_counts, "duration_seconds": args.seconds,
                "warmup_seconds": args.warmup, "packet_bytes": args.packet_bytes,
                "offered_mbps_per_ue": args.mbps, "security": "NIA2/NEA2", "plmn": "00101",
                "slice": "1/010203", "dnn": "internet", "session_ambr_mbps": 1000,
                "ambr_scope": "subscription configuration and N2 signalling target; NAS rate conformance and enforcement are untested",
                "transport": "IPv4", "path": "UE TUN -> gNB -> N3 -> UPF TUN -> N6 veth -> receiver",
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
        if self.extension:self.config.update(config['extension_config'])
        self.prefix = f"b5g-{os.getpid()}"
        self.core_ns, self.dn_ns, self.ue_ns = [f"{self.prefix}-{s}" for s in ("core", "dn", "ue")]
        self.namespaces, self.processes = [], []
        self.mongo = None
        self.mongo_volumes = []
        self.samples = []
        self.commands = {}
        self.monitor_stop = threading.Event()
        self.monitor_thread = None

    def ns(self, namespace, args, **kwargs):
        return command(["ip", "netns", "exec", namespace, *args], **kwargs)

    def start(self, name, args, namespace=None, env=None, role=None):
        if role == "ue" and self.config.get("enable_radio_commands"):
            args = [a for a in args if a != "-l"]
        argv = ["ip", "netns", "exec", namespace or self.core_ns, *map(str, args)]
        base_env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','TMPDIR')}
        with (self.output / f"{name}.log").open("wb") as handle:
            p = subprocess.Popen(argv, env={**base_env, "RUST_LOG": "info", "NO_COLOR": "1", **(env or {})},
                                 stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((p, role, name))
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

    def wait(self, predicate, seconds=45):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            dead = [name for p, role, name in self.processes if role and p.poll() is not None]
            if dead:
                raise RuntimeError(f"process stopped: {dead}")
            if predicate():
                return
            time.sleep(.05)
        raise TimeoutError("readiness deadline expired")

    def start_open5gs(self, count):
        count=max(count,2)
        conf = self.output / "open5gs"
        config_module = module(Path(self.config["configuration_tool"]), "benchmark_open5gs_config")
        config_module.configure(argparse.Namespace(config_dir=conf, profile=Path(self.config["profile"]),
            interface="lo", n3_bind_host="127.0.0.1", n3_advertise_host="127.0.0.1", metadata=self.output / "open5gs.json"))
        # Use direct NRF discovery for every NF. No host service or host config is changed.
        addresses = {"nrf": "127.0.0.10", "scp": "127.0.0.200", "ausf": "127.0.0.11", "udm": "127.0.0.12",
                     "udr": "127.0.0.20", "pcf": "127.0.0.13", "nssf": "127.0.0.14", "bsf": "127.0.0.15",
                     "amf": "127.0.0.5", "smf": "127.0.0.4", "upf": "127.0.0.7"}
        for nf in OPEN_NFS:
            path = conf / f"{nf}.yaml"
            data = read(path) if path.exists() else {nf: {"sbi": {"server": [{"address": addresses[nf], "port": 7777}], "client": {"nrf": [{"uri": "http://127.0.0.10:7777"}]}}}}
            data["logger"] = {"file": {"path": str(self.output / f"open5gs-{nf}.log")}}
            if "sbi" in data[nf] and nf != "nrf":
                client = data[nf]["sbi"].setdefault("client", {})
                client.pop("scp", None)
                client["nrf"] = [{"uri": "http://127.0.0.10:7777"}]
            if nf == "amf":
                data[nf]["ngap"]["server"] = [{"address": "127.0.0.1"}]
                data[nf]["security"] = {"integrity_order": ["NIA2"], "ciphering_order": ["NEA2"]}
            write(path, data)
        self.ns(self.core_ns, ["ip", "tuntap", "add", "ogstun", "mode", "tun"])
        self.ns(self.core_ns, ["ip", "addr", "add", "10.45.0.1/24", "dev", "ogstun"])
        self.ns(self.core_ns, ["ip", "link", "set", "ogstun", "up"])
        self.ns(self.core_ns, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.45.0.0/24", "-o", "dn0", "-j", "MASQUERADE"])
        self.ns(self.core_ns, ["iptables", "-A", "FORWARD", "-i", "ogstun", "-o", "dn0", "-s", "10.45.0.0/24", "-j", "ACCEPT"])
        self.ns(self.core_ns, ["iptables", "-A", "FORWARD", "-i", "dn0", "-o", "ogstun", "-d", "10.45.0.0/24", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
        self.start_database()
        js = 'for(let i=1;i<=' + str(count) + ';i++){const imsi=String(1010000000000+i).padStart(15,"0"); db.subscribers.insertOne({schema_version:1,imsi,msisdn:[],slice:[{sst:1,sd:"010203",default_indicator:true,session:[{name:"internet",type:3,qos:{index:9,arp:{priority_level:8,pre_emption_capability:1,pre_emption_vulnerability:2}},ambr:{downlink:{value:1,unit:3},uplink:{value:1,unit:3}},pcc_rule:[]}]}],security:{k:"' + KEY + '",opc:"' + OPC + '",amf:"8000",sqn:NumberLong(String(i*32))},ambr:{downlink:{value:1,unit:3},uplink:{value:1,unit:3}},access_restriction_data:32,network_access_mode:0,subscriber_status:0});}'
        self.provision_database("open5gs", js)
        for nf in OPEN_NFS:
            self.start(f"core-{nf}", [Path(self.config["open5gs_bin"]) / f"open5gs-{nf}d", "-c", conf / f"{nf}.yaml"], role="core")
        self.wait(lambda: "38412" in self.ns(self.core_ns, ["ss", "-lSnH"]))
        self.wait(lambda: "PFCP associated" in (self.output / "core-smf.log").read_text())

    def start_core(self,core,count):
        if core=='open5gs':return self.start_open5gs(count)
        if core=='free5gc':return self.start_free5gc(count)
        if not self.extension:raise ValueError('unknown core adapter')
        return self.extension.start_core(self,core,count)

    def start_database(self):
        self.mongo = f"{self.prefix}-mongo"
        command(["podman", "run", "-d", "--name", self.mongo, "--network", f"ns:/run/netns/{self.core_ns}",
                 self.config["mongo_image"], "--bind_ip", "127.0.0.1"], timeout=90)
        container = json.loads(command(["podman", "inspect", self.mongo]))[0]
        self.mongo_volumes = [m["Name"] for m in container["Mounts"] if m["Type"] == "volume"]
        write(self.output / "database-volumes.json", {"container": self.mongo, "volumes": self.mongo_volumes})
        self.wait(lambda: "27017" in self.ns(self.core_ns, ["ss", "-lntH"]))

    def provision_database(self, database, script):
        path = self.output / "subscribers.js"
        path.write_text(script)
        command(["podman", "cp", path, f"{self.mongo}:/tmp/subscribers.js"])
        command(["podman", "exec", self.mongo, "mongosh", "--quiet", database, "/tmp/subscribers.js"])

    def start_free5gc(self, count):
        count=max(count,2)
        root = Path(self.config["free5gc"])
        conf = self.output / "free5gc"
        conf.mkdir()
        aliases = {"nrf": 10, "amf": 1, "smf": 4, "upf": 7, "ausf": 11, "udm": 12,
                   "udr": 20, "pcf": 13, "nssf": 14, "chf": 15}
        for nf in FREE_NFS:
            text = (root / "source/config" / f"{nf}cfg.yaml").read_text()
            for name, suffix in aliases.items():
                text = text.replace(f"{name}.free5gc.org", f"127.0.0.{suffix}")
            text = text.replace("mongodb://db:", "mongodb://127.0.0.1:").replace("cert/", str(root / "source/cert") + "/")
            for field, old, new in (("mcc", "208", "001"), ("mnc", "93", "01")):
                text = re.sub(rf'(?m)^(\s*(?:-\s*)?{field}:\s*)(?:"{old}"|{old})(?=\s|$)', lambda m: m[1] + json.dumps(new), text)
            text = re.sub(r'(?m)^(\s*sd:\s*)([0-9a-fA-F]{6})(?=\s|$)', lambda m: m[1] + json.dumps(m[2]), text)
            text = text.replace("10.60.0.0/16", "10.45.0.0/24").replace("- NEA0", "- NEA2")
            # SBI authorization is outside this NAS/data-plane workload for all cores.
            # Preserve the upstream authorization requirement.
            if nf == "upf":
                text = text.replace("# ifname: gtpif", "ifname: gtpif").replace("# natifname: eth0", "natifname: dn0")
            (conf / f"{nf}cfg.yaml").write_text(text)
        self.ns(self.core_ns, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.45.0.0/24", "-o", "dn0", "-j", "MASQUERADE"])
        self.start_database()
        rows = []
        slice_ = {"sst": 1, "sd": "010203"}
        for i in range(1, count + 1):
            ue = f"imsi-{1010000000000 + i:015d}"
            base = {"ueId": ue, "servingPlmnId": "00101"}
            for collection, data in {
                "authenticationData.authenticationSubscription": {"ueId": ue, "authenticationMethod": "5G_AKA", "encPermanentKey": KEY, "encOpcKey": OPC, "authenticationManagementField": "8000", "sequenceNumber": {"sqnScheme": "GENERAL", "sqn": f"{initial_sqn(i):012x}"}},
                "provisionedData.amData": {**base, "gpsis": [], "nssai": {"defaultSingleNssais": [slice_], "singleNssais": [slice_]}, "subscribedUeAmbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"}},
                "provisionedData.smData": {**base, "singleNssai": slice_, "dnnConfigurations": {"internet": {"pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]}, "sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_1"]}, "sessionAmbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"}, "5gQosProfile": {"5qi": 9, "arp": {"priorityLevel": 8, "preemptCap": "NOT_PREEMPT", "preemptVuln": "PREEMPTABLE"}, "priorityLevel": 8}}}},
                "provisionedData.smfSelectionSubscriptionData": {**base, "subscribedSnssaiInfos": {"01010203": {"dnnInfos": [{"dnn": "internet"}]}}},
            }.items():
                rows.append(["subscriptionData." + collection, data])
            rows += [["policyData.ues.amData", {"ueId": ue, "subscCats": ["free5gc"]}],
                     ["policyData.ues.smData", {"ueId": ue, "smPolicySnssaiData": {"01010203": {"snssai": slice_, "smPolicyDnnData": {"internet": {"dnn": "internet"}}}}}]]
        js = "for (const [c,d] of " + json.dumps(rows) + ") db.getCollection(c).insertOne(d);"
        self.provision_database("free5gc", js)
        write(conf / "uerouting.yaml", {"info": {"version": "1.0.7", "description": "single UPF benchmark"}, "ueRoutingInfo": {}})
        for nf in ["nrf", "upf", "udr", "udm", "ausf", "pcf", "nssf", "chf", "smf", "amf"]:
            args = [root / "bin" / nf, "-c", conf / f"{nf}cfg.yaml"]
            if nf == "smf":
                args += ["-u", conf / "uerouting.yaml"]
            self.start(f"core-{nf}", args, role="core")
            if nf == "nrf":
                self.wait(lambda: "8000" in self.ns(self.core_ns, ["ss", "-lntH"]))
        self.wait(lambda: "38412" in self.ns(self.core_ns, ["ss", "-lSnH"]))
        self.wait(lambda: "PFCP Association Setup Accepted" in (self.output / "core-smf.log").read_text())

    def radio(self, kind, count, core="open5gs"):
        if kind == "ueransim":
            source = Path(self.config["radio_configs"])
            gnb = (source / "gnb.yaml").read_text()
            for field in ("linkIp", "ngapIp", "gtpIp"):
                gnb = re.sub(rf"{field}: .*", f"{field}: 127.0.0.2", gnb)
            (self.output / "gnb.yaml").write_text(gnb)
            ue = (source / "ue.yaml").read_text().replace("- 127.0.0.1", "- 127.0.0.2")
            for algorithm in ("IA1", "IA3", "EA1", "EA3"):
                ue = ue.replace(f"{algorithm}: true", f"{algorithm}: false")
            (self.output / "ue.yaml").write_text(ue)
            bins = Path(self.config["ueransim"]) / "build"
            self.start("gnb", [bins / "nr-gnb", "-c", self.output / "gnb.yaml"], role="gnb")
            self.wait(lambda: "NG Setup procedure is successful" in (self.output / "gnb.log").read_text())
            start = time.monotonic()
            self.start("ue", [bins / "nr-ue", "-c", self.output / "ue.yaml", "-r", "-l", "--num-of-UE", str(count)], role="ue")
            interfaces = [f"uesimtun{i}" for i in range(count)]
            self.wait(lambda: all(i in self.ns(self.core_ns, ["ip", "-o", "-4", "addr"]) for i in interfaces))
            elapsed = time.monotonic() - start
        else:
            if not self.extension:raise ValueError('unknown radio adapter')
            elapsed_ms,interfaces=self.extension.start_radio(self,kind,count,core)
            elapsed=elapsed_ms/1000
        # Both simulators reach the same timed boundary before this shared setup.
        before_move = json.loads(self.ns(self.core_ns, ["ip", "-j", "-4", "addr"]))
        assigned = {r["ifname"]: next((a["local"] for a in r["addr_info"] if a["family"] == "inet"), None) for r in before_move}
        for interface in interfaces:
            self.ns(self.core_ns, ["ip", "link", "set", interface, "netns", self.ue_ns])
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
        return elapsed * 1000, [(i, ips[i]) for i in interfaces]

    def pids(self):
        result = {p.pid: role for p, role, name in self.processes if role and p.poll() is None}
        if self.mongo:
            pid = command(["podman", "inspect", "--format", "{{.State.Pid}}", self.mongo], check=False).strip()
            if pid.isdigit() and int(pid):
                result[int(pid)] = "core"
        return result

    def monitor(self):
        ticks_per_second, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        pids = self.pids()
        while not self.monitor_stop.is_set():
            row = {"time": time.monotonic(), "roles": {role: {"cpu": 0, "rss": 0, "pss": 0} for role in ("core", "ue", "gnb")}}
            row["threads"] = {role: {} for role in ("core", "ue", "gnb")}
            try:
                for pid, role in pids.items():
                    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                    row["roles"][role]["cpu"] += (int(stat[11]) + int(stat[12])) / ticks_per_second
                    row["roles"][role]["rss"] += int(stat[21]) * page
                    memory = Path(f"/proc/{pid}/smaps_rollup").read_text()
                    row["roles"][role]["pss"] += int(re.search(r"^Pss:\s+(\d+)", memory, re.M)[1]) * 1024
                    if role in ("ue", "gnb"):
                        for task in Path(f"/proc/{pid}/task").iterdir():
                            try:
                                thread = (task / "stat").read_text().rsplit(") ", 1)[1].split()
                                row["threads"][role][task.name] = (int(thread[11]) + int(thread[12])) / ticks_per_second
                            except FileNotFoundError:
                                pass
                row["host"] = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
                memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
                row['host_memory_used_bytes']=memory['MemTotal']-memory['MemAvailable']
                self.samples.append(row)
            except (OSError, ValueError):
                self.samples.append({"error": "a measured process disappeared"})
                return
            self.monitor_stop.wait(self.config.get("resource_sample_seconds", .1))

    def traffic(self, interfaces, protocol):
        metrics, quality, metric_errors = {}, [], {}
        self.monitor_thread = threading.Thread(target=self.monitor)
        self.monitor_thread.start()
        def probes(label, count, interval):
            clients = [self.start(f"{label}-ping-{n}", ["ping", "-n", "-c", str(count), "-i", str(interval), "-W", "2", "-I", ip, "192.0.2.1"], namespace=self.ue_ns)
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
        for n, _ in enumerate(interfaces):
            self.start(f"server-{n}", ["iperf3", "-s", "-B", "192.0.2.1", "-p", str(5201 + n)], namespace=self.dn_ns)
        collect_probes("idle", probes("idle", 20, .05))
        if self.samples:
            for role in ("core", "ue", "gnb"):
                for name in ("rss", "pss"):
                    metrics[f"{role}_idle_{name}_bytes"] = percentile([s["roles"][role][name] for s in self.samples if "roles" in s], .5)
        if self.samples:
            metrics['host_idle_memory_used_bytes']=percentile([s['host_memory_used_bytes'] for s in self.samples if 'host_memory_used_bytes' in s],.5)
        for mode in ("tcp_ul", "tcp_dl", "udp_ul", "udp_dl"):
            phase_start = len(self.samples)
            phase_started = time.monotonic()
            processes = []
            for n, (interface, ip) in enumerate(interfaces):
                args = ["iperf3", "-c", "192.0.2.1", "-p", str(5201 + n), "-B", ip,
                        "-t", str(protocol["duration_seconds"]), "-O", str(protocol["warmup_seconds"]), "-J", "--get-server-output",
                        "-b", f"{protocol['offered_mbps_per_ue']}M"]
                if mode.startswith("udp"):
                    args += ["-u", "-l", str(protocol["packet_bytes"])]
                else:
                    args += ["-M", str(protocol["packet_bytes"]), "-l", str(protocol["packet_bytes"])]
                if mode.endswith("dl"):
                    args += ["-R"]
                processes.append(self.start(f"{mode}-{n}", args, namespace=self.ue_ns))
            time.sleep(protocol["warmup_seconds"])
            loaded_probes = probes(mode, max(10, int(protocol["duration_seconds"] * 5)), .2)
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
                write(self.output / f"{mode}-window.json", {"launch_monotonic": phase_started, "warmup_seconds": protocol["warmup_seconds"], "end_monotonic": phase_ended, "samples": len(phase), "boundary": "shared aggregate window; clients launch in sequence"})
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
                except (ValueError, subprocess.TimeoutExpired) as error:
                    for name in [f"{mode}_ping_loss_percent", *[f"{mode}_rtt_p{p}_ms" for p in (50, 95, 99)]]:
                        metric_errors[name] = str(error)
                for p in processes:
                    stop_group(p)
        self.monitor_stop.set()
        self.monitor_thread.join()
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
            if generator_saturated(self.samples, role):
                quality.append(f"{role} generator thread CPU exceeded 90% of one CPU core for at least one second")
        return metrics, quality, metric_errors

    def cleanup(self):
        write(self.output / "commands.json", {name: {"argv": list(map(str, args)), "namespace": namespace or self.core_ns, "role": role} for name, (args, namespace, env, role) in self.commands.items()})
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
    lab = Lab(read(config_path), output)
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
        capture = lab.start("capture", ["tcpdump", "-i", "lo", "-s", "0", "-c", str(max(2000, count * 64)), "-U", "-w", output / "n2-n3.pcap", "sctp or udp port 2152"])
        lab.wait(lambda: "listening on" in (output / "capture.log").read_text())
        attach, interfaces = lab.radio(candidate["radio"], count, candidate["core"])
        result["metrics"].update(attach_ready_ms=attach, attach_success_percent=100)
        ue_log=(output/'ue.log').read_text()
        resync=bool(re.search(r'(?i)AUTS|synchroni[sz]ation failure|SQN.*(failure|out of)',ue_log))
        profile={k:plan['protocol'][k] for k in ('plmn','slice','dnn','security','session_ambr_mbps','transport','sbi_authorization')}
        result['comparison_state']={'service_profile':object_hash(profile),
            'authentication_profile':None if resync else object_hash({'initial_sqn':[initial_sqn(i) for i in range(1,count+1)],'ue_state':'fresh','resynchronization':False}),
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
        metrics, errors, metric_errors = lab.traffic(interfaces, plan["protocol"])
        result["metrics"].update(metrics)
        result.setdefault("metric_errors", {}).update(metric_errors)
        result["quality_errors"].extend(errors)
        for f in ("external_n6", "tcp", "udp"):
            result["features"][f] = "passed"
        for transport in ("tcp", "udp"):
            if any(f"{transport}_{d}_mbps" in metric_errors or metrics.get(f"{transport}_{d}_mbps", 0) == 0 for d in ("ul", "dl")):
                result["features"][transport] = "failed"
        if capture.poll() is None:
            os.killpg(capture.pid, signal.SIGINT)
        capture.wait(timeout=10)
        # Independent protocol dissector. Preserve captures for review.
        for name, display_filter in (("n2", "ngap"), ("n3_ul", "gtp.message == 255 && ip.src == 127.0.0.2"), ("n3_dl", "gtp.message == 255 && ip.dst == 127.0.0.2")):
            decoded = subprocess.run(["tshark", "-r", "-", "-Y", display_filter, "-T", "fields", "-e", "frame.number"],
                input=(output / "n2-n3.pcap").read_bytes(), capture_output=True, timeout=30)
            if decoded.returncode:
                raise ValueError(f"packet decode failed: {decoded.stderr.decode(errors='replace')[-1000:]}")
            data = decoded.stdout.decode()
            (output / f"{name}.txt").write_text(data)
            if not data.strip():
                raise ValueError(f"missing decoded {name} packets")
        result["features"]["n2_ngap"] = result["features"]["n3_gtpu"] = "passed"
        core_bins=[Path(lab.config['open5gs_bin'])/f'open5gs-{nf}d' for nf in OPEN_NFS] if candidate['core']=='open5gs' else [Path(lab.config['free5gc'])/'bin'/nf for nf in FREE_NFS] if candidate['core']=='free5gc' else []
        radio_bins={r:Path(lab.config['ueransim'])/'build'/f'nr-{r}' for r in ('ue','gnb')} if candidate['radio']=='ueransim' else {}
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
