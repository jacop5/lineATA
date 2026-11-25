import threading
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, RING_ON
from bridge.transport import ControlQueue


def test_waits_for_matching_response_while_events_continue():
    clock = Mock(return_value=10)
    q = ControlQueue(clock)
    write = Mock()
    q.submit(bytes.fromhex('60031e'))
    q.submit(bytes.fromhex(RING_ON[0]))
    q.pump(write)
    q.pump(write)
    write.assert_called_once_with(bytes.fromhex('60031e'), 2000)
    assert not q.acknowledge(bytes.fromhex('a5040100'))
    assert not q.acknowledge(bytes.fromhex('400344'))
    q.pump(write)
    assert write.call_count == 1
    assert q.acknowledge(bytes.fromhex('600344'))
    q.pump(write)
    assert write.call_args.args[0] == bytes.fromhex(RING_ON[0])


def test_missing_ack_fails_without_replaying_hardware_write():
    clock = Mock(return_value=10)
    q = ControlQueue(clock)
    write = Mock()
    q.submit(bytes.fromhex('67043177'), timeout=500)
    q.pump(write)
    clock.return_value = 10.5
    with pytest.raises(TimeoutError, match='67'):
        q.pump(write)
    assert write.call_count == 1


def test_same_opcode_commands_each_need_their_own_ack():
    q = ControlQueue()
    write = Mock()
    for h in ('67043177', '67040b10'):
        q.submit(bytes.fromhex(h))
    q.pump(write)
    q.acknowledge(bytes.fromhex('6702'))
    q.pump(write)
    assert q.pending.data == bytes.fromhex('67040b10')
    assert q.acknowledge(bytes.fromhex('6702'))
    assert not q.pending


def test_cancelled_ring_edges_are_not_sent_after_answer():
    ata = LinearATA({})
    ata.usb_active = True
    ata.usb_owner = threading.get_ident()
    ata.dev = Mock()
    ata.ringing = True
    ata.call = object()
    ata.ring_deadline = 0
    ata.ring_tick(100)
    ata.drain_commands()  # the first status read is already on the wire
    ata.ringing = False
    ata.handle_frame(bytes.fromhex('600344'))
    ata.drain_commands()  # queued ring writes must now be discarded
    ata.dev.write.assert_called_once()
    assert ata.dev.write.call_args.args[1] == bytes.fromhex('60031e')


def test_usb_owner_also_queues_commands_and_does_not_bypass_ack():
    ata = LinearATA({})
    ata.dev = Mock()
    ata.usb_active = True
    ata.usb_owner = threading.get_ident()
    ata.write('a00301')
    ata.write('67043177')
    ata.dev.write.assert_not_called()
    ata.drain_commands()
    ata.handle_frame(bytes.fromhex('a5040100'))  # handled while ACK outstanding
    assert ata.ports[1]['hook']
    ata.drain_commands()
    assert ata.dev.write.call_count == 1
    ata.handle_frame(bytes.fromhex('a002'))
    ata.drain_commands()
    assert ata.dev.write.call_count == 2


@pytest.mark.parametrize('data', [b'', b'\x67\x01', b'\x67\x04\x31', b'\x00\x02'])
def test_invalid_control_frame_rejected(data):
    with pytest.raises(ValueError):
        ControlQueue().submit(data)
