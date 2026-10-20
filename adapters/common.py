"""Public test identities and common process helpers."""
import importlib.util
import os
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


def command(args, timeout=60, check=True, env=None):
    result = subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=None if env is None else {**os.environ,**env})
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr[-1500:]}")
    return result.stdout


def validate_runtime_inputs(paths, executables):
    for path in paths:
        if not path.is_file():
            raise ValueError('missing runtime input: '+str(path))
    for path in executables:
        if not path.is_file() or not os.access(path,os.X_OK):
            raise ValueError('build or install the executable: '+str(path))


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def service_profile(config):
    """One subscription and NAS contract for every native adapter."""
    import re
    cipher = config.get('cipher_algorithm', 2)
    sd = config.get('slice_sd', '010203')
    sqn = config.get('provisioned_sqn')
    if type(cipher) is not int or cipher not in (0, 2):
        raise ValueError('supported NAS cipher algorithms are 0 and 2')
    if not isinstance(sd, str) or not re.fullmatch('[0-9a-fA-F]{6}', sd):
        raise ValueError('slice SD must contain six hexadecimal digits')
    if sqn is not None and (type(sqn) is not int or not 0 <= sqn < 2**48):
        raise ValueError('SQN must be a 48-bit unsigned integer')
    return {'plmn': '00101', 'slice': '1/' + sd.lower(), 'dnn': 'internet',
            'security': 'NIA2/NEA' + str(cipher), 'cipher_algorithm': cipher,
            'slice_sd': sd.lower(), 'session_ambr_mbps': 1000, 'provisioned_sqn': sqn}


def subscriber_sqn(config, index):
    value = service_profile(config)['provisioned_sqn']
    return initial_sqn(index) if value is None else value


def subscriber_population(config,requested):
    minimum=config.get('minimum_subscribers',2)
    if type(minimum) is not int or not 1<=minimum<=253:
        raise ValueError('minimum subscribers must be an integer from 1 through 253')
    return max(requested,minimum)
