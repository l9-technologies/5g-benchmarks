"""open5gs provisioning and launch through the common lab interface."""
import argparse
from pathlib import Path
from runner.evidence import read,write
from .common import KEY,OPC,OPEN_NFS,module


def start(lab, count):
    count=max(count,2)
    conf = lab.output / "open5gs"
    config_module = module(Path(lab.config["configuration_tool"]), "benchmark_open5gs_config")
    config_module.configure(argparse.Namespace(config_dir=conf, profile=Path(lab.config["profile"]),
        interface="lo", n3_bind_host="127.0.0.1", n3_advertise_host="127.0.0.1", metadata=lab.output / "open5gs.json"))
    # Use direct NRF discovery for every NF. No host service or host config is changed.
    addresses = {"nrf": "127.0.0.10", "scp": "127.0.0.200", "ausf": "127.0.0.11", "udm": "127.0.0.12",
                 "udr": "127.0.0.20", "pcf": "127.0.0.13", "nssf": "127.0.0.14", "bsf": "127.0.0.15",
                 "amf": "127.0.0.5", "smf": "127.0.0.4", "upf": "127.0.0.7"}
    for nf in OPEN_NFS:
        path = conf / f"{nf}.yaml"
        data = read(path) if path.exists() else {nf: {"sbi": {"server": [{"address": addresses[nf], "port": 7777}], "client": {"nrf": [{"uri": "http://127.0.0.10:7777"}]}}}}
        data["logger"] = {"file": {"path": str(lab.output / f"open5gs-{nf}.log")}}
        if "sbi" in data[nf] and nf != "nrf":
            client = data[nf]["sbi"].setdefault("client", {})
            client.pop("scp", None)
            client["nrf"] = [{"uri": "http://127.0.0.10:7777"}]
        if nf == "amf":
            data[nf]["ngap"]["server"] = [{"address": "127.0.0.1"}]
            data[nf]["security"] = {"integrity_order": ["NIA2"], "ciphering_order": ["NEA2"]}
        write(path, data)
    lab.ns(lab.core_ns, ["ip", "tuntap", "add", "ogstun", "mode", "tun"])
    lab.ns(lab.core_ns, ["ip", "addr", "add", "10.45.0.1/24", "dev", "ogstun"])
    lab.ns(lab.core_ns, ["ip", "link", "set", "ogstun", "up"])
    lab.ns(lab.core_ns, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.45.0.0/24", "-o", "dn0", "-j", "MASQUERADE"])
    lab.ns(lab.core_ns, ["iptables", "-A", "FORWARD", "-i", "ogstun", "-o", "dn0", "-s", "10.45.0.0/24", "-j", "ACCEPT"])
    lab.ns(lab.core_ns, ["iptables", "-A", "FORWARD", "-i", "dn0", "-o", "ogstun", "-d", "10.45.0.0/24", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    lab.start_database()
    js = 'for(let i=1;i<=' + str(count) + ';i++){const imsi=String(1010000000000+i).padStart(15,"0"); db.subscribers.insertOne({schema_version:1,imsi,msisdn:[],slice:[{sst:1,sd:"010203",default_indicator:true,session:[{name:"internet",type:3,qos:{index:9,arp:{priority_level:8,pre_emption_capability:1,pre_emption_vulnerability:2}},ambr:{downlink:{value:1,unit:3},uplink:{value:1,unit:3}},pcc_rule:[]}]}],security:{k:"' + KEY + '",opc:"' + OPC + '",amf:"8000",sqn:NumberLong(String(i*32))},ambr:{downlink:{value:1,unit:3},uplink:{value:1,unit:3}},access_restriction_data:32,network_access_mode:0,subscriber_status:0});}'
    lab.provision_database("open5gs", js)
    for nf in OPEN_NFS:
        lab.start(f"core-{nf}", [Path(lab.config["open5gs_bin"]) / f"open5gs-{nf}d", "-c", conf / f"{nf}.yaml"], role="core")
    lab.wait(lambda: "38412" in lab.ns(lab.core_ns, ["ss", "-lSnH"]))
    lab.wait(lambda: "PFCP associated" in (lab.output / "core-smf.log").read_text())
