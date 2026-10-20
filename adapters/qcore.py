"""QCore benchmark binary launch through the common lab boundary.

The pinned binary must support the documented ARM, AMBR and TUN changes.
Preparation does not patch or build the core.
"""
from pathlib import Path
from .common import KEY, OPC, service_profile


def start(lab, count):
    count=max(count,2)  # Retain a second identity for the fresh-session recovery checks.
    profile = service_profile(lab.config)
    if profile['cipher_algorithm'] != 0:
        raise ValueError('this QCore adapter supports integrity-only NAS; encrypted checks remain unsupported')
    if profile['slice_sd'] != '000000' or profile['provisioned_sqn'] not in (None,32):
        raise ValueError('this QCore binary requires slice SD 000000 and first challenge SQN 32')
    for args in [
        ['ip','link','add','veth0','type','veth','peer','name','veth1'],
        ['ip','link','set','veth0','up'], ['ip','link','set','veth1','up'],
        ['ip','link','set','arp','off','dev','veth0'], ['ethtool','-K','veth0','rx','off','tx','off'],
        ['ip','tuntap','add','mode','tun','name','qcoretun'], ['ip','link','set','qcoretun','up'],
        ['sysctl','-w','net.ipv4.conf.qcoretun.rp_filter=0'],
        ['sysctl','-w','net.ipv4.conf.qcoretun.accept_local=1'],
        ['iptables','-t','nat','-A','POSTROUTING','-s','10.45.0.0/24','-o','dn0','-j','MASQUERADE']]:
        lab.ns(lab.core_ns,args)
    sims = lab.output/'sims.toml'
    sims.write_text('\n'.join(f'[imsi-{1010000000000+i:015d}]\nki = "{KEY}"\nopc = "{OPC}"' for i in range(1,count+1)))
    lab.start('core-qcore', [Path(lab.config['qcore_bin']), '--local-ip', lab.config['n3_ip'],
                           '--ran-interface-name', 'n3', '--no-dhcp', '--ue-subnet', '10.45.0.0',
                           '--sim-cred-file', sims, '--five-qi', '9'], role='core')
    lab.wait(lambda: '38412' in lab.ns(lab.core_ns,['ss','-lSnH']))
