"""UERANSIM configuration and launch through the common lab interface."""
from pathlib import Path
import re
import time


def start(lab,count,core):
    source = Path(lab.config["radio_configs"])
    gnb = (source / "gnb.yaml").read_text()
    for field in ("linkIp", "ngapIp", "gtpIp"):
        gnb = re.sub(rf"{field}: .*", f"{field}: 127.0.0.2", gnb)
    (lab.output / "gnb.yaml").write_text(gnb)
    ue = (source / "ue.yaml").read_text().replace("- 127.0.0.1", "- 127.0.0.2")
    for algorithm in ("IA1", "IA3", "EA1", "EA3"):
        ue = ue.replace(f"{algorithm}: true", f"{algorithm}: false")
    (lab.output / "ue.yaml").write_text(ue)
    bins = Path(lab.config["ueransim"]) / "build"
    lab.start("gnb", [bins / "nr-gnb", "-c", lab.output / "gnb.yaml"], role="gnb")
    lab.wait(lambda: "NG Setup procedure is successful" in (lab.output / "gnb.log").read_text())
    start = time.monotonic()
    lab.start("ue", [bins / "nr-ue", "-c", lab.output / "ue.yaml", "-r", "-l", "--num-of-UE", str(count)], role="ue")
    interfaces = [f"uesimtun{i}" for i in range(count)]
    lab.wait(lambda: all(i in lab.ns(lab.core_ns, ["ip", "-o", "-4", "addr"]) for i in interfaces))
    elapsed = time.monotonic() - start
    return elapsed*1000,interfaces
