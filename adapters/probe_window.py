"""Verify all probe packets against recorded traffic timer boundaries."""
from decimal import Decimal, InvalidOperation


def verify(events, packets, counts):
    if len(events)!=len(counts) or not counts:raise ValueError('traffic timer coverage differs from UE coverage')
    windows=[]
    for rows in events:
        if len(rows)!=2 or [row.get('event') for row in rows]!=['warmup_end','test_end']:
            raise ValueError('missing or duplicate traffic timer event')
        for row in rows:
            if any(type(row.get(k)) is not int for k in ('epoch_ns','monotonic_before_ns','monotonic_after_ns')):
                raise ValueError('invalid traffic timer clock')
            span=row['monotonic_after_ns']-row['monotonic_before_ns']
            if not 0<=span<=1000000:raise ValueError('traffic clock sample took more than 1 ms')
        first,last=rows
        real_delta=last['epoch_ns']-first['epoch_ns']
        mono_delta=(last['monotonic_before_ns']+last['monotonic_after_ns']-first['monotonic_before_ns']-first['monotonic_after_ns'])/2
        if real_delta<=0 or abs(real_delta-mono_delta)>1000000:raise ValueError('traffic clock step exceeds 1 ms')
        windows.append((first['epoch_ns']+1000000,last['epoch_ns']-1000000))
    begin=max(w[0] for w in windows);end=min(w[1] for w in windows)
    if begin>=end:raise ValueError('no shared traffic interval')
    requests={};replies={}
    for line in packets.splitlines():
        try:
            stamp,source,destination,kind,identifier,sequence=line.split('\t')
            decimal=Decimal(stamp)
            if not decimal.is_finite():raise ValueError('invalid packet clock')
            instant=int(decimal*1000000000);kind=int(kind)
            address=source if kind==8 else destination
            key=(address,int(identifier,0),int(sequence,0))
        except (ValueError,InvalidOperation) as error:raise ValueError('incomplete probe packet fields') from error
        if kind not in (0,8) or address not in counts:raise ValueError('unexpected probe packet')
        target=requests if kind==8 else replies
        if key in target:raise ValueError('duplicate probe packet')
        if not begin<=instant<=end:raise ValueError('probe packet is outside the shared traffic interval')
        target[key]=instant
    if replies.keys()-requests.keys():raise ValueError('probe reply has no captured request')
    for address,expected in counts.items():
        if sum(k[0]==address for k in requests)!=expected['sent'] or sum(k[0]==address for k in replies)!=expected['received']:
            raise ValueError('probe capture and sender counters differ')
    values=[(instant-requests[key])/1000000 for key,instant in replies.items()]
    if any(value<0 for value in values):raise ValueError('probe packet clocks are reversed')
    return {'begin_epoch_ns':begin,'end_epoch_ns':end,'sent':len(requests),'received':len(replies),'rtts_ms':values,
            'boundary':'UE namespace: captured echo request to captured echo reply, fully inside all traffic timer intervals'}


def generator_contract(binary,receipt_path):
    """One binary and clock-patch contract for preparation and calibration."""
    if receipt_path is None:return None
    from pathlib import Path
    from runner.evidence import read,digest
    receipt=read(receipt_path);patch=Path(__file__).resolve().parents[1]/'patches/iperf-3.16-phase-clock.patch'
    if receipt.get('instrumentation')!='traffic-phase-v1' or receipt.get('binary_sha256')!=digest(Path(binary)) or receipt.get('patch_sha256')!=digest(patch):
        raise ValueError('traffic generator build receipt differs from its binary or clock patch')
    if {c.get('mode') for c in receipt.get('checks',[]) if c.get('passed')}!={'tcp_ul','tcp_dl','udp_ul','udp_dl'}:raise ValueError('traffic generator checks are incomplete')
    if 'pacing_patch_sha256' in receipt:
        if receipt['pacing_patch_sha256']!=digest(patch.with_name('iperf-3.16-pacing.patch')):raise ValueError('traffic generator pacing patch differs')
        checks=receipt['checks']
        if len(checks)!=4 or any(type(c.get('combined_cpu_percent')) not in (int,float) or not 0<=c['combined_cpu_percent']<=25 for c in checks):
            raise ValueError('traffic generator low-rate CPU checks failed')
    if 'udp_listener_patch_sha256' in receipt and receipt['udp_listener_patch_sha256']!=digest(patch.with_name('iperf-3.16-udp-listener.patch')):
        raise ValueError('traffic generator UDP listener patch differs')
    return 'timer-and-ue-pcap-v1'
