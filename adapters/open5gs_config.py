#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import tomllib
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT_DIR / "configs/profile.toml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Open5GS for the local benchmark profile.")
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/open5gs"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--n3-bind-host")
    parser.add_argument("--n3-advertise-host", required=True)
    parser.add_argument("--metadata", type=Path, default=Path("/etc/open5gs-benchmark.json"))
    return parser.parse_args()


def required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def write_config(path: Path, payload: dict[str, object]) -> None:
    # JSON is valid YAML. Emitting the complete pinned profile avoids mutating
    # distribution defaults with a second YAML dependency at first boot.
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def configure(args: argparse.Namespace) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", args.interface):
        raise ValueError("--interface contains unsupported characters")
    advertise = required_text(args.n3_advertise_host, "--n3-advertise-host")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", advertise):
        raise ValueError("--n3-advertise-host must be a DNS name or IPv4 address")
    bind_host = args.n3_bind_host.strip() if args.n3_bind_host else None
    if bind_host and not re.fullmatch(r"[A-Za-z0-9.:-]+", bind_host):
        raise ValueError("--n3-bind-host must be an IPv4 address or interface name")

    profile = tomllib.loads(args.profile.read_text(encoding="utf-8"))
    radio = profile["radio"]
    user_plane = profile["user_plane"]
    mcc = required_text(radio["mcc"], "radio.mcc")
    mnc = required_text(radio["mnc"], "radio.mnc")
    sd = required_text(radio["sd"], "radio.sd")
    dnn = required_text(radio["apn"], "radio.apn")
    tac = int(radio["tac"])
    sst = int(radio["sst"])
    subnet = ipaddress.ip_network(required_text(user_plane["ue_subnet"], "user_plane.ue_subnet"))
    gateway = ipaddress.ip_address(required_text(user_plane["ue_gateway"], "user_plane.ue_gateway"))
    if gateway not in subnet:
        raise ValueError("user_plane.ue_gateway must be inside user_plane.ue_subnet")

    session = [{"subnet": str(subnet), "gateway": str(gateway), "dnn": dnn}]
    logger = lambda name: {"file": {"path": f"/var/log/open5gs/{name}.log"}}
    sbi_client = {"scp": [{"uri": "http://127.0.0.200:7777"}]}
    args.config_dir.mkdir(parents=True, exist_ok=True)
    write_config(args.config_dir / "nrf.yaml", {
        "logger": logger("nrf"),
        "global": {"max": {"ue": 1024}},
        "nrf": {"serving": [{"plmn_id": {"mcc": mcc, "mnc": mnc}}], "sbi": {"server": [{"address": "127.0.0.10", "port": 7777}]}},
    })
    write_config(args.config_dir / "amf.yaml", {
        "logger": logger("amf"),
        "global": {"max": {"ue": 1024}},
        "amf": {
            "sbi": {"server": [{"address": "127.0.0.5", "port": 7777}], "client": sbi_client},
            "ngap": {"server": [{"dev": args.interface}]},
            "metrics": {"server": [{"address": "127.0.0.5", "port": 9090}]},
            "guami": [{"plmn_id": {"mcc": mcc, "mnc": mnc}, "amf_id": {"region": 2, "set": 1}}],
            "tai": [{"plmn_id": {"mcc": mcc, "mnc": mnc}, "tac": tac}],
            "plmn_support": [{"plmn_id": {"mcc": mcc, "mnc": mnc}, "s_nssai": [{"sst": sst, "sd": sd}]}],
            "security": {"integrity_order": ["NIA2", "NIA1", "NIA0"], "ciphering_order": ["NEA0", "NEA1", "NEA2"]},
            "network_name": {"full": "Open5GS benchmark", "short": "Open5GS"},
            "amf_name": "open5gs-amf0",
            "time": {"t3512": {"value": 540}},
        },
    })
    write_config(args.config_dir / "smf.yaml", {
        "logger": logger("smf"),
        "global": {"max": {"ue": 1024}},
        "smf": {
            "sbi": {"server": [{"address": "127.0.0.4", "port": 7777}], "client": sbi_client},
            "pfcp": {"server": [{"address": "127.0.0.4"}], "client": {"upf": [{"address": "127.0.0.7"}]}},
            "gtpc": {"server": [{"address": "127.0.0.4"}]},
            "gtpu": {"server": [{"address": "127.0.0.4"}]},
            "metrics": {"server": [{"address": "127.0.0.4", "port": 9090}]},
            "session": session,
            "dns": ["8.8.8.8", "8.8.4.4"],
            "mtu": int(user_plane["mtu"]),
            "info": [{"s_nssai": [{"sst": sst, "sd": sd, "dnn": [dnn]}], "tai": [{"plmn_id": {"mcc": mcc, "mnc": mnc}, "tac": tac}]}],
        },
    })
    write_config(args.config_dir / "upf.yaml", {
        "logger": logger("upf"),
        "global": {"max": {"ue": 1024}},
        "upf": {
            "pfcp": {"server": [{"address": "127.0.0.7"}]},
            "gtpu": {"server": [{**({"address": bind_host} if bind_host else {"dev": args.interface}), "advertise": advertise}]},
            "session": session,
            "metrics": {"server": [{"address": "127.0.0.7", "port": 9090}]},
        },
    })
    write_config(args.config_dir / "nssf.yaml", {
        "logger": logger("nssf"),
        "global": {"max": {"ue": 1024}},
        "nssf": {"sbi": {"server": [{"address": "127.0.0.14", "port": 7777}], "client": {**sbi_client, "nsi": [{"uri": "http://127.0.0.10:7777", "s_nssai": {"sst": sst, "sd": sd}}]}}},
    })
    for name, address in (("pcf", "127.0.0.13"), ("udr", "127.0.0.20")):
        write_config(args.config_dir / f"{name}.yaml", {
            "db_uri": "mongodb://127.0.0.1/open5gs",
            "logger": logger(name),
            "global": {"max": {"ue": 1024}},
            name: {"sbi": {"server": [{"address": address, "port": 7777}], "client": sbi_client}},
        })

    metadata = {
        "core_implementation": "open5gs",
        "profile": str(args.profile),
        "config_dir": str(args.config_dir),
        "interface": args.interface,
        "n3_bind_host": bind_host,
        "n3_advertise_host": advertise,
        "plmn": f"{mcc}/{mnc}",
        "tac": tac,
        "slice": f"sst={sst} sd={sd}",
        "dnn": dnn,
        "ue_subnet": str(subnet),
    }
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def main() -> int:
    args = parse_args()
    print(json.dumps(configure(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
