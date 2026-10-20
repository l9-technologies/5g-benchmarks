"""UERANSIM configuration and launch through the common lab interface."""
from pathlib import Path
import re
import time
from .common import service_profile,subscriber_sqn


def start(lab,count,core):
    profile=service_profile(lab.config)
    radio_ns=getattr(lab,"radio_ns",lab.core_ns)
    source = Path(lab.config["radio_configs"])
    gnb = (source / "gnb.yaml").read_text()
    for field in ("linkIp", "ngapIp", "gtpIp"):
        gnb = re.sub(rf"{field}: .*", f"{field}: {lab.config.get('gnb_ip','127.0.0.2')}", gnb)
    gnb=gnb.replace("address: 127.0.0.1", "address: "+lab.config.get("n2_ip","127.0.0.1"))
    gnb=re.sub(r"sd: \d+", "sd: "+str(int(profile["slice_sd"],16)), gnb)
    (lab.output / "gnb.yaml").write_text(gnb)
    ue = (source / "ue.yaml").read_text().replace("- 127.0.0.1", "- "+lab.config.get("gnb_ip","127.0.0.2"))
    for algorithm in ("IA1", "IA3", "EA1", "EA3"):
        ue = ue.replace(f"{algorithm}: true", f"{algorithm}: false")
    ue=re.sub(r"EA2: (?:true|false)","EA2: "+("true" if profile["cipher_algorithm"]==2 else "false"),ue)
    ue=re.sub(r"sd: \d+", "sd: "+str(int(profile["slice_sd"],16)),ue)
    variant=lab.config.get('ue_variant')
    if variant:
        changes={'bad-key':("key: '00112233445566778899aabbccddeeff'","key: '10112233445566778899aabbccddeeff'"),
                 'unknown-subscriber':("supi: 'imsi-001010000000001'","supi: 'imsi-001010999999999'"),
                 'bad-dnn':("apn: 'internet'","apn: 'not-allowed.example'"),
                 'bad-slice':('sd: '+str(int(profile['slice_sd'],16)),'sd: '+str((int(profile['slice_sd'],16)+1)%(1<<24)))}
        if variant not in changes:raise ValueError('unknown negative UE profile')
        before,after=changes[variant]
        if before not in ue:raise ValueError('negative profile no longer matches the configured UE')
        ue=ue.replace(before,after)
    (lab.output / "ue.yaml").write_text(ue)
    bins = Path(lab.config["ueransim"]) / "build"
    lab.start("gnb", [bins / "nr-gnb", "-c", lab.output / "gnb.yaml"], role="gnb", namespace=radio_ns)
    lab.wait(lambda: "NG Setup procedure is successful" in (lab.output / "gnb.log").read_text())
    if lab.config.get("monitor_registration") and hasattr(lab,"start_monitor"):lab.start_monitor()
    start = time.monotonic()
    lab.start("ue", [bins / "nr-ue", "-c", lab.output / "ue.yaml", "-r", "-l", "--num-of-UE", str(count)], role="ue", namespace=radio_ns)
    interfaces = [f"uesimtun{i}" for i in range(count)]
    lab.wait(lambda: all(i in lab.ns(radio_ns, ["ip", "-o", "-4", "addr"]) for i in interfaces))
    elapsed = time.monotonic() - start
    return elapsed*1000,interfaces


def profile_proof(text,count,config):
    expected={f'{1010000000000+i:015d}':subscriber_sqn(config,i) for i in range(1,count+1)}
    observed={}
    for line in text.splitlines():
        prefix=re.match(r'^\[[^]]+\] \[([^]]+)\]',line)
        if not prefix:continue
        sqn=re.search(r'Received SQN \[([0-9a-fA-F]{12})\]',line)
        selected=re.search(r'Selected integrity\[(\d+)\] ciphering\[(\d+)\]',line)
        if not sqn and not selected:continue
        identity=prefix[1].split('|')[0].removeprefix('imsi-')
        if identity=='nas' and count==1:identity=next(iter(expected))
        row=observed.setdefault(identity,{'sqns':[],'algorithms':[]})
        if sqn:row['sqns'].append(int(sqn[1],16))
        if selected:row['algorithms'].append([int(selected[1]),int(selected[2])])
    service=service_profile(config)
    security=set(observed)==set(expected) and all(row['algorithms']==[[2,service['cipher_algorithm']]] for row in observed.values())
    challenges=set(observed)==set(expected) and all(observed[identity]['sqns']==[value] for identity,value in expected.items())
    resync=bool(re.search(r'(?i)AUTS|synchroni[sz]ation failure|SQN.*(failure|out of)',text))
    return {'passed':security and challenges and not resync,'security_passed':security,'challenge_passed':challenges,
            'resynchronization':resync,'expected_challenges':expected,'observed':observed}
