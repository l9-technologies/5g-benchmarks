"""Check echo delivery, a real server outage, recovery, and raw counters.

Failure modes: accepting changed payloads, hiding timeouts, losing TCP record
boundaries, reporting a dead server as recovered, or leaving child processes.
This uses loopback sockets. It is not a 5G performance measurement.
"""
import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'adapters/service_probe.py'

def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = pathlib.Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        for transport in ('tcp', 'udp'):
            receiver_files=[]
            def server():
                receiver_output=root/(transport+'-receiver-'+str(len(receiver_files))+'.jsonl')
                receiver_files.append(receiver_output)
                return subprocess.Popen([sys.executable, str(TOOL), 'server', '--transport', transport,
                    '--address', '127.0.0.1', '--port', str(port),'--output',str(receiver_output)], stdout=subprocess.DEVNULL)
            p = server()
            client = None
            try:
                time.sleep(.2)
                output = root / (transport+'.jsonl')
                client = subprocess.Popen([sys.executable, str(TOOL), 'client', '--transport', transport,
                    '--address', '127.0.0.1', '--bind', '127.0.0.1', '--port', str(port),
                    '--seconds', '3', '--interval', '.02', '--output', str(output)])
                time.sleep(.6)
                fault_ns=time.monotonic_ns()
                p.terminate(); p.wait(timeout=5)
                time.sleep(.5)
                p = server()
                time.sleep(.4)
                p.terminate(); p.wait(timeout=5)
                time.sleep(.3)
                p = server()
                assert client.wait(timeout=6) == 0
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                assert rows and all(row['transport'] == transport for row in rows)
                assert all(row['end_monotonic_ns'] >= row['start_monotonic_ns'] for row in rows)
                failed = [i for i,row in enumerate(rows) if not row['passed']]
                assert failed, 'The outage must stay visible'
                assert any(row['passed'] for row in rows[:min(failed)])
                assert sum(row['passed'] for row in rows[max(failed)+1:]) >= 10
                assert all(row['bytes'] == 1200 for row in rows if row['passed'])
                assert all(row['sent_sha256'] == row['received_sha256'] for row in rows if row['passed'])
                receiver_rows=[json.loads(line) for path in receiver_files for line in path.read_text().splitlines()]
                received={(row['stream_id'],row['sequence'],row['sha256']) for row in receiver_rows}
                assert all((row['stream_id'],row['sequence'],row['sent_sha256']) in received for row in rows if row['passed'])
                assert len({row['sequence'] for row in rows}) == len(rows)
                sys.path.insert(0,str(ROOT))
                from adapters.service_probe import recovery_result
                result=recovery_result(output,fault_ns,3)
                assert result['recovered'] and .9 <= result['first_stable_reply_seconds'] < 2.5
                assert result['failed_requests'] > 0 and result['baseline_replies'] >= 10
                assert not recovery_result(output,fault_ns,3,
                    request_floor_ns=rows[-5]['start_monotonic_ns'])['recovered']
                partial=root/(transport+'-partial.jsonl')
                partial.write_text(''.join(json.dumps(row)+'\n' for row in rows[:max(failed)+1]))
                assert not recovery_result(partial,fault_ns,3)['recovered']
            finally:
                if client and client.poll() is None:
                    client.terminate(); client.wait(timeout=5)
                if p.poll() is None:
                    p.terminate(); p.wait(timeout=5)
        # A responding peer must not pass when it returns a different payload.
        for transport in ('tcp','udp'):
            with socket.socket(type=socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM) as bad:
                bad.bind(('127.0.0.1',0)); bad.settimeout(.2)
                if transport == 'tcp':bad.listen()
                bad_port=bad.getsockname()[1]
                stop=threading.Event()
                def corrupt():
                    while not stop.is_set():
                        try:
                            if transport == 'tcp':
                                conn,_=bad.accept()
                                with conn:
                                    conn.recv(1200);conn.sendall(b'x'*1200)
                            else:
                                _,address=bad.recvfrom(1200);bad.sendto(b'x'*1200,address)
                        except TimeoutError:pass
                worker=threading.Thread(target=corrupt);worker.start()
                output=root/(transport+'-corrupt.jsonl')
                try:
                    subprocess.run([sys.executable,str(TOOL),'client','--transport',transport,
                        '--address','127.0.0.1','--bind','127.0.0.1','--port',str(bad_port),
                        '--seconds','1','--output',str(output)],check=True,timeout=5)
                    rows=[json.loads(line) for line in output.read_text().splitlines()]
                    assert rows and not any(row['passed'] for row in rows)
                    assert all('payload' in row.get('error','') for row in rows)
                finally:
                    stop.set();worker.join(timeout=2);assert not worker.is_alive()
        # Stop an actual request while its receiver withholds the reply.
        for transport in ('tcp','udp'):
            with socket.socket(type=socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM) as receiver:
                receiver.bind(('127.0.0.1',0));receiver.settimeout(3)
                if transport=='tcp':receiver.listen()
                output=root/(transport+'-inflight.jsonl')
                p=subprocess.Popen([sys.executable,str(TOOL),'client','--transport',transport,
                    '--address','127.0.0.1','--bind','127.0.0.1','--port',str(receiver.getsockname()[1]),
                    '--seconds','30','--output',str(output)])
                conn=None
                try:
                    if transport=='tcp':
                        conn,_=receiver.accept();conn.settimeout(3);assert conn.recv(1200)
                    else:assert receiver.recvfrom(1200)[0]
                    stopped=time.monotonic_ns();p.terminate();assert p.wait(timeout=3)==0
                    rows=[json.loads(line) for line in output.read_text().splitlines()]
                    assert len(rows)==1 and not rows[0]['passed']
                    assert rows[0]['start_monotonic_ns']<=stopped<=rows[0]['end_monotonic_ns']
                finally:
                    if conn:conn.close()
                    if p.poll() is None:p.kill();p.wait(timeout=3)
        print(json.dumps({'passed':True, 'transports':['tcp','udp'], 'outages_retained':True,
                          'scope':'loopback tool E2E; no core measurements'}))

if __name__ == '__main__':
    main()
