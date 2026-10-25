"""Record receiver-verified TCP or UDP replies across a service interruption."""
import argparse
import hashlib
import json
import pathlib
import signal
import socket
import socketserver
import struct
import threading
import time

SIZE = 1200

def recovery_result(path, fault_ns, deadline_seconds,request_floor_ns=None):
    text=pathlib.Path(path).read_text()
    if text and not text.endswith('\n'):
        raise ValueError('Incomplete service probe record')
    rows = [json.loads(line) for line in text.splitlines()]
    for i,row in enumerate(rows):
        if (type(row.get('sequence')) is not int or row['sequence'] != i or
                type(row.get('passed')) is not bool or
                any(type(row.get(key)) is not int for key in ['start_monotonic_ns','end_monotonic_ns']) or
                row['end_monotonic_ns'] < row['start_monotonic_ns'] or
                i and row['start_monotonic_ns'] < rows[i-1]['end_monotonic_ns'] or
                row['passed'] and (row.get('bytes') != SIZE or not isinstance(row.get('sent_sha256'),str)
                                  or len(row['sent_sha256'])!=64 or row['sent_sha256']!=row.get('received_sha256'))):
            raise ValueError('Invalid service probe record')
    before = [row for row in rows if row['passed'] and row['end_monotonic_ns'] <= fault_ns]
    after = [row for row in rows if max(fault_ns,request_floor_ns or fault_ns)<=row['start_monotonic_ns']<=fault_ns+deadline_seconds*1e9]
    last_failure=max((i for i,row in enumerate(after) if not row['passed']),default=-1)
    run=after[last_failure+1:]
    stable=run[:10] if len(run)>=10 else None
    confirmed = (stable[-1]['end_monotonic_ns']-fault_ns)/1e9 if stable else None
    first = (stable[0]['end_monotonic_ns']-fault_ns)/1e9 if stable else None
    final_ten = len(after) >= 10 and all(row['passed'] for row in after[-10:])
    return {'recovered': len(before) >= 10 and confirmed is not None and confirmed <= deadline_seconds and final_ten,
            'baseline_replies': len(before), 'attempted_requests': len(rows),
            'successful_requests': sum(row['passed'] for row in rows),
            'failed_requests': sum(not row['passed'] for row in rows),
            'baseline_failed_requests':sum(not row['passed'] and row['start_monotonic_ns']<fault_ns for row in rows),
            'observation_failed_requests':sum(not row['passed'] for row in after),
            'final_ten_replies_passed': final_ten,
            'first_stable_reply_seconds': first, 'confirmed_seconds': confirmed,
            'last_before_to_first_stable_reply_seconds':
                (stable[0]['end_monotonic_ns']-before[-1]['end_monotonic_ns'])/1e9 if before and stable else None,
            'boundary': 'Ten verified replies after the last failed request in the observation window, with no later failed requests; TCP reconnects are permitted; UE/gNB restart is not permitted'}

def receiver_record(server,data,peer,transport):
    row={'transport':transport,'received_monotonic_ns':time.monotonic_ns(),
         'peer':list(peer),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
         'sequence':struct.unpack('!Q',data[:8])[0] if len(data)>=8 else None,
         'stream_id':data[8:24].hex()}
    with server.audit_lock:
        if server.audit is not None:
            server.audit.write(json.dumps(row)+'\n');server.audit.flush()

def receive(sock, count):
    data = bytearray()
    while len(data) < count:
        block = sock.recv(count-len(data))
        if not block:
            raise ConnectionError('TCP peer closed before a complete reply')
        data.extend(block)
    return bytes(data)

class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            while True:
                data=receive(self.request,SIZE)
                receiver_record(self.server,data,self.client_address,'tcp')
                self.request.sendall(data)
        except (OSError, ConnectionError):
            return

class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        receiver_record(self.server,data,self.client_address,'udp')
        sock.sendto(data, self.client_address)

class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

class UDPServer(socketserver.UDPServer):
    allow_reuse_address = True
    daemon_threads = True

def client(args):
    stopping=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    sock = None
    sequence = 0
    start = time.monotonic()
    next_probe = start
    stream_id=hashlib.sha256((args.bind+'|'+args.transport).encode()).digest()[:16]
    with args.output.open('x') as output:
        while time.monotonic()-start < args.seconds and not stopping.is_set():
            payload = struct.pack('!Q', sequence) + stream_id + bytes((sequence % 251,))*(SIZE-24)
            row = {'sequence': sequence, 'transport': args.transport,
                   'start_monotonic_ns': time.monotonic_ns(), 'passed': False, 'bytes': 0,
                   'stream_id':stream_id.hex(),'sent_sha256':hashlib.sha256(payload).hexdigest(),
                   'received_sha256':None}
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if args.transport == 'tcp' else socket.SOCK_DGRAM)
                    if args.transport == 'tcp':
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(.1)
                    sock.bind((args.bind, 0))
                    sock.connect((args.address, args.port))
                if args.transport == 'tcp':
                    sock.sendall(payload)
                    reply = receive(sock, SIZE)
                else:
                    sock.send(payload)
                    reply = sock.recv(SIZE+1)
                row['received_sha256']=hashlib.sha256(reply).hexdigest()
                if reply != payload:
                    raise ValueError('Reply payload or sequence differs')
                row.update(passed=True, bytes=len(reply))
            except (OSError, ValueError, ConnectionError) as error:
                row['error'] = str(error)
                if sock is not None:
                    sock.close()
                    sock = None
            row['end_monotonic_ns'] = time.monotonic_ns()
            output.write(json.dumps(row)+'\n')
            output.flush()
            sequence += 1
            next_probe = max(next_probe+args.interval, time.monotonic())
            time.sleep(max(0, next_probe-time.monotonic()))
    if sock is not None:
        sock.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['server','client'])
    parser.add_argument('--transport', choices=['tcp','udp'], required=True)
    parser.add_argument('--address', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--bind')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--interval', type=float, default=.02)
    parser.add_argument('--output', type=pathlib.Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.action == 'server':
        server = TCPServer if args.transport == 'tcp' else UDPServer
        with server((args.address,args.port), TCP if args.transport == 'tcp' else UDP) as service:
            service.audit=args.output.open('x') if args.output is not None else None
            service.audit_lock=threading.Lock();service.timeout=.1
            stopping=threading.Event();signal.signal(signal.SIGTERM,lambda *_:stopping.set())
            try:
                while not stopping.is_set():service.handle_request()
            finally:
                with service.audit_lock:
                    if service.audit is not None:service.audit.close();service.audit=None
    else:
        if not args.bind or args.output is None or args.seconds is None or not 1 <= args.seconds <= 3600 or not .001 <= args.interval <= 1:
            parser.error('client needs bind, output, 1..3600 seconds and .001..1 second interval')
        client(args)

if __name__ == '__main__':
    main()
