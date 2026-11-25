"""Serialized control transactions; voice packets bypass this ACK queue.

Only the USB owner calls pump/acknowledge. Other threads may submit commands.
There is no wire transaction ID, so at most one command can await a response.
"""
from dataclasses import dataclass
import queue
import time


@dataclass
class Command:
    data: bytes
    timeout: int
    guard: object = None


class ControlQueue:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.commands = queue.Queue(maxsize=256)
        self.pending = None
        self.deadline = 0

    def submit(self, data, timeout=2000, guard=None):
        if len(data) < 2 or data[1] != len(data) or data[0] == 0:
            raise ValueError('expected a complete control frame')
        if timeout <= 0:
            raise ValueError('command timeout must be positive')
        try:
            self.commands.put_nowait(Command(bytes(data), timeout, guard))
        except queue.Full as exc:
            raise RuntimeError('USB control queue overflow') from exc

    def pump(self, write):
        if self.pending:
            if self.clock() >= self.deadline:
                raise TimeoutError(f'USB response timeout for command {self.pending.data[0]:02x}')
            return
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            if command.guard is None or command.guard():
                break
        # Install pending before invoking the transport, including test fakes.
        self.pending = command
        self.deadline = self.clock() + command.timeout / 1000
        write(command.data, command.timeout)

    def acknowledge(self, frame):
        if self.pending and frame[0] == self.pending.data[0]:
            self.pending = None
            return True
        return False
