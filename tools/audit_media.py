#!/usr/bin/env python3
"""Summarize USB/PCMU timing without printing audio contents.

Network input is microsecond pcap, LINKTYPE_LINUX_SLL2 (tcpdump -i any).
Identical RTP packets mirrored across bridge interfaces are counted once.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import socket
import statistics
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.media import pcmu_payload
from tools.audit_captures import audit


def audit_network(path):
    streams = defaultdict(list)
    interfaces = defaultdict(set)
    seen = set()
    duplicates = 0
    with open(path, 'rb') as source:
        header = source.read(24)
        if len(header) != 24 or header[:4] != b'\xd4\xc3\xb2\xa1' or struct.unpack_from('<I', header, 20)[0] != 276:
            raise ValueError('expected little-endian microsecond LINUX_SLL2 pcap')
        while record := source.read(16):
            if len(record) != 16:
                raise ValueError('truncated pcap record header')
            seconds, microseconds, size, original = struct.unpack('<IIII', record)
            packet = source.read(size)
            if len(packet) != size or size != original:
                raise ValueError('truncated network packet')
            if len(packet) < 40 or packet[:2] != b'\x08\x00':
                continue
            interface = struct.unpack_from('!I', packet, 4)[0]
            ip = packet[20:]
            ihl = (ip[0] & 15) * 4
            if ip[0] >> 4 != 4 or ihl < 20 or len(ip) < ihl + 8:
                raise ValueError('invalid IPv4 header')
            if ip[9] != 17 or struct.unpack_from('!H', ip, 6)[0] & 0x3fff:
                continue  # non-UDP or fragmented; not a complete RTP datagram
            udp = ip[ihl:]
            sport, dport, length, _ = struct.unpack_from('!HHHH', udp)
            if length < 8 or length > len(udp):
                raise ValueError('invalid UDP length')
            rtp = udp[8:length]
            payload = pcmu_payload(rtp)
            if payload is None:
                continue
            sequence, timestamp, ssrc = struct.unpack_from('!HII', rtp, 2)
            key = (socket.inet_ntoa(ip[12:16]), sport, socket.inet_ntoa(ip[16:20]), dport, ssrc)
            interfaces[key].add(interface)
            identity = (key, rtp)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            streams[key].append((seconds + microseconds / 1e6, sequence, timestamp, len(payload)))
    result = {'file': Path(path).name, 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
              'mirror_or_duplicate_records_removed': duplicates, 'streams': []}
    for key, rows in streams.items():
        intervals = sorted(b[0]-a[0] for a, b in zip(rows, rows[1:]))
        summary = dict(source=f'{key[0]}:{key[1]}', destination=f'{key[2]}:{key[3]}',
                       ssrc=key[4], packets=len(rows), interfaces=sorted(interfaces[key]),
                       payload_lengths=sorted({r[3] for r in rows}))
        if intervals and rows[-1][0] > rows[0][0]:
            summary.update(rate_hz=(len(rows)-1)/(rows[-1][0]-rows[0][0]),
                           interval_ms_median=1000*statistics.median(intervals),
                           interval_ms_p95=1000*intervals[19*len(intervals)//20],
                           timestamp_steps=dict(Counter((b[2]-a[2]) & 0xffffffff for a,b in zip(rows,rows[1:]))),
                           sequence_discontinuities=sum((b[1]-a[1]) & 0xffff != 1 for a,b in zip(rows,rows[1:])))
        result['streams'].append(summary)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usb', type=Path, required=True)
    parser.add_argument('--network', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({'usb': audit(args.usb), 'network': audit_network(args.network)}, indent=2))
