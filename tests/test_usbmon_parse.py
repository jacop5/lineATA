import struct

import pytest

from tools.usbmon_parse import HDR, parse_record, read_pcap
from tools.audit_captures import audit


def capture(tmp_path, body=b'', linktype=220, incl=None, orig=None):
    header = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, linktype)
    if incl is not None:
        header += struct.pack('<IIII', 1, 20, incl, orig if orig is not None else incl)
    path = tmp_path / 'test.pcap'
    path.write_bytes(header + body)
    return path


def test_wrong_link_type_cannot_be_audited_as_usb(tmp_path):
    with pytest.raises(ValueError, match='LINKTYPE'):
        list(read_pcap(capture(tmp_path, linktype=1)))


def test_partial_record_header_is_not_clean_eof(tmp_path):
    with pytest.raises(ValueError, match='header'):
        list(read_pcap(capture(tmp_path, b'\0' * 10)))


@pytest.mark.parametrize('body,incl,orig', [(b'ab', 3, 3), (b'ab', 2, 3)])
def test_incomplete_records_are_rejected(tmp_path, body, incl, orig):
    with pytest.raises(ValueError, match='truncat'):
        list(read_pcap(capture(tmp_path, body, incl=incl, orig=orig)))


def test_usbmon_payload_cannot_be_silently_shortened():
    values = [0] * 18
    values[1:5] = [67, 3, 129, 95]
    values[11:14] = [4, 4, b'\0' * 8]
    header = HDR.pack(*values)
    with pytest.raises(ValueError, match='payload'):
        parse_record(header + b'\xa5\x04')
    assert parse_record(header + bytes.fromhex('a5040100'))['payload'] == bytes.fromhex('a5040100')


def test_short_usb_header_cannot_be_silently_omitted():
    with pytest.raises(ValueError, match='header'):
        parse_record(b'\0' * 63)


def usb_record(typ, xfer, ep, payload, uid=1, length=None, status=0):
    values = [0] * 18
    values[0:6] = [uid, typ, xfer, ep, 10, 1]
    values[10] = status
    values[11:14] = [len(payload) if length is None else length, len(payload), b'\0' * 8]
    return HDR.pack(*values) + payload


def write_records(tmp_path, records):
    body = b''.join(struct.pack('<IIII', 1, i * 1000, len(r), len(r)) + r
                    for i, r in enumerate(records))
    return capture(tmp_path, body)


def descriptor(pid):
    data = bytearray(18)
    data[:2] = b'\x12\x01'
    struct.pack_into('<HH', data, 8, 0x31b7, pid)
    return usb_record(67, 2, 128, data)


def test_boot_console_is_not_mistaken_for_service_frames(tmp_path):
    result = audit(write_records(tmp_path, [descriptor(2), usb_record(83, 3, 2, b'b')]))
    assert result['devices'] == {}
    assert result['out_transfers'] == {}


def test_audit_splits_coalesced_events_and_checks_usb_completion(tmp_path):
    result = audit(write_records(tmp_path, [descriptor(1),
        usb_record(83, 3, 2, bytes.fromhex('c102'), uid=2),
        usb_record(67, 3, 2, b'', uid=2, length=2),
        usb_record(67, 3, 129, bytes.fromhex('a5040100a4043100'))]))
    device = result['devices']['1:10']
    assert [event[1] for event in device['events']] == ['a5040100', 'a4043100']
    assert device['framing_errors'] == 0
    assert result['out_transfers']['completed_out'] == 1
    assert result['out_without_completion'] == 0


def test_short_out_completion_is_reported(tmp_path):
    result = audit(write_records(tmp_path, [descriptor(1),
        usb_record(83, 3, 2, bytes.fromhex('c102'), uid=2),
        usb_record(67, 3, 2, b'', uid=2, length=1)]))
    assert result['out_transfers']['failed_or_short_out'] == 1
    assert result['out_errors'][0]['actual'] == 1


def test_partial_timeout_data_preserves_following_voice_boundaries(tmp_path):
    voice = b'\x00\xa2' + b'\xff' * 160
    result = audit(write_records(tmp_path, [descriptor(1),
        usb_record(67, 3, 129, voice[:64], status=-2),
        usb_record(67, 3, 129, voice[64:]),
        usb_record(67, 3, 129, voice)]))
    device = result['devices']['1:10']
    assert device['voice']['0x81']['count'] == 2
    assert device['framing_errors'] == 0
    assert device['in_payload_completion_statuses'] == {-2: 1, 0: 2}
