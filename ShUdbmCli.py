#!/usr/bin/env python3

from __future__ import annotations

import argparse
import queue
import re
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TOOL_VERSION = "1.0"
BAUDRATE = 115200
DEFAULT_TIMEOUT = 120.0
USB_READ_POLL_TIMEOUT_MS = 2000
USB_VID = 0x04DD
USB_PID = 0x91C9
USB_OBEX_CLASS = (0x02, 0x0B, 0x00)
RESTORE_CONFIRMATION = "ERASE-AND-RESTORE"
AT_BAUDRATE = 115200
AT_TERMINATORS = (
    "OK",
    "ERROR",
    "NO CARRIER",
    "BUSY",
    "NO DIALTONE",
    "NO ANSWER",
    "COMMAND NOT SUPPORT",
)
AT_BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)
SHUSRFLAG_COMMANDS = {
    "status": "AT*SHUSERFLAG?",
    "on": 'AT*SHUSERFLAG="USRFLAGON","DBMQRZPV"',
    "off": 'AT*SHUSERFLAG="USRFLAGOFF","DBMQRZPV"',
}

OP_CONNECT = 0x80
OP_DISCONNECT = 0x81
OP_PUT = 0x02
OP_PUT_FINAL = 0x82
OP_GET_FINAL = 0x83
RSP_CONTINUE = 0x90
RSP_SUCCESS = 0xA0
RSP_HISTORY_FINAL = 0xE1

HDR_NAME = 0x01
HDR_TYPE = 0x42
HDR_BODY = 0x48
HDR_END_BODY = 0x49
HDR_LENGTH = 0xC3
HDR_CONNECTION_ID = 0xCB

CONNECT_REQUEST = bytes.fromhex(
    "80 00 12 10 00 FF FF 46 00 0B 53 48 42 61 63 6B 55 50"
)

CORE_OBJECTS = ("SRAM", "NOR", "S-AND")
BLOCK_STREAM_OBJECTS = ("NOR", "S-AND")
AMR_OBJECTS = tuple(f"/AMR/REC{index:02d}.AMR" for index in range(1, 21))
RESTORE_ORDER = CORE_OBJECTS + ("HISTORY",) + AMR_OBJECTS
RESTORABLE_OBJECTS = RESTORE_ORDER

ERROR_CODES = {
    0xC0: "Bad Request",
    0xC1: "Unauthorized",
    0xC3: "Forbidden",
    0xC4: "Not Found",
    0xC6: "Not Acceptable",
    0xCC: "Precondition Failed",
    0xD0: "Internal Server Error",
    0xD3: "Service Unavailable",
}


class ToolError(RuntimeError):
    pass


class ProtocolError(ToolError):
    pass


class SerialPort:

    def __init__(self, port: str, baudrate: int = AT_BAUDRATE) -> None:
        self.port = port
        self.baudrate = baudrate
        self.connection: Any = None

    def open(self) -> None:
        if self.baudrate not in AT_BAUD_RATES:
            supported = ", ".join(str(value) for value in AT_BAUD_RATES)
            raise ToolError(f"Unsupported AT baud rate {self.baudrate}; choose one of: {supported}")
        try:
            import serial
        except ImportError as exc:
            raise ToolError("AT commands require pyserial; install it with: python -m pip install pyserial") from exc
        try:
            self.connection = serial.Serial(
                port=self.port, baudrate=self.baudrate, timeout=0.1,
                write_timeout=2.0, xonxoff=False, rtscts=False, dsrdtr=False,
            )
            self.flush()
        except Exception as exc:
            self.close()
            raise ToolError(f"Cannot open AT port {self.port}: {exc}") from exc

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def flush(self) -> None:
        if self.connection is not None:
            self.connection.reset_input_buffer()
            self.connection.reset_output_buffer()

    def write(self, data: bytes) -> int:
        if self.connection is None:
            raise ToolError("AT port is not open")
        return self.connection.write(data)

    def read(self, size: int = 1024, timeout: float = 0.1) -> bytes:
        if self.connection is None:
            raise ToolError("AT port is not open")
        self.connection.timeout = timeout
        return self.connection.read(size)


