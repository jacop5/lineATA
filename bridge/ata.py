#!/usr/bin/env python3
"""LineATA: native USB-to-FXS ATA for Keenetic Linear (KN-3110).

Replaces Keenetic's nvox with a self-contained daemon:
  USB  : Si32287/EFM32 mailbox protocol (init replay, hook/DTMF events,
         tone/ring commands, 160-byte u-law voice frames)
  SIP  : minimal UA (REGISTER/INVITE/ACK/BYE, digest auth, PCMU RTP)

The USB protocol was derived from a byte-exact usbmon capture of nvox
0.8.2.96 driving this dongle; see bridge/capture_init.py.

Frame format (both directions, bulk endpoints 0x81 in / 0x02 out):
    [type][len][payload...]        len = total frame length
    0xA5 04 <1|0> 00      hook change
    0xA4 04 <ascii> 00    DTMF digit
    0x64 ...              physical detect
    0x00 A2 <160 bytes>   u-law audio, 20 ms
"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import random
import queue
import re
import socket
import struct
import sys
import threading
import time
import uuid

import usb.core
import usb.util

try:
    from .capture_init import OPS
    from .framing import FrameDecoder
    from .media import pcmu_payload, advance_deadline
    from .transport import ControlQueue
except ImportError:
    from capture_init import OPS
    from framing import FrameDecoder
    from media import pcmu_payload, advance_deadline
    from transport import ControlQueue

VID = 0x31B7
PID_CDC = 0x0002
PID_LINEAR = 0x0001
EP_IN = 0x81
EP_OUT = 0x02

# Dial tone: nvox sends this 94-byte frame ~7 ms after OFFHOOK. NOTE: a second
# 94-byte frame with different middle bytes was observed after the 2nd DTMF
# digit — the frame may embed per-digit tone-generator state; verbatim replay
# of the first frame is proven ACKed, audibility confirmed only under nvox.
DIAL_TONE = (
    "6e5e0002317701001a0000008f0701001b000000260001001c00000000000000"
    "331f00003240000035000000340001001d000000000801001e00000000000100"
    "1f0000000000000037000000360000003900000038000000304600013101"
)
TONE_OFF = "67043177"
# Incoming-call announce: nvox sends these for BOTH port command families when
# the INVITE arrives (capture t=4.423, ~1.9 s before the first ring frame) and
# again right before answering (t=7.076). The frame writes reg 0x1e = 0x04;
# afterwards reads of 0x1e return 0x44 (call state) instead of 0x11 (idle).
# Its omission is a candidate cause of the native ring failure, not a proven cause.
CALL_ANNOUNCE = ["6e0a0002168000001e04", "4e0a0002168000001e04"]
# Ring edges: nvox reads reg 0x1e on both port families, then sends the ring
# frame for BOTH families, ~400 ms on / ~350 ms off (capture t=6.326..7.076).
RING_POLL = ["60031e", "40031e"]
RING_ON = ["6e0e0002500400001e0100015004", "4e0e0002500400001e0100015004"]
RING_OFF = ["6e0e0002500400001e0200015004", "4e0e0002500400001e0200015004"]
RING_ON_MS = 400
RING_OFF_MS = 350
# Answer (offhook during inbound call), verbatim from capture t=7.134..7.138
# (the 6e12.. frame was missing from earlier revisions).
ANSWER = ["67043177", "61040b01", "210600010100",
          "6e1200000c0900020d0300000e0900020f03", "66040b10"]
# Oracle sends this ~200 ms later, immediately before USB audio starts.
# Native controlled test confirmed it enables handset->host voice frames.
MEDIA_ENABLE = '270300'
MEDIA_ENABLE_DELAY = 0.2
# Hangup (ONHOOK during a call), verbatim from capture t=16.111..16.116.
HANGUP = ["270303", "210600000002", "0302", "67043177", "67040b10"]

PT_PCMU = 0


def sip_header(text, name):
    match = re.search(rf'^{re.escape(name)}:\s*([^\r\n]+)', text, re.M | re.I)
    return match.group(1) if match else ''


def transaction_key(text):
    return (sip_header(text, 'Call-ID'), sip_header(text, 'CSeq'), sip_header(text, 'Via'))


# ---------------------------------------------------------------- usb boot
def _find_tty():
    for pat in ("/dev/ttyACM*", "/dev/cu.usbmodem*"):
        import glob
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def reset_to_bootloader(timeout=6.0):
    d = usb.core.find(idVendor=VID)
    if d is not None and d.idProduct == PID_CDC:
        return True
    if d is not None:
        try:
            d.reset()
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = usb.core.find(idVendor=VID)
        if d is not None and d.idProduct == PID_CDC:
            return True
        time.sleep(0.2)
    return False


def boot_app(timeout=10.0):
    if usb.core.find(idVendor=VID, idProduct=PID_LINEAR) is not None:
        return True
    tty = _find_tty()
    if not tty:
        return False
    import termios
    fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, b"b")
    finally:
        os.close(fd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if usb.core.find(idVendor=VID, idProduct=PID_LINEAR) is not None:
            time.sleep(2.5)
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- SIP utils
def _md5(s):
    return hashlib.md5(s.encode() if isinstance(s, str) else s).hexdigest()


def parse_digest(challenge, method, uri, user, password):
    """Build Authorization/Proxy-Authorization from a WWW-Authenticate value."""
    fields = {key.lower(): quoted or plain for key, quoted, plain in
              re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', challenge)}
    realm = fields.get("realm", "")
    nonce = fields.get("nonce", "")
    qop = fields.get("qop")
    opaque = fields.get("opaque")
    algo = fields.get("algorithm", "MD5").upper()
    if algo != 'MD5':
        raise ValueError('unsupported SIP digest algorithm: ' + algo)
    if not realm or not nonce:
        raise ValueError('incomplete SIP digest challenge')
    if qop and 'auth' not in {value.strip() for value in qop.split(',')}:
        raise ValueError('unsupported SIP digest qop')
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        nc = "00000001"
        cnonce = uuid.uuid4().hex[:16]
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', 'response="%s"' % resp, "qop=auth",
                 f"nc={nc}", f'cnonce="{cnonce}"', f"algorithm={algo}"]
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
        parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                 f'uri="{uri}"', 'response="%s"' % resp, f"algorithm={algo}"]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def sdp_local(addr, port):
    sess = str(random.getrandbits(63))
    return (
        "v=0\r\n"
        f"o=- {sess} 1 IN IP4 {addr}\r\n"
        "s=LineATA\r\n"
        f"c=IN IP4 {addr}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/AVP {PT_PCMU}\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=ptime:20\r\n"
        "a=sendrecv\r\n"
    )


def parse_sdp(body):
    """Select the first audio stream supported by this IPv4/PCMU UA.

    A media-level connection overrides the session connection. Connections
    belonging to a video or subsequent media section must not leak into audio.
    Disabled streams and unsupported transports/codecs cannot carry our audio.
    """
    body = body.split('\r\n\r\n', 1)[-1]
    session_ip = None
    audio_ip = None
    audio = None
    in_media = False
    in_audio = False
    for line in body.splitlines():
        fields = line.split()
        if line.startswith('m='):
            if audio is not None:
                break
            in_media = True
            in_audio = bool(fields and fields[0] == 'm=audio')
            if in_audio:
                audio = fields
        elif line.startswith('c=') and len(fields) == 3 and fields[:2] == ['c=IN', 'IP4']:
            if in_audio:
                audio_ip = fields[2]
            elif not in_media:
                session_ip = fields[2]
    if not audio or len(audio) < 4 or audio[2] != 'RTP/AVP' or '0' not in audio[3:]:
        return None, None
    ip = audio_ip or session_ip
    try:
        address = ipaddress.IPv4Address(ip)
        port = int(audio[1])
    except (ValueError, TypeError, ipaddress.AddressValueError):
        return None, None
    if address.is_unspecified or address.is_multicast or not 0 < port <= 65535:
        return None, None
    return str(address), port


# ---------------------------------------------------------------- the ATA
class LinearATA:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dev = None
        self.ports = {1: {"hook": False, "last_dtmf": 0.0}}
        self.decoder = FrameDecoder()
        self.usb_owner = None
        self.usb_active = False
        self.usb_commands = ControlQueue()
        self.ring_deadline = 0.0
        self.ring_edge = False
        self.last_heart = 0.0
        self.heart_state = 0
        self.audio_lock = threading.Lock()
        self.in_audio = bytearray()
        self.out_audio = bytearray()
        self.call = None            # SIPCall
        self.invite_transactions = {}
        self.invite_acks = {}
        self.final_responses = {}
        self.dialing = ""
        self.dial_timer = 0.0
        self.ringing = False
        self.stop = threading.Event()
        self.stat_in = 0
        self.stat_out = 0
        self.stat_last = 0.0
        self.debug_frames = False   # log raw IN frames (ONHOOK-loss debugging)

    # ---- logging
    def log(self, *a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)

    # ---- usb
    def connect(self):
        if usb.core.find(idVendor=VID, idProduct=PID_LINEAR) is None:
            if self.cfg.get("cold", True):
                self.log("usb: cold boot")
                reset_to_bootloader()
                if not boot_app():
                    return False
        dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if dev is None:
            return False
        try:
            dev.set_configuration()
        except Exception:
            pass
        try:
            usb.util.claim_interface(dev, cfg := dev.get_active_configuration()[(0, 0)])
        except Exception:
            pass
        self.dev = dev
        return True

    def write(self, hexstr, timeout=2000, guard=None):
        data = bytes.fromhex(hexstr)
        if self.usb_active:
            self.usb_commands.submit(data, timeout, guard)
        else:
            self.dev.write(EP_OUT, data, timeout)

    def drain_commands(self):
        # Only usb_loop touches libusb once it starts. A write lock alone is
        # insufficient: a competing reader can steal hook events or ACKs.
        if threading.get_ident() != self.usb_owner:
            raise RuntimeError('USB commands must be drained by the USB owner')
        self.usb_commands.pump(lambda data, timeout: self.dev.write(EP_OUT, data, timeout))

    def read(self, timeout=50, size=4096):
        try:
            return bytes(self.dev.read(EP_IN, size, timeout))
        except usb.core.USBTimeoutError:
            return None

    def usb_init(self):
        self.log("usb: init (%d frames)" % len(OPS))
        self.decoder = FrameDecoder()
        for delay_ms, frame in OPS:
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            command = bytes.fromhex(frame)
            try:
                self.write(frame)
                deadline = time.monotonic() + 0.8
                matched = False
                while time.monotonic() < deadline and not matched:
                    reply = self.read(80)
                    for response in self.decoder.feed(reply or b''):
                        if response[0] == command[0]:
                            matched = True
                        elif response[0] in (0xa4, 0xa5, 0x64):
                            # Do not execute call control in the middle of init.
                            self.log('usb: init event', response.hex())
                if not matched:
                    self.log('usb: init response timeout', frame[:12])
                    return False
            except (usb.core.USBError, ValueError) as exc:
                self.log('usb: init failed', frame[:12], exc)
                return False
            # 0x1d is a register READ, not a wait-until-zero primitive.
            # Oracle startup replies include 0x81, 0x85 and 0x90. Repeating
            # every captured read until zero adds unobserved writes and delays.
        self.log("usb: init replay acknowledged (hardware readiness unverified)")
        return True

    # ---- audio plumbing (µ-law passthrough)
    def push_usb_audio(self, pcm):
        with self.audio_lock:
            self.in_audio += pcm
            # keep the buffer bounded if nobody consumes
            if len(self.in_audio) > 32000:
                del self.in_audio[:len(self.in_audio) - 3200]

    def take_usb_audio(self, n):
        with self.audio_lock:
            if len(self.in_audio) < n:
                return None
            out = bytes(self.in_audio[:n])
            del self.in_audio[:n]
            return out

    def queue_rtp_audio(self, pcm):
        with self.audio_lock:
            self.out_audio += pcm
            if len(self.out_audio) > 32000:
                del self.out_audio[:len(self.out_audio) - 3200]

    def take_rtp_audio(self):
        with self.audio_lock:
            if len(self.out_audio) < 160:
                return None
            out = bytes(self.out_audio[:160])
            del self.out_audio[:160]
            return out

    # ---- frames
    def handle_frame(self, f):
        if self.usb_commands.acknowledge(f):
            return
        t = f[0]
        if t == 0xA5 and len(f) >= 3:
            hook = bool(f[2])
            self.ports[1]["hook"] = hook
            self.log("EVENT hook", "OFFHOOK" if hook else "ONHOOK")
            if hook:
                self.dialing = ""
                self.dial_timer = time.time()
                if self.ringing:
                    self.ringing = False
                    self.answer_incoming()
                elif self.call is None:
                    self.write(DIAL_TONE)
            else:
                # Audio shutdown belongs to an active dialog. Repeating it
                # after remote BYE, or while only playing dial tone, is not
                # the captured call teardown and can reset the firmware.
                call = self.call
                frames = HANGUP if call and call.established else [TONE_OFF]
                for fr in frames:
                    try:
                        self.write(fr)
                    except usb.core.USBError:
                        break
                if self.ringing:
                    self.stop_ringing()
                if self.call is not None:
                    self.call.hangup()
                    self.call = None
                self.dialing = ""
        elif t == 0xA4 and len(f) >= 3:
            ch = chr(f[2]) if 0x20 <= f[2] < 0x7F else "?"
            self.log("EVENT dtmf", repr(ch))
            if self.call is not None or not self.ports[1]["hook"]:
                return  # in-call digits must never originate another call
            self.write(TONE_OFF)
            if ch == "#":
                self.try_dial()
            else:
                self.dialing += ch
                self.dial_timer = time.time()
        elif t == 0x64:
            self.log("EVENT phys", f.hex())
        elif t == 0x00 and len(f) >= 3:
            self.push_usb_audio(f[2:])
            self.stat_in += 1
        elif len(f) > 2:
            self.log("EVENT other", f.hex())

    def try_dial(self):
        number = self.dialing
        self.dialing = ""
        if not number:
            return
        self.log("dialing", number)
        call = SIPCall(self, number)
        self.call = call
        self.invite_transactions[call.call_id] = call

        def originate():
            try:
                connected = call.invite()
            except (OSError, ValueError) as exc:
                self.log('sip: call transport failed', exc)
                connected = False
            finally:
                self.invite_transactions.pop(call.call_id, None)
            if not connected:
                self.log("call failed")
                call.close()
                if self.call is call:
                    self.call = None
                self.write(TONE_OFF)

        # SIP waits must never block hook events, USB media or heartbeat.
        threading.Thread(target=originate, daemon=True).start()

    def start_ringing(self, call):
        self.ringing = True
        self.call = call
        call.incoming_answered = False
        for fr in CALL_ANNOUNCE:
            self.write(fr, guard=lambda: self.ringing and self.call is call)
        self.ring_edge = False
        # Oracle waits ~2 seconds between initial preparation and first ring;
        # caller-ID activity may explain that gap. Preserve it until tested.
        self.ring_deadline = time.monotonic() + 2.0

    def ring_tick(self, now):
        if not self.ringing or now < self.ring_deadline:
            return
        self.ring_edge = not self.ring_edge
        edges = RING_ON if self.ring_edge else RING_OFF
        call = self.call
        # Interleave per-family read and edge exactly as the oracle does.
        # Replies go through the same decoder as asynchronous events.
        for poll, edge in zip(RING_POLL, edges):
            self.write(poll, guard=lambda: self.ringing and self.call is call)
            self.write(edge, guard=lambda: self.ringing and self.call is call)
        self.ring_deadline = now + (RING_ON_MS if self.ring_edge else RING_OFF_MS) / 1000

    def answer_incoming(self):
        call = self.call
        if not call or not getattr(call, "incoming", False):
            return
        try:
            call.prepare_media()
        except OSError as exc:
            self.log('rtp: cannot reserve local port', exc)
            self.send_response(call.invite_text, call.remote, 500, 'Server Internal Error')
            self.stop_ringing()
            call.close()
            self.call = None
            return
        for fr in CALL_ANNOUNCE[:1] + ANSWER:  # only active family at answer
            try:
                self.write(fr, guard=lambda: self.call is call and not call.closed)
            except usb.core.USBError:
                pass
        body = sdp_local(self.cfg["local_ip"], call.local_rtp)
        ip, rport = parse_sdp(call.invite_text)
        call.remote_ip, call.remote_rtp = ip, rport
        call.start_media()
        self.send_response(call.invite_text, call.remote, 200, "OK", body)

    def stop_ringing(self):
        if self.ringing:
            self.ringing = False
            for fr in RING_OFF:
                try:
                    self.write(fr)
                except usb.core.USBError:
                    break

    # ---- main loops
    def media_tick(self, now):
        call = self.call
        if call and call.media and call.media_enable_due and now >= call.media_enable_due:
            call.media_enable_due = 0
            self.write(MEDIA_ENABLE, guard=lambda: self.call is call and not call.closed)

    def usb_loop(self):
        self.usb_owner = threading.get_ident()
        try:
            self._usb_loop()
        except (usb.core.USBError, ValueError, TimeoutError, RuntimeError) as exc:
            self.log('usb: fatal transport error', exc)
            self.stop.set()
            self.media_stop()
        finally:
            usb.util.dispose_resources(self.dev)

    def _usb_loop(self):
        next_audio = time.monotonic()
        while not self.stop.is_set():
            self.drain_commands()
            self.ring_tick(time.monotonic())
            self.media_tick(time.monotonic())
            timeout = 5
            if self.call is not None and self.call.media:
                timeout = max(1, min(5, math.ceil((next_audio - time.monotonic()) * 1000)))
            data = self.read(timeout)
            if data:
                if self.debug_frames:
                    self.log("usb raw in:", data.hex()[:80])
                for frame in self.decoder.feed(data):
                    self.handle_frame(frame)
            now = time.time()
            if now - self.last_heart >= 1.0:
                self.last_heart = now
                self.heart_state ^= 1
                if self.heart_state:
                    try:
                        self.write("a00301", 500)
                    except usb.core.USBError:
                        pass
                else:
                    try:
                        self.write("a00300", 500)
                    except usb.core.USBError:
                        pass
            if time.time() - self.stat_last > 2.0:
                self.stat_last = time.time()
                if self.stat_in or self.stat_out:
                    self.log("audio usb_in=%d usb_out=%d" % (self.stat_in, self.stat_out))
            # dial timeout -> complete the number
            if self.dialing and time.time() - self.dial_timer > self.cfg["dial_timeout"]:
                self.try_dial()
            # Pace 160-byte packets at 20 ms, never burst-drain a backlog.
            now = time.monotonic()
            if now >= next_audio:
                next_audio = advance_deadline(next_audio, now)
                pcm = self.take_rtp_audio()
                if pcm and self.call is not None and self.call.media:
                    self.dev.write(EP_OUT, b"\x00\xa2" + pcm, 500)
                    self.stat_out += 1

    # ---- SIP registration / incoming
    def start_sip(self, register=True):
        cfg = self.cfg
        self.log('sip: starting on', cfg['local_ip'], cfg['sip_port'])
        self.sip_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip_sock.bind((cfg["local_ip"], cfg["sip_port"]))
        self.sip_sock.settimeout(0.5)
        self.sip_local_port = self.sip_sock.getsockname()[1]
        self.reg_call_id = uuid.uuid4().hex
        self.reg_cseq = 1
        self.reg_tag = uuid.uuid4().hex[:8]
        self.reg_nonce = None
        self.reg_ok = False
        self.reg_event = threading.Event()
        threading.Thread(target=self.sip_listener, daemon=True).start()
        if register:
            threading.Thread(target=self.registrar, daemon=True).start()

    def registration_request(self, expires=300):
        cfg = self.cfg
        contact = f"sip:{cfg['login']}@{cfg['local_ip']}:{self.sip_local_port}"
        uri = f"sip:{cfg['domain']}"
        self.reg_cseq += 1
        auth = ''
        if self.reg_nonce:
            auth = ('Authorization: ' + parse_digest(self.reg_nonce, 'REGISTER', uri,
                                                    cfg['login'], cfg['password']) + '\r\n')
        return (
            f'REGISTER {uri} SIP/2.0\r\n'
            f"Via: SIP/2.0/UDP {cfg['local_ip']}:{self.sip_local_port};branch=z9hG4bK{uuid.uuid4().hex[:10]};rport\r\n"
            'Max-Forwards: 70\r\n'
            f"From: <sip:{cfg['login']}@{cfg['domain']}>;tag={self.reg_tag}\r\n"
            f"To: <sip:{cfg['login']}@{cfg['domain']}>\r\n"
            f'Call-ID: {self.reg_call_id}\r\nCSeq: {self.reg_cseq} REGISTER\r\n'
            f'Contact: <{contact}>;expires={expires}\r\n'
            f'{auth}User-Agent: LineATA\r\nContent-Length: 0\r\n\r\n'
        )

    def registrar(self):
        cfg = self.cfg
        self.log('sip: registrar thread up')
        expires = 300
        while not self.stop.is_set():
            try:
                msg = self.registration_request(expires)
            except ValueError as exc:
                self.log('sip: registration authentication failed', exc)
                self.stop.set()
                return
            self.reg_event.clear()
            try:
                self.sip_sock.sendto(msg.encode(), (cfg["pbx_ip"], cfg["pbx_port"]))
            except OSError as exc:
                self.log("sip: register send failed", exc)
            self.reg_event.wait(4)
            if self.reg_ok:
                self.reg_ok = False
                self.log("sip: registered as", cfg["login"])
                self.stop.wait(max(30, expires - 60))
                continue
            if self.reg_nonce:
                continue  # challenged -> re-send immediately with auth
            self.log("sip: register timeout")
            self.stop.wait(10)

    def sip_listener(self):
        while not self.stop.is_set():
            self.sip_tick(time.monotonic())
            try:
                data, addr = self.sip_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.handle_sip_message(data.decode(errors="replace"), addr)

    def handle_sip_message(self, text, addr):
        # Headers are case insensitive; normalize the names consumed below.
        head, separator, body = text.partition('\r\n\r\n')
        names = {name.lower(): name for name in ('Via', 'From', 'To', 'Call-ID',
                 'CSeq', 'Contact', 'WWW-Authenticate', 'Proxy-Authenticate')}
        names.update({'v': 'Via', 'f': 'From', 't': 'To', 'i': 'Call-ID', 'm': 'Contact'})
        lines = []
        for line in head.split('\r\n'):
            if not lines:
                lines.append(line)
            elif line[:1] in (' ', '\t'):
                lines[-1] += ' ' + line.strip()
            elif ':' in line:
                name, value = line.split(':', 1)
                lines.append(names.get(name.lower(), name) + ': ' + value.strip())
            else:
                lines.append(line)
        text = '\r\n'.join(lines) + separator + body
        cid_value = sip_header(text, 'Call-ID')
        csq_value = sip_header(text, 'CSeq')
        if not cid_value or not re.fullmatch(r'\d+\s+\w+', csq_value):
            return
        if text.startswith("SIP/2.0"):
            status = re.match(r'^SIP/2\.0 ([1-6]\d{2})(?: |\r\n)', text)
            if not status:
                return
            if int(status.group(1)) >= 200 and csq_value.split()[1].upper() == 'INVITE':
                key = (cid_value, csq_value.split()[0], sip_header(text, 'To'))
                cached = self.invite_acks.get(key)
                if cached and time.monotonic() < cached[1]:
                    self.sip_sock.sendto(cached[0], addr)
                    return
            csq = re.search(r'^CSeq:\s*(\d+)\s+REGISTER\s*$', text, re.M | re.I)
            cid = re.search(r'^Call-ID:\s*([^\r\n]+)', text, re.M | re.I)
            if csq and cid and cid.group(1) == self.reg_call_id and int(csq.group(1)) == self.reg_cseq:
                code = text.split(" ", 2)[1]
                m = re.search(r'(?:WWW|Proxy)-Authenticate: (.+)', text)
                if m:
                    self.reg_nonce = m.group(1)
                    self.reg_event.set()
                elif code == "200":
                    self.reg_nonce = None
                    self.reg_ok = True
                    self.reg_event.set()
                elif code[0] in "3456":
                    self.log("sip: register failed", text.split("\r\n")[0])
                    self.reg_nonce = None
                    self.reg_event.set()
            call = self.invite_transactions.get(cid_value) or self.call
            if call and f"Call-ID: {call.call_id}\r\n" in text:
                call.responses.put((text, addr))
            return
        first = text.split("\r\n", 1)[0]
        parts = first.split()
        if len(parts) != 3 or parts[2] != 'SIP/2.0':
            return
        method = parts[0]
        if method != csq_value.split()[1] or any(not sip_header(text, name) for name in ('Via', 'From', 'To')):
            return
        cached = self.final_responses.get(transaction_key(text))
        if cached and time.monotonic() < cached[1]:
            self.sip_sock.sendto(cached[0], addr)
            return
        if method == 'ACK':
            call = self.call
            if (call and call.incoming and call.call_id == cid_value
                    and csq_value.split()[0] == call.invite_cseq
                    and ';tag=' + call.from_tag in sip_header(text, 'To')):
                call.ack_received = True
        elif method == "OPTIONS":
            self.send_response(text, addr, 200, "OK")
        elif method == "INVITE":
            self.on_invite(text, addr)
        elif method == "CANCEL":
            call = self.call
            if (call and call.incoming and call.call_id == cid_value
                    and csq_value.split()[0] == call.invite_cseq
                    and sip_header(text, 'Via') == sip_header(call.invite_text, 'Via')):
                self.send_response(text, addr, 200, "OK")
                if not call.established:
                    self.send_response(call.invite_text, call.remote, 487, 'Request Terminated')
                    self.stop_ringing()
                    call.close()
                    self.call = None
            else:
                self.send_response(text, addr, 481, 'Call Does Not Exist')
        elif method == "BYE":
            if not self.call or f"Call-ID: {self.call.call_id}\r\n" not in text:
                self.send_response(text, addr, 481, 'Call Does Not Exist')
                return
            self.send_response(text, addr, 200, "OK")
            self.log("sip: remote BYE")
            self.stop_ringing()
            try:
                for fr in HANGUP:
                    self.write(fr)
            except usb.core.USBError:
                pass
            self.media_stop()
            if self.call is not None:
                self.call.close()
            self.call = None

    def sip_tick(self, now):
        for cache in (self.final_responses, self.invite_acks):
            for key, (_, expires) in list(cache.items()):
                if now >= expires:
                    cache.pop(key, None)
        call = self.call
        if not call or not call.incoming or not call.final_due or call.ack_received:
            return
        if now >= call.final_until:
            self.log('sip: incoming answer was not ACKed')
            call.close()
            self.call = None
            for frame in HANGUP:
                self.write(frame)
        elif now >= call.final_due:
            self.sip_sock.sendto(call.last_response, call.remote)
            call.final_interval = min(4, call.final_interval * 2)
            call.final_due = now + call.final_interval

    def media_stop(self):
        if self.call is not None:
            self.call.media = False

    def on_invite(self, text, addr):
        cid = (re.search(r"Call-ID: ([^\r\n]+)", text) or [None, ""])[1]
        if self.call:
            if (self.call.call_id == cid and self.call.incoming
                    and sip_header(text, 'CSeq').split()[0] == self.call.invite_cseq
                    and self.call.last_response):
                self.sip_sock.sendto(self.call.last_response, addr)
            else:
                self.send_response(text, addr, 486, "Busy Here")
            return
        if self.ports[1]['hook']:
            self.send_response(text, addr, 486, 'Busy Here')
            return
        ip, port = parse_sdp(text)
        if ip is None or port is None:
            self.send_response(text, addr, 488, 'Not Acceptable Here')
            return
        self.log("sip: incoming INVITE")
        self.send_response(text, addr, 100, "Trying")
        call = SIPCall(self, "incoming")
        call.incoming = True
        call.invite_cseq = sip_header(text, 'CSeq').split()[0]
        call.remote_ip, call.remote_rtp = ip, port
        call.remote = addr
        call.invite_text = text
        call.remote_tag = (re.search(r"From: .*?tag=([^;\s]+)", text) or [None, ""])[1]
        call.call_id = cid
        call.remote_to = (re.search(r"From: ([^\r\n]+)", text) or [None, ''])[1]
        call.local_from = (re.search(r"To: ([^\r\n]+)", text) or [None, ''])[1]
        if 'tag=' not in call.local_from:
            call.local_from += ';tag=' + call.from_tag
        contact = re.search(r'Contact: *<([^>]+)>', text)
        call.target_uri = contact.group(1) if contact else text.split(' ', 2)[1]
        call.sock = self.sip_sock
        self.start_ringing(call)
        self.send_response(text, addr, 180, 'Ringing')

    def send_response(self, req, addr, code, reason, body=""):
        cfg = self.cfg
        to = re.search(r"To: ([^\r\n]+)", req)
        vias = re.findall(r'^Via: ([^\r\n]+)', req, re.M)
        frm = re.search(r"From: ([^\r\n]+)", req)
        cid = re.search(r"Call-ID: ([^\r\n]+)", req)
        csq = re.search(r"CSeq: ([^\r\n]+)", req)
        contact = re.search(r"Contact: ([^\r\n]+)", req)
        to_hdr = to.group(1) if to else ""
        if "tag=" not in to_hdr and code != 100:
            call = self.call
            tag = call.from_tag if call and cid and cid.group(1) == call.call_id else uuid.uuid4().hex[:8]
            to_hdr += ";tag=" + tag
        lines = [f"SIP/2.0 {code} {reason}",
                 *(f'Via: {via}' for via in vias),
                 f"From: {frm.group(1) if frm else ''}",
                 f"To: {to_hdr}",
                 f"Call-ID: {cid.group(1) if cid else ''}",
                 f"CSeq: {csq.group(1) if csq else ''}"]
        if body:
            # 200 OK to INVITE must carry OUR contact, not the caller's
            lines.append(f"Contact: <sip:{cfg['login']}@{cfg['local_ip']}:{self.sip_local_port}>")
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        msg = "\r\n".join(lines) + "\r\n\r\n" + body
        encoded = msg.encode()
        if self.call and cid and self.call.call_id == cid.group(1) and csq and csq.group(1).endswith(' INVITE'):
            self.call.last_response = encoded
            if code == 200 and self.call.incoming:
                self.call.final_interval = 0.5
                self.call.final_due = time.monotonic() + 0.5
                self.call.final_until = time.monotonic() + 32
        if code >= 200:
            self.final_responses[transaction_key(req)] = (encoded, time.monotonic() + 32)
        try:
            self.sip_sock.sendto(encoded, addr)
        except OSError:
            pass

    def run(self):
        for attempt in range(5):
            if not self.connect():
                self.log("usb: waiting for dongle")
                time.sleep(3)
                continue
            if self.usb_init():
                self.usb_active = True
                t = threading.Thread(target=self.usb_loop, daemon=True)
                t.start()
                return True
            self.log("usb: init failed; cold reboot (attempt %d)" % (attempt + 1))
            try:
                self.dev.reset()
            except Exception:
                pass
            self.dev = None
            time.sleep(2)
        return False


# ---------------------------------------------------------------- SIP
class SIPCall:
    def __init__(self, ata, number):
        self.ata = ata
        self.number = number
        self.sock = None
        self.remote = None
        self.local_rtp = random.randint(20000, 30000)
        self.remote_rtp = None
        self.remote_ip = None
        self.from_tag = uuid.uuid4().hex[:8]
        self.call_id = uuid.uuid4().hex
        self.cseq = 1
        self.media = False
        self.media_enable_due = 0
        self.thread = None
        self.responses = queue.Queue()
        self.cancelled = False
        self.closed = False
        self.established = False
        self.incoming = False
        self.invite_cseq = None
        self.ack_received = False
        self.final_due = 0
        self.final_until = 0
        self.final_interval = 0.5
        self.remote_to = None
        self.local_from = None
        self.target_uri = None
        self.last_request = None
        self.rtp = None
        self.last_response = None
        self.branch = 'z9hG4bK' + uuid.uuid4().hex[:10]

    # ---- helpers
    def cfg(self, k):
        return self.ata.cfg[k]

    def send(self, data, addr):
        self.sock.sendto(data.encode() if isinstance(data, str) else data, addr)

    def invite(self):
        cfg = self.ata.cfg
        self.sock = self.ata.sip_sock
        pbx = (cfg["pbx_ip"], cfg["pbx_port"])
        self.remote = pbx
        self.prepare_media()
        uri = f"sip:{self.number}@{cfg['domain']}"
        body = sdp_local(cfg["local_ip"], self.local_rtp)
        hdrs = (
            f"INVITE {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {cfg['local_ip']}:{self.ata.sip_local_port};branch=z9hG4bK{uuid.uuid4().hex[:10]};rport\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <sip:{cfg['login']}@{cfg['domain']}>;tag={self.from_tag}\r\n"
            f"To: <{uri}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} INVITE\r\n"
            f"Contact: <sip:{cfg['login']}@{cfg['local_ip']}:{self.ata.sip_local_port}>\r\n"
            "Subject: LineATA call\r\n"
            "User-Agent: LineATA\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}"
        )
        self.last_request = hdrs
        self.send(hdrs, pbx)
        return self.handle_responses()

    def handle_responses(self, timeout=45):
        cfg = self.ata.cfg
        t0 = time.time()
        auth = None
        retry_at = time.monotonic() + 0.5
        retry_interval = 0.5
        provisional = False
        while time.time() - t0 < timeout and not self.closed:
            try:
                text, addr = self.responses.get(timeout=0.1)
            except queue.Empty:
                if not provisional and not self.cancelled and time.monotonic() >= retry_at:
                    self.send(self.last_request, self.remote)
                    retry_interval = min(4, retry_interval * 2)
                    retry_at = time.monotonic() + retry_interval
                continue
            # CANCEL and INVITE share a Call-ID and numeric CSeq. Their
            # responses can race: 200 to CANCEL does not establish a dialog.
            cseq = re.search(r'^CSeq:\s*(\d+)\s+(\S+)', text, re.M | re.I)
            if not cseq or cseq.group(2).upper() != 'INVITE' or int(cseq.group(1)) != self.cseq:
                continue
            first = text.split("\r\n", 1)[0]
            code = first.split(" ")[1] if len(first.split(" ")) > 1 else "0"
            if code in ("401", "407"):
                self.ack_response(text, addr)
                if self.cancelled or auth is not None:
                    return False
                m = re.search(r'(?:WWW|Proxy)-Authenticate: (.+)', text)
                if m:
                    hdr = "Authorization" if code == "401" else "Proxy-Authorization"
                    auth = parse_digest(m.group(1), "INVITE",
                                        f"sip:{self.number}@{cfg['domain']}",
                                        cfg["login"], cfg["password"])
                    self.cseq += 1
                    body = sdp_local(cfg["local_ip"], self.local_rtp)
                    via = re.search(r"Via: (.+)", text)
                    withhdr = (
                        f"INVITE sip:{self.number}@{cfg['domain']} SIP/2.0\r\n"
                        f"Via: SIP/2.0/UDP {cfg['local_ip']}:{self.ata.sip_local_port};branch=z9hG4bK{uuid.uuid4().hex[:10]};rport\r\n"
                        "Max-Forwards: 70\r\n"
                        f"From: <sip:{cfg['login']}@{cfg['domain']}>;tag={self.from_tag}\r\n"
                        f"To: <sip:{self.number}@{cfg['domain']}>\r\n"
                        f"Call-ID: {self.call_id}\r\n"
                        f"CSeq: {self.cseq} INVITE\r\n"
                        f"Contact: <sip:{cfg['login']}@{cfg['local_ip']}:{self.ata.sip_local_port}>\r\n"
                        f"{hdr}: {auth}\r\n"
                        "User-Agent: LineATA\r\n"
                        "Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n{body}"
                    )
                    self.last_request = withhdr
                    self.send(withhdr, addr)
                    provisional = False
                    retry_at = time.monotonic() + 0.5
                continue
            if code in ("100", "180", "183"):
                provisional = True
                self.ata.log("sip:", first)
                # early media: use their SDP if present
                ip, port = parse_sdp(text)
                if ip and port:
                    self.remote_ip, self.remote_rtp = ip, port
                continue
            if code == "200":
                self.ata.log("sip: answered")
                ip, port = parse_sdp(text)
                if ip and port:
                    self.remote_ip, self.remote_rtp = ip, port
                self.remote_to = (re.search(r'To: ([^\r\n]+)', text) or [None, ''])[1]
                self.local_from = (re.search(r'From: ([^\r\n]+)', text) or [None, ''])[1]
                contact = re.search(r'Contact: *<([^>]+)>', text)
                self.target_uri = contact.group(1) if contact else f"sip:{self.number}@{cfg['domain']}"
                self.established = True
                self.ack_response(text, addr, dialog=True)
                if self.cancelled or self.ata.call is not self:
                    self.hangup()
                    return False
                if self.remote_ip is None or self.remote_rtp is None:
                    self.ata.log('sip: answer has no usable PCMU media endpoint')
                    self.hangup()
                    return False
                for fr in ANSWER:
                    try:
                        self.ata.write(fr, guard=lambda: self.ata.call is self and not self.closed)
                    except usb.core.USBError:
                        break
                self.start_media()
                return True
            if code.isdigit() and int(code) >= 300:
                self.ack_response(text, addr)
                self.ata.log("sip: failed", first)
                return False
        return False

    def ack_response(self, response, addr, dialog=False):
        ack = (self.dialog_request('ACK', self.cseq) if dialog
               else self.transaction_request('ACK', response))
        key = (self.call_id, str(self.cseq), sip_header(response, 'To'))
        self.ata.invite_acks[key] = (ack.encode(), time.monotonic() + 32)
        self.send(ack, addr)

    @staticmethod
    def _to_tag(text):
        m = re.search(r"To: ([^\r\n]+)", text)
        if m:
            t = re.search(r"tag=([^;\s]+)", m.group(1))
            if t:
                return f";tag={t.group(1)}"
        return ""

    def transaction_request(self, method, response=None):
        request = self.last_request
        uri = request.split(' ', 2)[1]
        fields = {}
        for key in ('Via', 'From', 'To', 'Call-ID', 'CSeq'):
            source = response if key == 'To' and response else request
            fields[key] = re.search(rf'^{key}: ([^\r\n]+)', source, re.M).group(1)
        cseq = fields['CSeq'].split()[0]
        fields['CSeq'] = f'{cseq} {method}'
        return (f'{method} {uri} SIP/2.0\r\n' +
                ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) +
                'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n')

    def dialog_request(self, method, cseq):
        cfg = self.ata.cfg
        return (
            f'{method} {self.target_uri} SIP/2.0\r\n'
            f"Via: SIP/2.0/UDP {cfg['local_ip']}:{self.ata.sip_local_port};branch=z9hG4bK{uuid.uuid4().hex[:10]};rport\r\n"
            f'From: {self.local_from}\r\nTo: {self.remote_to}\r\n'
            f'Call-ID: {self.call_id}\r\nCSeq: {cseq} {method}\r\n'
            'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
        )

    def prepare_media(self):
        """Reserve the advertised UDP port before emitting an SDP offer/answer."""
        if self.rtp is not None:
            return
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtp.bind((self.ata.cfg['local_ip'], 0))
            rtp.settimeout(0.005)
        except OSError:
            rtp.close()
            raise
        self.local_rtp = rtp.getsockname()[1]
        self.rtp = rtp

    def start_media(self):
        if self.media or self.remote_ip is None or self.closed:
            return
        self.prepare_media()
        self.established = True
        with self.ata.audio_lock:
            self.ata.in_audio.clear()
            self.ata.out_audio.clear()
        self.media = True
        self.media_enable_due = time.monotonic() + MEDIA_ENABLE_DELAY
        self.thread = threading.Thread(target=self.media_loop, daemon=True)
        self.thread.start()

    def media_loop(self):
        seq = random.getrandbits(16)
        ts = random.getrandbits(32)
        ssrc = random.getrandbits(32)
        addr = (self.remote_ip, self.remote_rtp)
        due = time.monotonic()
        try:
            while self.media and not self.ata.stop.is_set():
                now = time.monotonic()
                if now >= due:
                    due = advance_deadline(due, now)
                    pcm = self.ata.take_usb_audio(160)
                    if pcm is not None:
                        hdr = struct.pack("!BBHII", 0x80, PT_PCMU, seq, ts, ssrc)
                        self.rtp.sendto(hdr + pcm, addr)
                        seq = (seq + 1) & 0xFFFF
                    ts = (ts + 160) & 0xFFFFFFFF
                try:
                    self.rtp.settimeout(max(.001, min(.005, due - time.monotonic())))
                    pkt, source = self.rtp.recvfrom(2048)
                    payload = pcmu_payload(pkt)
                    if source == addr and payload:
                        self.ata.queue_rtp_audio(payload)
                except socket.timeout:
                    pass
        except OSError as exc:
            if self.media:
                self.ata.log('rtp: transport error', exc)
        finally:
            self.media = False

    def close(self):
        self.closed = True
        self.media = False
        if self.rtp is not None:
            self.rtp.close()
        if self.ata.call is self or self.ata.call is None:
            with self.ata.audio_lock:
                self.ata.in_audio.clear()
                self.ata.out_audio.clear()

    def hangup(self):
        self.cancelled = True
        self.media = False
        if self.sock and self.established:
            self.cseq += 1
            try:
                self.send(self.dialog_request('BYE', self.cseq), self.remote)
            except OSError:
                pass
            self.close()
        elif self.sock and self.last_request:
            try:
                self.send(self.transaction_request('CANCEL'), self.remote)
            except OSError:
                pass
            # Keep the INVITE worker alive for the 487 or a racing 200.
        else:
            self.close()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbx", default="10.9.12.136")
    ap.add_argument("--login", required=True)
    ap.add_argument("--password", default=os.environ.get('LINEATA_SIP_PASSWORD'),
                    help='SIP secret (defaults to LINEATA_SIP_PASSWORD environment variable)')
    ap.add_argument("--domain", default=None)
    ap.add_argument("--local-ip", default="10.9.12.200")
    ap.add_argument("--pbx-port", type=int, default=5060)
    ap.add_argument("--dial-timeout", type=float, default=3.0)
    ap.add_argument("--sip-port", type=int, default=5062)
    ap.add_argument("--no-cold", action="store_true")
    ap.add_argument("--debug-frames", action="store_true",
                    help="log raw USB IN frames (event-loss debugging)")
    args = ap.parse_args()
    if not args.password:
        ap.error('set LINEATA_SIP_PASSWORD or provide --password')
    cfg = dict(
        pbx_ip=args.pbx, login=args.login, password=args.password,
        domain=args.domain or args.pbx, local_ip=args.local_ip,
        pbx_port=args.pbx_port, dial_timeout=args.dial_timeout,
        sip_port=args.sip_port,
        cold=not args.no_cold,
    )
    ata = LinearATA(cfg)
    ata.debug_frames = args.debug_frames
    if not ata.run():
        sys.exit("usb init failed after retries")
    ata.start_sip()
    ata.log("ready: pick up the phone")
    try:
        while not ata.stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        ata.stop.set()
        if ata.call:
            ata.call.hangup()
        ata.sip_sock.close()


if __name__ == "__main__":
    main()
