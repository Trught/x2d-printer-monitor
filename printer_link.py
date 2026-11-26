"""Standard-library MQTT transport shared by the GUI and offline tests."""

from __future__ import annotations

import json
import os
import queue
import socket
import ssl
import threading
import time
from pathlib import Path

from bambu_monitor import (
    BambuMonitorError,
    create_ssl_context,
    decode_der_certificate,
    validate_serial,
)
from printer_queries import build_query


def cn_from_der(der: bytes) -> str | None:
    """Read the subject common name without guessing at DER byte offsets."""
    decoded, _ = decode_der_certificate(der)
    for group in decoded.get("subject", ()):
        for key, value in group:
            if key == "commonName":
                return value
    return None


def _str(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65535 or b"\x00" in encoded:
        raise ValueError("Invalid MQTT string")
    return len(encoded).to_bytes(2, "big") + encoded


def _packet(header: int, body: bytes) -> bytes:
    length, out = len(body), bytearray([header])
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out) + body


class AuthError(Exception):
    pass


class PrinterLink(threading.Thread):
    """MQTT 3.1.1 over TLS; passive mode forbids every application publish.

    Queue events are ``status``, ``serial``, ``msg``, ``auth`` or ``fatal``.
    Reconnects on network loss; credentials and certificate failures stop.
    """

    CONNACK = {
        1: "protocol refused",
        2: "client id refused",
        3: "server unavailable",
        4: "bad user name or access code",
        5: "not authorized (wrong access code?)",
    }
    MAX_PACKET = 8 * 1024 * 1024

    def __init__(
        self,
        host: str,
        code: str,
        serial: str,
        out: queue.Queue,
        *,
        passive: bool = True,
        cert_file: str | Path | None = None,
    ):
        super().__init__(daemon=True)
        self.host, self.code, self.serial, self.out = host, code, serial, out
        self.passive = passive
        self.cert_file = Path(cert_file) if cert_file else None
        self.stop_event = threading.Event()
        self.sock = None
        self.online = False
        self.outbox: queue.Queue = queue.Queue()

    def stop(self) -> None:
        self.stop_event.set()
        self.online = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def publish(self, msg: dict) -> bool:
        """Do not queue commands in passive mode or for a later connection."""
        if self.passive or not self.online or self.stop_event.is_set():
            return False
        self.outbox.put(msg)
        return True

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._session()
            except AuthError as exc:
                self.out.put(("auth", str(exc)))
                return
            except (ssl.SSLCertVerificationError, BambuMonitorError, ValueError) as exc:
                self.out.put(("fatal", str(exc)))
                return
            except (OSError, ConnectionError) as exc:
                self.online = False
                if self.stop_event.is_set():
                    break
                self.out.put(
                    ("status", ("error", f"Connection lost: {exc}. Retrying in 5 s"))
                )
                self.stop_event.wait(5)
            finally:
                self.online = False
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
        self.out.put(("status", ("off", "Disconnected")))

    def _recv_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("closed by printer")
            data.extend(chunk)
        return bytes(data)

    def _read(self, first_timeout: float):
        self.sock.settimeout(first_timeout)
        try:
            header = self._recv_exact(1)[0]
        except (socket.timeout, TimeoutError):
            return None
        self.sock.settimeout(15)
        length, multiplier = 0, 1
        for _ in range(4):
            byte = self._recv_exact(1)[0]
            length += (byte & 0x7F) * multiplier
            if length > self.MAX_PACKET:
                raise ConnectionError("MQTT packet exceeds 8 MiB")
            if not byte & 0x80:
                return header, self._recv_exact(length)
            multiplier *= 128
        raise ConnectionError("Invalid MQTT packet length")

    def _send(self, data: bytes) -> None:
        self.sock.settimeout(15)
        self.sock.sendall(data)

    def _pushall(self, topic: str) -> None:
        if self.passive:
            return
        msg = build_query("status")
        self._send(_packet(0x30, _str(topic) + json.dumps(msg).encode()))

    def _session(self) -> None:
        self.out.put(("status", ("busy", f"Connecting to {self.host}…")))
        context = create_ssl_context(self.cert_file)
        raw = socket.create_connection((self.host, 8883), timeout=10)
        try:
            self.sock = context.wrap_socket(
                raw, server_hostname=self.serial or self.host
            )
        except Exception:
            raw.close()
            raise
        serial = self.serial or cn_from_der(self.sock.getpeercert(binary_form=True))
        if not serial:
            raise BambuMonitorError(
                "Could not read serial from certificate; type it in the Serial field."
            )
        serial = validate_serial(serial)
        self.out.put(("serial", serial))

        body = _str("MQTT") + bytes([4, 0xC2]) + (60).to_bytes(2, "big")
        body += (
            _str(f"monitor-{os.getpid()}-{id(self):x}") + _str("bblp") + _str(self.code)
        )
        self._send(_packet(0x10, body))
        packet = self._read(10)
        if not packet or packet[0] >> 4 != 2 or len(packet[1]) != 2:
            raise ConnectionError("no valid answer to login")
        rc = packet[1][1]
        if rc:
            raise AuthError("Login refused: " + self.CONNACK.get(rc, f"code {rc}"))

        report, request = f"device/{serial}/report", f"device/{serial}/request"
        self._send(_packet(0x82, b"\x00\x01" + _str(report) + b"\x00"))
        # Wait for the subscription acknowledgement before publishing anything.
        subscribed = False
        self.out.put(("status", ("busy", "Subscribing to printer reports…")))
        while not self.outbox.empty():
            self.outbox.get_nowait()
        last_ping = last_push = last_received = time.monotonic()
        subscribe_deadline = last_received + 15
        while not self.stop_event.is_set():
            while not self.passive and subscribed:
                try:
                    msg = self.outbox.get_nowait()
                except queue.Empty:
                    break
                self._send(_packet(0x30, _str(request) + json.dumps(msg).encode()))
            packet = self._read(0.25)
            now = time.monotonic()
            if now - last_ping > 30:
                self._send(b"\xc0\x00")
                last_ping = now
            if subscribed and not self.passive and now - last_push > 300:
                self._pushall(request)
                last_push = now
            if not subscribed and now > subscribe_deadline:
                raise ConnectionError("no subscription acknowledgement")
            if now - last_received > 90:
                raise ConnectionError("printer stopped answering MQTT keepalive")
            if not packet:
                continue
            last_received = now
            header, data = packet
            if header >> 4 == 9:
                if len(data) != 3 or data[:2] != b"\x00\x01":
                    raise ConnectionError("invalid subscription acknowledgement")
                if data[2] == 0x80:
                    raise AuthError("Subscription refused (wrong serial number?)")
                if data[2] not in (0, 1, 2):
                    raise ConnectionError("invalid subscription result")
                if not subscribed:
                    subscribed = self.online = True
                    self._pushall(request)
                    mode = "passive MQTT" if self.passive else "status requests enabled"
                    tls = (
                        "certificate trusted"
                        if self.cert_file
                        else "TLS identity unverified"
                    )
                    self.out.put(("status", ("ok", f"Connected: {mode}; {tls}")))
                continue
            if header >> 4 != 3:
                continue
            if len(data) < 2:
                continue
            topic_length = int.from_bytes(data[:2], "big")
            pos = 2 + topic_length
            qos = (header >> 1) & 3
            if pos > len(data) or qos > 1 or (qos and pos + 2 > len(data)):
                continue
            if qos:
                self._send(b"\x40\x02" + data[pos : pos + 2])
                pos += 2
            if data[2 : 2 + topic_length] != report.encode("utf-8"):
                continue
            try:
                message = json.loads(data[pos:])
            except (ValueError, UnicodeError):
                continue
            if isinstance(message, dict) and isinstance(message.get("print", {}), dict):
                self.out.put(("msg", message))
