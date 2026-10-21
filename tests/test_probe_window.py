"""Failure cases before implementation: missing/duplicate clocks or packets,
clock steps, probes outside useful load, censored replies, unequal counters.
This parser isolates evidence validation; live capture remains an E2E gate.
"""
import unittest
from adapters.probe_window import verify

def clock(event,seconds):
    return {'event':event,'epoch_ns':int(seconds*1e9),'monotonic_before_ns':int((seconds-90)*1e9),'monotonic_after_ns':int((seconds-90)*1e9)+100}
class ProbeWindow(unittest.TestCase):
    def inputs(self):
        events=[[clock('warmup_end',100),clock('test_end',130)]]
        packets='102\t10.45.0.2\t192.0.2.1\t8\t11\t1\n102.001\t192.0.2.1\t10.45.0.2\t0\t11\t1\n103\t10.45.0.2\t192.0.2.1\t8\t11\t2\n'
        return events,packets,{'10.45.0.2':{'sent':2,'received':1}}
    def test_complete_reply_and_loss_use_all_requests(self):
        events,packets,counts=self.inputs();result=verify(events,packets,counts)
        self.assertAlmostEqual(result['rtts_ms'][0],1);self.assertEqual(result['sent'],2);self.assertEqual(result['received'],1)
    def test_missing_clock_and_packet_fail(self):
        events,packets,counts=self.inputs()
        for bad in [[],[events[0][:1]],[events[0]+[events[0][0]]]]:
            with self.assertRaises(ValueError):verify(bad,packets,counts)
        with self.assertRaises(ValueError):verify(events,'',counts)
    def test_outside_load_and_late_reply_fail_without_clipping(self):
        events,packets,counts=self.inputs()
        for bad in [packets.replace('102\t','99\t'),packets.replace('102.001\t','131\t')]:
            with self.assertRaises(ValueError):verify(events,bad,counts)
    def test_clock_step_and_duplicate_reply_fail(self):
        events,packets,counts=self.inputs();events[0][1]['epoch_ns']+=10000000
        with self.assertRaises(ValueError):verify(events,packets,counts)
        events,packets,counts=self.inputs()
        with self.assertRaises(ValueError):verify(events,packets+packets.splitlines()[1]+'\n',counts)

class GeneratorReceipt(unittest.TestCase):
    def test_pacing_patch_and_low_rate_cpu_checks_are_bound_to_binary(self):
        import tempfile,pathlib,json,hashlib
        from adapters.probe_window import generator_contract
        root=pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as t:
            p=pathlib.Path(t);binary=p/'iperf3';binary.write_bytes(b'fixture')
            digest=lambda f:hashlib.sha256(f.read_bytes()).hexdigest()
            receipt={'instrumentation':'traffic-phase-v1','binary_sha256':digest(binary),'patch_sha256':digest(root/'patches/iperf-3.16-phase-clock.patch'),'pacing_patch_sha256':'incorrect','checks':[{'mode':m,'passed':True,'combined_cpu_percent':2} for m in ('tcp_ul','tcp_dl','udp_ul','udp_dl')]}
            path=p/'receipt.json';path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError,'pacing'):generator_contract(binary,path)
            receipt['pacing_patch_sha256']=digest(root/'patches/iperf-3.16-pacing.patch')
            path.write_text(json.dumps(receipt));self.assertEqual(generator_contract(binary,path),'timer-and-ue-pcap-v1')
            receipt['udp_listener_patch_sha256']='incorrect';path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError,'listener'):generator_contract(binary,path)
            receipt['udp_listener_patch_sha256']=digest(root/'patches/iperf-3.16-udp-listener.patch');path.write_text(json.dumps(receipt))
            self.assertEqual(generator_contract(binary,path),'timer-and-ue-pcap-v1')
            for bad in (None,101):
                receipt['checks'][0]['combined_cpu_percent']=bad;path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError,'CPU'):generator_contract(binary,path)


class CapturePrecision(unittest.TestCase):
    """Before implementation: reject wrong precision, unknown or short headers;
    keep submicrosecond RTTs and reject a nanosecond file with coarse samples.
    """
    def test_capture_header_precision_is_verified(self):
        import pathlib,tempfile,struct
        from adapters.probe_window import capture_precision
        with tempfile.TemporaryDirectory() as t:
            p=pathlib.Path(t)/'capture.pcap'
            for order,magic in [('<','4d3cb2a1'),('>','a1b23c4d')]:
                p.write_bytes(bytes.fromhex(magic)+struct.pack(order+'HHIIII',2,4,0,0,128,276))
                self.assertEqual(capture_precision(p,'nano'),'nano')
            p.write_bytes(bytes.fromhex('d4c3b2a1')+struct.pack('<HHIIII',2,4,0,0,128,276))
            self.assertEqual(capture_precision(p,'micro'),'micro')
            with self.assertRaises(ValueError):capture_precision(p,'nano')
            for bad in (b'',b'pcap',bytes.fromhex('4d3cb2a1')):
                p.write_bytes(bad)
                with self.assertRaises(ValueError):capture_precision(p,'nano')
    def test_nanosecond_rtts_keep_submicrosecond_values(self):
        events,packets,counts=ProbeWindow().inputs()
        packets=packets.replace('102\t','102.000000123\t').replace('102.001\t','102.000000623\t')
        result=verify(events,packets,counts,require_nanoseconds=True)
        self.assertAlmostEqual(result['rtts_ms'][0],.0005)
        self.assertGreater(result['submicrosecond_timestamps'],0)
        events,packets,counts=ProbeWindow().inputs()
        with self.assertRaisesRegex(ValueError,'quantized'):verify(events,packets,counts,require_nanoseconds=True)

if __name__=='__main__':unittest.main()
