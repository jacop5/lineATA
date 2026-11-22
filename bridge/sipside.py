#!/usr/bin/env python3
"""SIP side of the bridge: FXS port <-> Asterisk (PJSIP) via pyVoIP.

State machine per physical FXS port:
  IDLE      on-offhook -> collect digits (startdial timer)
            -> INVITE sip:<digits>@pbx
  ACTIVE    on-onhook  -> BYE
            DTMF       -> INFO relay (if call up)
            incoming   -> cannot ring yet (ring experiment pending) so
                          port rings via ring.ring_incoming()/voice harness.

Transport note: USB side is in bridge/linearusb.py; audio is NOT bridged
yet (voice framing unknown) - calls establish with early media marked
inactive. When voice.py finds the framing, plug audio pump here.
"""
import threading
import queue
import time


class PortCallState:
    IDLE = "idle"
    DIALING = "dialing"
    ACTIVE = "active"
    RINGING = "ringing"


class PortFSM:
    def __init__(self, index, on_outgoing, hangup_digit="#", dial_timeout=5.0):
        self.index = index
        self.state = PortCallState.IDLE
        self.digits = ""
        self.call = None
        self.on_outgoing = on_outgoing     # (port, number) -> opaque call handle
        self.dial_timeout = dial_timeout
        self._t = None

    # -- FXS events (from the USB event loop) ------------------------------
    def on_hook(self, offhook: bool):
        if offhook:
            if self.state == PortCallState.IDLE:
                self.state = PortCallState.DIALING
                self.digits = ""
                self._arm_timer()
            return None
        # on-hook
        digits = self.digits
        self.digits = ""
        was = self.state
        self.state = PortCallState.IDLE
        if was == PortCallState.ACTIVE or was == PortCallState.DIALING:
            return ("hangup", None)
        return None

    def on_dtmf(self, digit: str):
        if self.state != PortCallState.DIALING:
            return None
        self.digits += digit
        self._arm_timer()
        return ("collecting", self.digits)

    def _arm_timer(self):
        if self._t:
            self._t.cancel()
        import threading as _th
        self._t = _th.Timer(self.dial_timeout, self._fire)
        self._t.daemon = True
        self._t.start()

    def _fire(self):
        if self.state == PortCallState.DIALING and self.digits:
            n = self.digits
            self.digits = ""
            self.state = PortCallState.ACTIVE
            self.on_outgoing(self.index, n)

    def outgoing_established(self, call):
        self.call = call
        self.state = PortCallState.ACTIVE

    def force_idle(self):
        was = self.state
        self.state = PortCallState.IDLE
        self.digits = ""
        return was


class SipAgent:
    """pyVoIP registration + call control. Constructed after USB is up."""

    def __init__(self, host="127.0.0.1", port=5060, user="101", password="lineata101"):
        from pyVoIP import SIPClient  # deferred import (heavy)
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.client = None

    def register(self, on_incoming, on_answer, on_hangup):
        from pyVoIP import SIPClient
        self.client = SIPClient(
            self.user, self.password, self.host, self.port,
            callCallback=on_incoming, myIP=self._my_ip())
        # pyVoIP 2.x API: register(...) blocks; give it a thread.
        reg = threading.Thread(target=self.client.start, daemon=True)
        reg.start()

    @staticmethod
    def _my_ip():
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("127.0.0.1", 9))
            return s.getsockname()[0]
        finally:
            s.close()

    def dial(self, number):
        if not self.client:
            raise RuntimeError("not registered")
        return self.client.call(f"sip:{number}@{self.host}")

    # incoming (from PBX) -> we must ring the FXS port; ring path pending.
