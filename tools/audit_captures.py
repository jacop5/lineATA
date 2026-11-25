#!/usr/bin/env python3
"""Reproduce structural capture findings without printing audio or SIP secrets.

Times are relative to the FIRST PCAP RECORD (including other USB devices).
No electrical or codec semantics are inferred from command acknowledgements.
"""
import hashlib
import json
import statistics
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.usbmon_parse import parse_record, read_pcap
from bridge.capture_init import OPS
from bridge.framing import FrameDecoder


def audit(path):
    records = list(read_pcap(path))
    if not records:
        raise ValueError('empty capture')
    start = records[0][0]
    wire = []
    identities = {}
    submissions = {}
    transfers = Counter()
    errors = []
    for ts, data in records:
        r = parse_record(data)
        if not r:
            continue
        key = (r['bus'], r['dev'])
        if r['xfer'] == 2 and r['type'] == 67 and r['status'] == 0 and r['payload'][:2] == b'\x12\x01':
            if len(r['payload']) >= 18:
                identities[key] = struct.unpack_from('<HH', r['payload'], 8)
        r['identity'] = identities.get(key)
        if r['xfer'] != 3:
            continue
        if r['ep'] == 2 and r['identity'] == (0x31b7, 1):
            uid = (r['bus'], r['uid'])
            if r['type'] == 83:
                submissions[uid] = r
                transfers['submitted_out'] += 1
            elif r['type'] in (67, 69) and uid in submissions:
                submitted = submissions.pop(uid)
                if r['status'] == 0 and r['length'] == submitted['length']:
                    transfers['completed_out'] += 1
                else:
                    transfers['failed_or_short_out'] += 1
                    errors.append({'time': round(ts-start, 6), 'status': r['status'],
                                   'actual': r['length'], 'requested': submitted['length']})
        if (r['payload'] and r['identity'] == (0x31b7, 1)
                and ((r['type'], r['ep']) == (83, 2)
                     or ((r['type'], r['ep']) == (67, 129) and r['status'] == 0))):
            wire.append((ts - start, r))
    result = {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'time_origin': 'first pcap record', 'scope': 'descriptor-confirmed 31b7:0001 bulk only',
              'out_transfers': dict(transfers), 'out_errors': errors,
              'out_without_completion': len(submissions), 'devices': {}}
    for bus, dev in sorted({(r['bus'], r['dev']) for _, r in wire}):
        rows = [(t, r) for t, r in wire if (r['bus'], r['dev']) == (bus, dev)]
        out = [(t, r['payload'].hex()) for t, r in rows if r['type'] == 83]
        i = 0
        skipped = []
        for t, h in out:
            if i == len(OPS):
                break
            if h == OPS[i][1]:
                i += 1
            else:
                # Never dump audio or device provisioning/identity contents.
                skipped.append({'time': round(t, 6), 'opcode': h[:2],
                                'length': len(h)//2, 'sha256': hashlib.sha256(bytes.fromhex(h)).hexdigest()})
        decoders = {2: FrameDecoder(), 129: FrameDecoder()}
        frames = []
        framing_errors = 0
        for t, r in rows:
            try:
                frames.extend((t, r['ep'], frame) for frame in decoders[r['ep']].feed(r['payload']))
            except ValueError:
                framing_errors += 1
        events = [[round(t, 6), f.hex()] for t, ep, f in frames
                  if ep == 129 and f[0] in (0xa4, 0xa5)]
        voices = {}
        for ep in (2, 129):
            samples = [t for t, endpoint, f in frames if endpoint == ep and f[:2] == b'\x00\xa2']
            if len(samples) > 1:
                voices[hex(ep)] = {'count': len(samples), 'first': samples[0], 'last': samples[-1],
                    'median_interval': statistics.median(b-a for a, b in zip(samples, samples[1:]))}
        result['devices'][f'{bus}:{dev}'] = {'ops_subsequence_matched': i, 'omitted_before_ops_end': skipped,
            'events': events, 'voice': voices,
            'framing_errors': framing_errors,
            'incomplete_frame_bytes_at_end': {hex(ep): len(decoder.pending) for ep, decoder in decoders.items()},
            'out_commands_after_4s': dict(Counter(h[:2] for t, h in out if t > 4))}
    return result


if __name__ == '__main__':
    paths = [Path(x) for x in sys.argv[1:]] or sorted(Path('captures/2026-11-25').glob('*.pcap'))
    if not paths:
        sys.exit('no captures found')
    print(json.dumps([audit(p) for p in paths], indent=2))
