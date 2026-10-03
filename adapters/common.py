"""Public test identities and common process helpers."""
import importlib.util
import subprocess
import sys

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
