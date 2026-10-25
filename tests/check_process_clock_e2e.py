"""Check a live child's CPU clock against its own clock and sleep behavior.

Failure modes: the observer clock is read instead of the child clock, a sleep
is counted as CPU work, short work is lost to tick rounding, or a dead process
silently becomes a zero counter. Run on Linux; no core or radio is used.
"""
import json
import pathlib
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    if sys.platform!='linux':raise SystemExit('This live CPU-clock check requires Linux')
    from adapters.process_clock import ProcessClock
    code='''import sys,time
print('ready',flush=True)
for line in sys.stdin:
 if line.strip()=='sleep':time.sleep(.05)
 else:
  end=time.process_time_ns()+3_000_000
  while time.process_time_ns()<end:pass
 print(time.process_time_ns(),flush=True)
'''
    p=subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert p.stdout.readline().strip()=='ready'
        clock=ProcessClock();previous=clock.read_ns(p.pid);deltas=[]
        for _ in range(6):
            p.stdin.write('burn\n');p.stdin.flush();own=int(p.stdout.readline());now=clock.read_ns(p.pid)
            assert own<=now and now-own<2_000_000
            assert now>previous;deltas.append(now-previous);previous=now
        assert any(1_500_000<delta<9_000_000 for delta in deltas),'Short CPU work is still tick-quantized'
        p.stdin.write('sleep\n');p.stdin.flush();own=int(p.stdout.readline());now=clock.read_ns(p.pid)
        assert now-previous<5_000_000,'Sleep time was counted as CPU work'
        assert abs(now-own)<2_000_000
        p.stdin.close();assert p.wait(timeout=5)==0
        try:clock.read_ns(p.pid)
        except OSError:pass
        else:raise AssertionError('A dead process must not produce a CPU sample')
        print(json.dumps({'passed':True,'scope':'live child CPU clock; no core performance claim',
                          'short_work_deltas_ns':deltas,'sleep_cpu_delta_ns':now-previous}))
    finally:
        if p.poll() is None:p.kill();p.wait(timeout=5)

if __name__=='__main__':main()
