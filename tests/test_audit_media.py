import socket
import struct

from tools.audit_media import audit_network


def packet(sequence, interface=1):
    rtp = struct.pack('!BBHII', 0x80, 0, sequence, sequence * 160, 42) + b'\xff' * 160
    udp = struct.pack('!HHHH', 24000, 25000, len(rtp) + 8, 0) + rtp
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, len(udp) + 20, 0, 0, 64, 17, 0,
                     socket.inet_aton('192.0.2.1'), socket.inet_aton('192.0.2.2')) + udp
    return struct.pack('!HHIHBB8s', 0x0800, 0, interface, 1, 0, 6, b'\0' * 8) + ip


def test_bridge_mirrors_are_not_mistaken_for_double_rtp_rate(tmp_path):
    path = tmp_path / 'network.pcap'
    data = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 262144, 276)
    for sequence, micros in ((1, 0), (2, 20000)):
        for interface in (1, 2):
            record = packet(sequence, interface)
            data += struct.pack('<IIII', 1, micros, len(record), len(record)) + record
    path.write_bytes(data)
    report = audit_network(path)
    assert report['mirror_or_duplicate_records_removed'] == 2
    stream, = report['streams']
    assert stream['packets'] == 2
    assert abs(stream['rate_hz'] - 50) < .001
    assert stream['interfaces'] == [1, 2]
    assert stream['timestamp_steps'] == {160: 1}
    assert stream['sequence_discontinuities'] == 0
