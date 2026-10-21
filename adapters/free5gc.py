"""free5gc provisioning and launch through the common lab interface."""
import json
from pathlib import Path
import re
import shutil
from runner.evidence import write
from .common import KEY,OPC,FREE_NFS,initial_sqn,service_profile,subscriber_sqn,subscriber_population


def registered_nf_ids(log):
    return set(re.findall(r"\|\s*201\s*\|[^\n]*\|\s*PUT\s*\|\s*/nnrf-nfm/v1/nf-instances/([0-9a-f-]{36})(?=\s|$)",log))


def start(lab, count):
    count=subscriber_population(lab.config,count)
    profile=service_profile(lab.config)
    root = Path(lab.config["free5gc"])
    conf = lab.output / "free5gc"
    conf.mkdir()
    certificates=conf/"cert"
    shutil.copytree(root/"source/cert",certificates)
    aliases = {"nrf": 10, "amf": 1, "smf": 4, "upf": 7, "ausf": 11, "udm": 12,
               "udr": 20, "pcf": 13, "nssf": 14, "chf": 15}
    import yaml
    class JsonConfigLoader(yaml.SafeLoader):
        # Keep timestamp text while retaining numeric and boolean YAML types.
        yaml_implicit_resolvers={k:[r for r in rows if r[0]!='tag:yaml.org,2002:timestamp'] for k,rows in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    for nf in FREE_NFS:
        text = (root / "source/config" / f"{nf}cfg.yaml").read_text()
        for name, suffix in aliases.items():
            text = text.replace(f"{name}.free5gc.org", f"127.0.0.{suffix}")
        text = text.replace("mongodb://db:", "mongodb://127.0.0.1:")
        text=re.sub(r"(?:\./)?cert/",str(certificates)+"/",text)
        for field, old, new in (("mcc", "208", "001"), ("mnc", "93", "01")):
            text = re.sub(rf'(?m)^(\s*(?:-\s*)?{field}:\s*)(?:"{old}"|{old})(?=\s|$)', lambda m: m[1] + json.dumps(new), text)
        text = re.sub(r'(?m)^(\s*sd:\s*)([0-9a-fA-F]{6})(?=\s|$)', lambda m: m[1] + json.dumps(m[2]), text)
        text = text.replace("10.60.0.0/16", "10.45.0.0/24").replace("- NEA0", "- NEA"+str(profile["cipher_algorithm"]))
        # SBI authorization is outside this NAS/data-plane workload for all cores.
        # Preserve the upstream authorization requirement.
        if nf == "upf":
            text = text.replace("# ifname: gtpif", "ifname: gtpif").replace("# natifname: eth0", "natifname: dn0")
        data=yaml.load(text,Loader=JsonConfigLoader)
        configuration=data.get('configuration',{})
        if nf=='amf':
            configuration['ngapIpList']=[lab.config.get('n2_ip','127.0.0.1')]
            for row in configuration.get('supportTaiList',[]):row['tac']='000001'
        if nf=='smf':
            for node in configuration.get('userplaneInformation',{}).get('upNodes',{}).values():
                for interface in node.get('interfaces',[]):interface['endpoints']=[lab.config.get('n3_ip','127.0.0.1')]
        if nf=='upf':
            for row in data.get('gtpu',configuration.get('gtpu',{})).get('ifList',[]):row['addr']=lab.config.get('n3_ip','127.0.0.1')
        def slices(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key=='sd':value[key]=profile['slice_sd']
                    else:slices(item)
            elif isinstance(value,list):
                for item in value:slices(item)
        slices(data)
        write(conf/f'{nf}cfg.yaml',data)
    lab.ns(lab.core_ns, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.45.0.0/24", "-o", "dn0", "-j", "MASQUERADE"])
    lab.start_database()
    rows = []
    slice_ = {"sst": 1, "sd": profile["slice_sd"]}
    for i in range(1, count + 1):
        ue = f"imsi-{1010000000000 + i:015d}"
        base = {"ueId": ue, "servingPlmnId": "00101"}
        for collection, data in {
            "authenticationData.authenticationSubscription": {"ueId": ue, "authenticationMethod": "5G_AKA", "encPermanentKey": KEY, "encOpcKey": OPC, "authenticationManagementField": "8000", "sequenceNumber": {"sqnScheme": "GENERAL", "sqn": f"{subscriber_sqn(lab.config,i):012x}"}},
            "provisionedData.amData": {**base, "gpsis": [], "nssai": {"defaultSingleNssais": [slice_], "singleNssais": [slice_]}, "subscribedUeAmbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"}},
            "provisionedData.smData": {**base, "singleNssai": slice_, "dnnConfigurations": {"internet": {"pduSessionTypes": {"defaultSessionType": "IPV4", "allowedSessionTypes": ["IPV4"]}, "sscModes": {"defaultSscMode": "SSC_MODE_1", "allowedSscModes": ["SSC_MODE_1"]}, "sessionAmbr": {"uplink": "1 Gbps", "downlink": "1 Gbps"}, "5gQosProfile": {"5qi": 9, "arp": {"priorityLevel": 8, "preemptCap": "NOT_PREEMPT", "preemptVuln": "PREEMPTABLE"}, "priorityLevel": 8}}}},
            "provisionedData.smfSelectionSubscriptionData": {**base, "subscribedSnssaiInfos": {"01"+profile["slice_sd"]: {"dnnInfos": [{"dnn": "internet"}]}}},
        }.items():
            rows.append(["subscriptionData." + collection, data])
        rows += [["policyData.ues.amData", {"ueId": ue, "subscCats": ["free5gc"]}],
                 ["policyData.ues.smData", {"ueId": ue, "smPolicySnssaiData": {"01"+profile["slice_sd"]: {"snssai": slice_, "smPolicyDnnData": {"internet": {"dnn": "internet"}}}}}]]
    js = "for (const [c,d] of " + json.dumps(rows) + ") db.getCollection(c).insertOne(d);"
    lab.provision_database("free5gc", js)
    write(conf / "uerouting.yaml", {"info": {"version": "1.0.7", "description": "single UPF benchmark"}, "ueRoutingInfo": {}})
    for nf in ["nrf", "upf", "udr", "udm", "ausf", "pcf", "nssf", "chf", "smf", "amf"]:
        args = [root / "bin" / nf, "-c", conf / f"{nf}cfg.yaml"]
        if nf == "smf":
            args += ["-u", conf / "uerouting.yaml"]
        lab.start(f"core-{nf}", args, role="core")
        if nf == "nrf":
            lab.wait(lambda: "8000" in lab.ns(lab.core_ns, ["ss", "-lntH"]))
    lab.wait(lambda: "38412" in lab.ns(lab.core_ns, ["ss", "-lSnH"]))
    lab.wait(lambda: "PFCP Association Setup Accepted" in (lab.output / "core-smf.log").read_text())
    lab.wait(lambda: len(registered_nf_ids((lab.output/'core-nrf.log').read_text()))==8)
