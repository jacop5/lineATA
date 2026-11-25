"""PCMU RTP parsing. USB framing is handled separately."""
import struct


def pcmu_payload(packet):
    """Return PT=0 payload, accounting for CSRCs, extension and padding."""
    if len(packet) < 12 or packet[0] >> 6 != 2 or packet[1] & 0x7f:
        return None
    offset = 12 + 4 * (packet[0] & 15)
    if len(packet) < offset:
        return None
    if packet[0] & 0x10:
        if len(packet) < offset + 4:
            return None
        words = struct.unpack_from('!H', packet, offset + 2)[0]
        offset += 4 + 4 * words
    end = len(packet)
    if packet[0] & 0x20:
        padding = packet[-1]
        if not padding or padding > end - offset:
            return None
        end -= padding
    if offset >= end:
        return None
    return packet[offset:end]
