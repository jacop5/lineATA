"""Incremental framing for the observed Linear service bulk protocol."""


class FrameDecoder:
    def __init__(self):
        self.pending = bytearray()

    def feed(self, data):
        self.pending.extend(data)
        frames = []
        while len(self.pending) >= 2:
            size = self.pending[1]
            if size < 2:
                # No sync marker exists: guessing a new boundary can turn voice
                # bytes into control events. Fail visibly instead.
                self.pending.clear()
                raise ValueError('invalid USB frame length')
            if len(self.pending) < size:
                break
            frames.append(bytes(self.pending[:size]))
            del self.pending[:size]
        return frames