class ATDevice:
    def __init__(self, port: str, baudrate: int = AT_BAUDRATE) -> None:
        self.port = port
        self.serial = SerialPort(port, baudrate)
        self.running = False
        self.reader_thread: threading.Thread | None = None
        self.command_queue: queue.Queue[tuple[str, bool]] | None = None
        self.lock = threading.Lock()

    def connect(self) -> None:
        self.serial.open()
        self.running = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def disconnect(self) -> None:
        self.running = False
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1.0)
        self.serial.close()

    def _reader_loop(self) -> None:
        raw_buffer = b""
        while self.running and self.serial.connection is not None:
            try:
                chunk = self.serial.read(timeout=0.05)
            except Exception:
                break
            if not chunk:
                continue
            raw_buffer += chunk
            if raw_buffer.strip() == b">" or raw_buffer.endswith(b"> "):
                self._dispatch_line(raw_buffer.decode("latin1", errors="replace"), True)
                raw_buffer = b""
                continue
            while b"\n" in raw_buffer:
                line_bytes, raw_buffer = raw_buffer.split(b"\n", 1)
                self._dispatch_line(line_bytes.decode("latin1", errors="replace").strip("\r"))

    def _dispatch_line(self, line: str, is_prompt: bool = False) -> None:
        if self.command_queue is not None:
            self.command_queue.put((line, is_prompt))

    def send_command(self, command: str, timeout: float = 2.0) -> tuple[bool, list[str]]:
        with self.lock:
            self.serial.flush()
            self.command_queue = queue.Queue()
            clean_command = command.rstrip("\r\n")
            self.serial.write(clean_command.encode("latin1") + b"\r\n")
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            success = False
            try:
                while time.monotonic() < deadline:
                    remaining = max(0.01, deadline - time.monotonic())
                    try:
                        line, is_prompt = self.command_queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    lines.append(line)
                    clean = line.strip()
                    if is_prompt:
                        success = True
                        break
                    if clean in AT_TERMINATORS:
                        success = clean == "OK"
                        break
                    if clean.startswith(("+CME ERROR:", "+CMS ERROR:")):
                        break
                return success, lines
            finally:
                self.command_queue = None


@dataclass(frozen=True)
class Header:
    identifier: int
    value: bytes


@dataclass(frozen=True)
class Packet:
    opcode: int
    raw: bytes
    headers: tuple[Header, ...]
    version: int | None = None
    flags: int | None = None
    max_packet: int | None = None

    def values(self, identifier: int) -> list[bytes]:
        return [header.value for header in self.headers if header.identifier == identifier]


@dataclass(frozen=True)
class ObjectResult:
    name: str
    size: int
    expected_size: int | None
    missing: bool
    returned_name: str | None
    returned_type: str | None


