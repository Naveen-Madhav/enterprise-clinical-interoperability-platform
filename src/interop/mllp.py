"""Bounded MLLP transport: VT + ER7 + FS + CR. No transport-level encryption."""

import socket
import socketserver
import threading
from contextlib import contextmanager

from .hl7 import MAX_MESSAGE

START, END = b"\x0b", b"\x1c\x0d"


class FrameReader:
    def __init__(self, sock, limit=MAX_MESSAGE):
        self.sock, self.limit, self.buffer = sock, limit, b""

    def read(self) -> bytes | None:
        while True:
            if self.buffer and not self.buffer.startswith(START):
                raise ValueError("Invalid MLLP start block")
            position = self.buffer.find(END)
            if position >= 0:
                if position - 1 > self.limit:
                    raise ValueError("MLLP frame exceeds limit")
                message = self.buffer[1:position]
                self.buffer = self.buffer[position + len(END) :]
                if b"\x0b" in message or b"\x1c" in message:
                    raise ValueError("Invalid MLLP payload")
                return message
            if len(self.buffer) > self.limit + len(START) + len(END):
                raise ValueError("MLLP frame exceeds limit")
            chunk = self.sock.recv(4096)
            if not chunk:
                if self.buffer:
                    raise ValueError("Truncated MLLP frame")
                return None
            self.buffer += chunk


def send(host: str, port: int, raw: str, timeout=8.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(START + raw.encode("ascii") + END)
        response = FrameReader(sock).read()
        if response is None:
            raise ValueError("Connection closed without acknowledgment")
        return response.decode("ascii")


class Listener(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, bridge):
        self.bridge = bridge
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        reader = FrameReader(self.request)
        try:
            while (raw := reader.read()) is not None:
                ack = self.server.bridge.receive(raw.decode("ascii"))
                self.request.sendall(START + ack.encode("ascii") + END)
        except (ValueError, UnicodeError, OSError):
            # Invalid framing has no reliably correlatable message; close the connection.
            return


@contextmanager
def listening(bridge, host="127.0.0.1", port=0):
    with Listener((host, port), bridge) as listener:
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        try:
            yield listener
        finally:
            listener.shutdown()
            thread.join(timeout=10)
