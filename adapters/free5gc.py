"""free5gc provisioning and launch through the common lab interface."""
import json
from pathlib import Path
import re
from runner.evidence import write
from .common import KEY,OPC,FREE_NFS,initial_sqn


def start(lab, count):
    count=max(count,2)
    root = Path(lab.config["free5gc"])
    conf = lab.output / "free5gc"
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
    lab.ns(lab.core_ns, ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.45.0.0/24", "-o", "dn0", "-j", "MASQUERADE"])
    lab.start_database()
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