class UsbBulkConnection:
    def __init__(
        self,
        device: Any,
        interfaces: tuple[int, ...],
        endpoint_in: Any,
        endpoint_out: Any,
        read_timeout_ms: int = USB_READ_POLL_TIMEOUT_MS,
    ) -> None:
        self.device = device
        self.interfaces = interfaces
        self.endpoint_in = endpoint_in
        self.endpoint_out = endpoint_out
        self.read_timeout_ms = read_timeout_ms
        self._rx_buffer = bytearray()

    def read(self, size: int) -> bytes:
        if self._rx_buffer:
            chunk = bytes(self._rx_buffer[:size])
            del self._rx_buffer[:size]
            return chunk
        try:
            packet = max(1, int(getattr(self.endpoint_in, "wMaxPacketSize", 1)))
            request_size = max(packet, ((max(1, size) + packet - 1) // packet) * packet)
            chunk = bytes(
                self.device.read(
                    self.endpoint_in.bEndpointAddress,
                    request_size,
                    timeout=self.read_timeout_ms,
                )
            )
            if len(chunk) > size:
                self._rx_buffer.extend(chunk[size:])
                return chunk[:size]
            return chunk
        except Exception as exc:
            message = str(exc).lower()
            if (
                exc.__class__.__name__ == "USBTimeoutError"
                or getattr(exc, "errno", None) in (60, 110)
                or getattr(exc, "backend_error_code", None) == -7
                or "timeout" in message
                or "timed out" in message
            ):
                return b""
            if getattr(exc, "errno", None) == 84 or getattr(exc, "backend_error_code", None) == -8:
                return b""
            raise

    def write(self, data: bytes) -> int:
        try:
            return int(self.device.write(self.endpoint_out.bEndpointAddress, data, timeout=5000))
        except TypeError:
            return int(self.endpoint_out.write(data, timeout=5000))

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def reset_output_buffer(self) -> None:
        return None

    def close(self) -> None:
        try:
            import usb.util
            for interface in reversed(self.interfaces):
                try:
                    usb.util.release_interface(self.device, interface)
                except Exception:
                    pass
        finally:
            try:
                import usb.util
                usb.util.dispose_resources(self.device)
            except Exception:
                pass


def _cdc_union_slaves(interface: Any) -> tuple[int, ...]:
    data = bytes(getattr(interface, "extra_descriptors", b""))
    slaves = []
    offset = 0
    while offset + 3 <= len(data):
        length = data[offset]
        if length < 3 or offset + length > len(data):
            break
        if data[offset + 1] == 0x24 and data[offset + 2] == 0x06 and length >= 5:
            slaves.extend(data[offset + 4 : offset + length])
        offset += length
    return tuple(slaves)


def open_usb_bulk(vid: int = USB_VID, pid: int = USB_PID) -> UsbBulkConnection:
    try:
        import usb.core
        import usb.util
    except ModuleNotFoundError as exc:
        raise ToolError("pyusb is required") from exc
    try:
        device = usb.core.find(idVendor=vid, idProduct=pid)
    except usb.core.NoBackendError as exc:
        raise ToolError(
            "libusb backend is unavailable"
        ) from exc
    if device is None:
        raise ToolError(f"USB device {vid:04X}:{pid:04X} was not found")
    try:
        configuration = device.get_active_configuration()
    except usb.core.USBError:
        try:
            device.set_configuration()
            configuration = device.get_active_configuration()
        except usb.core.USBError as exc:
            raise ToolError(f"Cannot configure USB device {vid:04X}:{pid:04X}: {exc}") from exc
    interfaces = list(configuration)
    by_number = {item.bInterfaceNumber: item for item in interfaces}
    for control in interfaces:
        if (control.bInterfaceClass, control.bInterfaceSubClass, control.bInterfaceProtocol) != USB_OBEX_CLASS:
            continue
        candidates = [control]
        candidates.extend(by_number[number] for number in _cdc_union_slaves(control) if number in by_number)
        candidates.extend(item for item in interfaces if item.bInterfaceClass == 0x0A and item.bInterfaceNumber == control.bInterfaceNumber + 1)
        for data_interface in candidates:
            try:
                device.set_interface_altsetting(data_interface.bInterfaceNumber, data_interface.bAlternateSetting)
            except usb.core.USBError:
                pass
            bulk_in = next((ep for ep in data_interface if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK and usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN), None)
            bulk_out = next((ep for ep in data_interface if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK and usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT), None)
            if bulk_in is None or bulk_out is None:
                continue
            claimed = tuple(dict.fromkeys((control.bInterfaceNumber, data_interface.bInterfaceNumber)))
            try:
                for number in claimed:
                    usb.util.claim_interface(device, number)
            except usb.core.USBError as exc:
                raise ToolError(f"Cannot claim OBEX USB interface {number}: {exc}") from exc
            # Match the official driver's 115200 8N1 open settings. Some WMC
            # firmware accepts CDC ACM requests here; rejection is harmless.
            try:
                device.ctrl_transfer(0x21, 0x20, 0, control.bInterfaceNumber, struct.pack("<IBBB", BAUDRATE, 0, 0, 8), timeout=1000)
                device.ctrl_transfer(0x21, 0x22, 0, control.bInterfaceNumber, None, timeout=1000)
            except usb.core.USBError:
                pass
            # MCCI resets/clears pipes as part of its serial-port open path.
            for endpoint in (bulk_in, bulk_out):
                try:
                    device.ctrl_transfer(0x02, 0x01, 0, endpoint.bEndpointAddress, None, timeout=1000)
                except usb.core.USBError:
                    pass
            print(f"Using libusb OBEX interface {control.bInterfaceNumber}, data interface {data_interface.bInterfaceNumber}: IN 0x{bulk_in.bEndpointAddress:02X}, OUT 0x{bulk_out.bEndpointAddress:02X}")
            return UsbBulkConnection(device, claimed, bulk_in, bulk_out)
    usb.util.dispose_resources(device)
    raise ToolError(f"USB device {vid:04X}:{pid:04X} has no bulk OBEX interface")


def be16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ProtocolError(f"16-bit value out of range: {value}")
    return struct.pack(">H", value)


def be32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ProtocolError(f"32-bit value out of range: {value}")
    return struct.pack(">I", value)


def make_packet(opcode: int, headers: Iterable[bytes] = ()) -> bytes:
    body = b"".join(headers)
    length = len(body) + 3
    if length > 0xFFFF:
        raise ProtocolError(f"OBEX packet is too large: {length}")
    return bytes((opcode,)) + be16(length) + body


def unicode_name_header(name: str) -> bytes:
    payload = name.encode("utf-16-be") + b"\x00\x00"
    return bytes((HDR_NAME,)) + be16(len(payload) + 3) + payload


def connection_header(connection_id: bytes | None) -> bytes:
    if connection_id is None:
        return b""
    if len(connection_id) != 4:
        raise ProtocolError("Connection-ID must contain exactly four bytes")
    return bytes((HDR_CONNECTION_ID,)) + connection_id


def length_header(length: int) -> bytes:
    return bytes((HDR_LENGTH,)) + be32(length)


def body_header(data: bytes, final: bool) -> bytes:
    identifier = HDR_END_BODY if final else HDR_BODY
    return bytes((identifier,)) + be16(len(data) + 3) + data


def decode_name(payload: bytes) -> str:
    if len(payload) % 2:
        raise ProtocolError("Odd-length UTF-16BE Name header")
    return payload.decode("utf-16-be", errors="strict").rstrip("\x00")


def parse_headers(raw: bytes, start: int) -> tuple[Header, ...]:
    headers = []
    offset = start
    while offset < len(raw):
        identifier = raw[offset]
        kind = identifier & 0xC0
        if kind in (0x00, 0x40):
            if offset + 3 > len(raw):
                raise ProtocolError("Truncated variable-length OBEX header")
            length = int.from_bytes(raw[offset + 1 : offset + 3], "big")
            if length < 3 or offset + length > len(raw):
                raise ProtocolError(f"Invalid OBEX header length {length} at offset {offset}")
            value = raw[offset + 3 : offset + length]
            offset += length
        elif kind == 0x80:
            if offset + 2 > len(raw):
                raise ProtocolError("Truncated one-byte OBEX header")
            value = raw[offset + 1 : offset + 2]
            offset += 2
        else:
            if offset + 5 > len(raw):
                raise ProtocolError("Truncated four-byte OBEX header")
            value = raw[offset + 1 : offset + 5]
            offset += 5
        headers.append(Header(identifier, value))
    return tuple(headers)


def parse_packet(raw: bytes, connect_response: bool = False) -> Packet:
    if len(raw) < 3:
        raise ProtocolError("OBEX packet is shorter than three bytes")
    declared = int.from_bytes(raw[1:3], "big")
    if declared != len(raw):
        raise ProtocolError(f"OBEX length mismatch: header={declared}, received={len(raw)}")

    if connect_response:
        if len(raw) < 7:
            raise ProtocolError("CONNECT response is shorter than seven bytes")
        return Packet(
            opcode=raw[0],
            raw=raw,
            headers=parse_headers(raw, 7),
            version=raw[3],
            flags=raw[4],
            max_packet=int.from_bytes(raw[5:7], "big"),
        )
    return Packet(raw[0], raw, parse_headers(raw, 3))


def describe_status(opcode: int) -> str:
    return ERROR_CODES.get(opcode, f"OBEX response 0x{opcode:02X}")


class ObexTransport:
    def __init__(self, connection: Any, timeout: float, verbose: bool) -> None:
        self.connection = connection
        self.timeout = timeout
        self.verbose = verbose

    def _show(self, prefix: str, data: bytes) -> None:
        if not self.verbose:
            return
        preview = data[:96].hex(" ").upper()
        suffix = " ..." if len(data) > 96 else ""
        print(f"{prefix} {len(data)} bytes: {preview}{suffix}")

    def write_packet(self, packet: bytes) -> None:
        self._show(">>>", packet)
        try:
            written = self.connection.write(packet)
            self.connection.flush()
        except Exception as exc:
            details = ""
            if hasattr(exc, "errno"):
                details = f" (errno={getattr(exc, 'errno', None)})"
            if hasattr(exc, "backend_error_code"):
                details += f" (libusb={getattr(exc, 'backend_error_code', None)})"
            raise ToolError(f"USB write failed{details}: {exc}") from exc
        if written != len(packet):
            raise ToolError(f"Short serial write: {written}/{len(packet)} bytes")

    def read_exact(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        received = bytearray()
        while len(received) < size and time.monotonic() < deadline:
            try:
                chunk = self.connection.read(size - len(received))
            except Exception as exc:
                raise ToolError(f"USB read failed: {exc}") from exc
            if chunk:
                received.extend(chunk)
        if len(received) != size:
            raise ToolError(f"USB timeout: received {len(received)}/{size} bytes")
        return bytes(received)

    def read_packet(self, connect_response: bool = False) -> Packet:
        prefix = self.read_exact(3)
        length = int.from_bytes(prefix[1:3], "big")
        if length < 3 or length > 0xFFFF:
            raise ProtocolError(f"Invalid OBEX packet length: {length}")
        raw = prefix + self.read_exact(length - 3)
        self._show("<<<", raw)
        if raw[0] == 0x00:
            raise ProtocolError(
                "Phone returned an non-OBEX response on this port. "
            )
        return parse_packet(raw, connect_response)

    def exchange(self, packet: bytes, connect_response: bool = False) -> Packet:
        self.write_packet(packet)
        return self.read_packet(connect_response)


class Progress:
    def __init__(self, name: str, total: int | None) -> None:
        try:
            from tqdm import tqdm
        except ModuleNotFoundError as exc:
            raise ToolError(
                "tqdm is required"
            ) from exc
        self._bar = tqdm(
            total=total,
            desc=name,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            mininterval=0.1,
            disable=None,
        )
        self._closed = False

    @property
    def total(self) -> int | None:
        return self._bar.total

    @total.setter
    def total(self, value: int | None) -> None:
        if self._bar.total != value:
            self._bar.total = value
            self._bar.refresh()

    def update(self, current: int, total: int | None = None) -> None:
        if total is not None:
            self.total = total
        delta = current - self._bar.n
        if delta:
            self._bar.update(delta)

    def finish(self, current: int) -> None:
        if self._closed:
            return
        self.update(current)
        self._bar.close()
        self._closed = True


class SilentProgress:
    def __init__(self, total: int | None) -> None:
        self.total = total

    def update(self, current: int, total: int | None = None) -> None:
        if total is not None:
            self.total = total

    def finish(self, current: int) -> None:
        return None


def tqdm_write(message: str) -> None:
    try:
        from tqdm import tqdm
    except ModuleNotFoundError:
        print(message, file=sys.stderr)
    else:
        tqdm.write(message, file=sys.stderr)


class SHBackUPClient:
    def __init__(self, transport: ObexTransport) -> None:
        self.transport = transport
        self.connection_id: bytes | None = None
        self.max_packet = 0xFFFF
        self.connected = False

    def connect(self) -> None:
        response = self.transport.exchange(CONNECT_REQUEST, connect_response=True)
        if response.opcode != RSP_SUCCESS:
            raise ProtocolError(f"CONNECT failed: {describe_status(response.opcode)}")
        if response.max_packet is None or response.max_packet < 64:
            raise ProtocolError(f"Phone returned an invalid maximum packet size: {response.max_packet}")
        self.max_packet = min(response.max_packet, 0xFFFF)
        ids = response.values(HDR_CONNECTION_ID)
        self.connection_id = ids[-1] if ids else None
        self.connected = True

    def disconnect(self) -> None:
        if not self.connected:
            return
        packet = make_packet(OP_DISCONNECT, (connection_header(self.connection_id),))
        try:
            response = self.transport.exchange(packet)
            if response.opcode != RSP_SUCCESS:
                raise ProtocolError(f"DISCONNECT failed: {describe_status(response.opcode)}")
        finally:
            self.connected = False

    def _update_connection_id(self, packet: Packet) -> None:
        ids = packet.values(HDR_CONNECTION_ID)
        if ids:
            self.connection_id = ids[-1]

    def get_object(
        self,
        name: str,
        destination: Path,
        *,
        show_progress: bool = True,
    ) -> ObjectResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        expected_size = None
        returned_name = None
        returned_type = None
        received = 0
        first = True
        progress = Progress(name, None) if show_progress else SilentProgress(None)

        try:
            with partial.open("wb") as stream:
                while True:
                    headers = [connection_header(self.connection_id)]
                    if first:
                        headers.append(unicode_name_header(name))
                    request = make_packet(OP_GET_FINAL, headers)
                    response = self.transport.exchange(request)
                    self._update_connection_id(response)

                    allowed_final = (RSP_SUCCESS,)
                    if name == "HISTORY":
                        allowed_final += (RSP_HISTORY_FINAL,)
                    if response.opcode not in (RSP_CONTINUE,) + allowed_final:
                        raise ProtocolError(f"GET {name} failed: {describe_status(response.opcode)}")

                    names = response.values(HDR_NAME)
                    if names:
                        current_name = decode_name(names[-1])
                        if returned_name is None:
                            returned_name = current_name
                        if name != "IMEI" and current_name != name:
                            raise ProtocolError(f"GET {name} returned object name {current_name!r}")

                    types = response.values(HDR_TYPE)
                    if types:
                        try:
                            current_type = types[-1].rstrip(b"\x00").decode("ascii")
                        except UnicodeDecodeError as exc:
                            raise ProtocolError(f"GET {name} returned a non-ASCII Type header") from exc
                        if returned_type is None:
                            returned_type = current_type
                        elif returned_type != current_type:
                            raise ProtocolError(
                                f"GET {name} changed Type from {returned_type!r} to {current_type!r}"
                            )

                    lengths = response.values(HDR_LENGTH)
                    if lengths:
                        if len(lengths[-1]) != 4:
                            raise ProtocolError(f"GET {name} returned a malformed Length header")
                        announced = int.from_bytes(lengths[-1], "big")
                        if expected_size is not None and expected_size != announced:
                            raise ProtocolError(
                                f"GET {name} changed total length from {expected_size} to {announced}"
                            )
                        expected_size = announced
                        progress.total = announced

                    for identifier in (HDR_BODY, HDR_END_BODY):
                        for body in response.values(identifier):
                            stream.write(body)
                            received += len(body)
                            progress.update(received)

                    first = False
                    if response.opcode in allowed_final:
                        break

            missing = received == 0 and expected_size in (None, 0) and name.startswith("/AMR/")
            if expected_size is not None and expected_size != received:
                if name in BLOCK_STREAM_OBJECTS and received > expected_size:
                    tqdm_write(
                        f"WARNING: GET {name} received {received:,} bytes, "
                        f"{received - expected_size:,} more than the declared payload "
                        f"length {expected_size:,}; preserving the complete block stream."
                    )
                else:
                    raise ProtocolError(
                        f"GET {name} length mismatch: announced {expected_size}, received {received}"
                    )
            progress.finish(received)
            if missing:
                partial.unlink(missing_ok=True)
            else:
                partial.replace(destination)
            return ObjectResult(
                name, received, expected_size, missing,
                returned_name, returned_type,
            )
        except Exception:
            progress.finish(received)
            raise

    def put_object(self, name: str, source: Path) -> None:
        total = source.stat().st_size
        sent = 0
        first = True
        progress = Progress(name, total)

        with source.open("rb") as stream:
            while first or sent < total:
                prefix = connection_header(self.connection_id)
                if first:
                    prefix += unicode_name_header(name) + length_header(total)
                capacity = self.max_packet - 3 - len(prefix) - 3
                if capacity < 1 and total != 0:
                    raise ProtocolError(
                        f"Negotiated packet size {self.max_packet} is too small for PUT {name}"
                    )

                chunk = stream.read(min(capacity, total - sent)) if total else b""
                final = sent + len(chunk) == total
                opcode = OP_PUT_FINAL if final else OP_PUT
                request = make_packet(opcode, (prefix, body_header(chunk, final)))
                response = self.transport.exchange(request)
                self._update_connection_id(response)

                expected_response = RSP_SUCCESS if final else RSP_CONTINUE
                if response.opcode != expected_response:
                    raise ProtocolError(
                        f"PUT {name} expected 0x{expected_response:02X}, "
                        f"received {describe_status(response.opcode)}"
                    )

                sent += len(chunk)
                progress.update(sent)
                first = False
                if final:
                    break
        progress.finish(sent)


def object_path(name: str) -> Path:
    if name.startswith("/AMR/"):
        return Path("AMR") / Path(name).name
    return Path(f"{name}.bin")


def object_file_label(name: str) -> str:
    if name.startswith("/AMR/"):
        return f"AMR_{Path(name).stem}"
    return name


def automatic_backup_path(name: str) -> Path:
    timestamp = datetime.now().astimezone()
    return Path(f"{object_file_label(name)}_{timestamp:%Y%m%d}_{timestamp:%H%M%S}.bin")


def infer_restore_object(path: Path) -> str | None:
    filename = path.name.casefold()
    stem = path.stem.casefold()
    for name in RESTORE_ORDER:
        expected = object_path(name)
        if filename == expected.name.casefold():
            return name
        aliases = {expected.stem.casefold(), object_file_label(name).casefold()}
        for alias in aliases:
            if any(stem.startswith(alias + separator) for separator in ("_", "-", ".")):
                return name
    return None


def imei_digits(raw: bytes) -> str:
    if len(raw) != 15 or any(value > 9 for value in raw):
        raise ProtocolError(f"Invalid raw IMEI payload, received {raw.hex(' ')}")
    return "".join(str(value) for value in raw)


def print_phone_identity(result: ObjectResult, source: Path) -> None:
    if result.returned_name:
        print(f"Model: {result.returned_name}")
    if result.returned_type:
        print(f"Software Version: {result.returned_type}")
    print(f"IMEI: {imei_digits(source.read_bytes())}")


def probe_phone_identity(client: SHBackUPClient) -> None:
    with tempfile.TemporaryDirectory(prefix="shbackup-imei-") as temporary:
        destination = Path(temporary) / "IMEI.bin"
        result = client.get_object("IMEI", destination, show_progress=False)
        print_phone_identity(result, destination)


def connected_client(args: argparse.Namespace) -> tuple[Any, SHBackUPClient]:
    connection = open_usb_bulk()
    transport = ObexTransport(connection, args.timeout, args.verbose)
    return connection, SHBackUPClient(transport)


def run_probe(args: argparse.Namespace) -> int:
    connection, client = connected_client(args)
    try:
        client.connect()
        connection_id = client.connection_id.hex(" ").upper() if client.connection_id else "none"
        print(f"Connected: max_packet={client.max_packet}, connection_id={connection_id}")
        probe_phone_identity(client)
        client.disconnect()
        return 0
    finally:
        if client.connected:
            try:
                client.disconnect()
            except Exception:
                pass
        connection.close()


def list_candidate_at_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise ToolError("AT commands require pyserial; install it with: python -m pip install pyserial") from exc
    ignored = {"/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.debug-console"}
    ports = [port for port in sorted(list_ports.comports(), key=lambda port: port.device) if port.device not in ignored]
    sharp = [port for port in ports if (port.vid, port.pid) == (USB_VID, USB_PID)]
    usb = [port for port in ports if port.vid is not None and port not in sharp]
    other = [port for port in ports if port not in sharp and port not in usb]
    return [port.device for port in sharp + usb + other]


def connected_at_device(args: argparse.Namespace) -> ATDevice:
    port = args.port
    if not port:
        candidates = list_candidate_at_ports()
        if not candidates:
            raise ToolError("No accessible AT serial port was found; specify one with --port")
        port = candidates[0]
        print(f"Using AT port: {port}")
    device = ATDevice(port, args.baud)
    device.connect()
    return device


def at_response_value(command: str, lines: list[str]) -> str | None:
    for line in lines:
        value = line.strip()
        if not value or value == command or value in AT_TERMINATORS:
            continue
        if value.startswith(("+CME ERROR:", "+CMS ERROR:")):
            continue
        return value
    return None


def clean_firmware_version(value: str) -> str:
    cleaned = re.sub(r"^(?i:ver\.?|v\.?)", "", value.strip()).strip()
    if not cleaned or cleaned in (".", "..") or any(character in cleaned for character in ("/", "\\", "\x00")):
        raise ToolError(f"Invalid firmware version filename: {cleaned!r}")
    return cleaned


def auth_sd_targets(output: Path | None, firmware_version: str) -> list[Path]:
    if output is not None:
        target = output.expanduser()
        return [target / firmware_version if target.is_dir() else target]
    return [Path(__file__).resolve().parent / firmware_version]


def run_makeauthsd(args: argparse.Namespace) -> int:
    device = connected_at_device(args)
    try:
        if args.version:
            firmware_version = clean_firmware_version(args.version)
        else:
            firmware_raw = None
            for command in ("AT+GMR", "AT+CGMR"):
                _, lines = device.send_command(command, timeout=args.timeout)
                firmware_raw = at_response_value(command, lines)
                if firmware_raw:
                    break
            if firmware_raw is None:
                raise ToolError("Cannot read the firmware version with AT+GMR or AT+CGMR")
            firmware_version = clean_firmware_version(firmware_raw)

        _, lines = device.send_command("AT+CGSN", timeout=args.timeout)
        imei_value = at_response_value("AT+CGSN", lines)
        digits = "" if imei_value is None else "".join(character for character in imei_value if character.isdigit())
        if len(digits) != 15:
            raise ToolError(f"Expected a 15-digit IMEI from AT+CGSN, received: {imei_value!r}")
        payload = bytes(int(character) for character in digits)

        written: list[Path] = []
        failures: list[str] = []
        for target in auth_sd_targets(args.output, firmware_version):
            try:
                target.write_bytes(payload)
                written.append(target)
                print(f"Auth file written: {target.resolve()}")
            except OSError as exc:
                failures.append(f"{target}: {exc}")

        if not written:
            raise ToolError("Cannot write an SD auth file: " + "; ".join(failures))
        for failure in failures:
            print(f"WARNING: {failure}", file=sys.stderr)
        print(f"Firmware Version: {firmware_version}")
        print(f"IMEI: {digits}")
        print(f"Payload: {payload.hex(' ').upper()}")
        print("Please copy Auth File into SD Card manually!")
        return 0
    finally:
        device.disconnect()


def run_shusrflag(args: argparse.Namespace) -> int:
    command = SHUSRFLAG_COMMANDS[args.state]
    device = connected_at_device(args)
    try:
        success, lines = device.send_command(command, timeout=args.timeout)
        response_lines = [line for line in lines if line.strip() and line.strip() != command]
        for line in response_lines:
            print(line)
        if not success:
            raise ToolError(f"SH User Flag {args.state} command failed or timed out")
        return 0
    finally:
        device.disconnect()


def run_backup(args: argparse.Namespace) -> int:
    destination = (args.output or automatic_backup_path(args.object)).resolve()
    if destination.exists() and destination.is_dir():
        raise ToolError(f"Output must be a file, not a directory: {destination}")
    if not destination.parent.is_dir():
        raise ToolError(f"Output directory was not found: {destination.parent}")

    connection, client = connected_client(args)
    try:
        client.connect()
        client.get_object(args.object, destination)
        client.disconnect()
        print(f"Backup Complete: {destination}")
        return 0
    finally:
        if client.connected:
            try:
                client.disconnect()
            except Exception:
                pass
        connection.close()


def run_restore(args: argparse.Namespace) -> int:
    if args.confirm != RESTORE_CONFIRMATION:
        raise ToolError(
            f"Restore is destructive. Re-run with --confirm {RESTORE_CONFIRMATION} when ready."
        )
    source = args.input.resolve()
    if not source.is_file():
        raise ToolError(f"Backup file was not found: {source}")
    name = args.object or infer_restore_object(source)
    if name is None:
        raise ToolError(
            f"Cannot infer the restore object from filename {source.name!r}; specify it with --object"
        )

    print("WARNING: restore can erase and rewrite handset RAM, NOR, and S-AND data.")
    print(f"Selected restore object: {name}")
    connection, client = connected_client(args)
    wrote_anything = False
    try:
        client.connect()
        wrote_anything = True
        client.put_object(name, source)

        client.disconnect()
        print("Restore transfer complete. DO NOT PRESS POWER BUTTON TO SHUTDOWN!")
        return 0
    except Exception as exc:
        if wrote_anything:
            raise ToolError(
                f"Restore failed after destructive transfer began; handset state may be partial: {exc}"
            ) from exc
        raise
    finally:
        if client.connected:
            try:
                client.disconnect()
            except Exception:
                pass
        connection.close()


def add_transport_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="per-packet timeout in seconds (default: 120)",
    )
    parser.add_argument("--verbose", action="store_true", help="print OBEX frame summaries")


def add_at_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="AT serial port; auto-detected when omitted")
    parser.add_argument("--baud", type=int, default=AT_BAUDRATE, help="AT baud rate (default: 115200)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="AT command timeout in seconds (default: 2)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up or restore a Sharp RTOS handset using the SHBackUP protocol."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    probe = subparsers.add_parser(
        "probe",
        help="connect, display protocol parameters and phone identity, then disconnect",
    )
    add_transport_options(probe)

    makeauthsd = subparsers.add_parser(
        "makeauthsd",
        help="create an SD authentication file from firmware version and IMEI",
    )
    add_at_options(makeauthsd)
    makeauthsd.add_argument(
        "--output",
        type=Path,
        help="output file or directory; default: the directory containing this tool",
    )
    makeauthsd.add_argument(
        "--version",
        help="firmware version filename override; otherwise query AT+GMR",
    )

    shusrflag = subparsers.add_parser(
        "shusrflag",
        help="query, enable, or disable the Sharp user flag over AT",
    )
    add_at_options(shusrflag)
    shusrflag.add_argument("state", choices=tuple(SHUSRFLAG_COMMANDS), help="requested flag operation")

    backup = subparsers.add_parser("backup", help="back up one phone object into a file")
    add_transport_options(backup)
    backup.add_argument(
        "--output",
        type=Path,
        nargs="?",
        required=True,
        help="output file; omit the value to generate OBJECT_YYYYMMDD_HHMMSS.bin",
    )
    backup.add_argument(
        "--object",
        required=True,
        choices=RESTORABLE_OBJECTS,
        metavar="NAME",
        help="object to back up",
    )

    restore = subparsers.add_parser("restore", help="destructively restore backup files")
    add_transport_options(restore)
    restore.add_argument(
        "--input",
        type=Path,
        required=True,
        help="backup file",
    )
    restore.add_argument(
        "--object",
        choices=RESTORABLE_OBJECTS,
        metavar="NAME",
        help="restore object; inferred from a standard filename when omitted",
    )
    restore.add_argument(
        "--confirm",
        metavar=RESTORE_CONFIRMATION,
        help="required exact confirmation phrase for destructive restore",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if hasattr(args, "timeout") and args.timeout <= 0:
        raise ToolError("--timeout must be greater than zero")
    if args.action == "probe":
        return run_probe(args)
    if args.action == "makeauthsd":
        return run_makeauthsd(args)
    if args.action == "shusrflag":
        return run_shusrflag(args)
    if args.action == "backup":
        return run_backup(args)
    if args.action == "restore":
        return run_restore(args)
    raise ToolError(f"Unsupported action: {args.action}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
