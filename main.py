#!/usr/bin/env python3
"""
Cross-platform NFC keyboard wedge for NTAG213 (Type 2 Tag) via PC/SC.

Flow:
1) Wait for NFC card tap on selected PC/SC reader.
2) Read UID.
3) Try to read and decode NDEF (Text or URI) from NTAG memory.
4) If NDEF decode fails, fallback to UID mapping.
5) Type output into currently focused app window.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import sys
import tempfile
import time
from typing import Callable, List, Optional, Sequence, Tuple

from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from smartcard.Exceptions import CardConnectionException, NoCardException
from smartcard.System import readers

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# ---------------------------
# User configuration
# ---------------------------
PRESS_ENTER_AFTER_SCAN = False
COOLDOWN_SECONDS = 8  # Prevent duplicate same-card typing from reader re-detect bounce.
READER_INDEX = 0  # Fixed to NFC reader index 0; no prompt when multiple readers exist.
ENABLE_NDEF_READ = True  # Decode NDEF from tag memory.
ENABLE_UID_FALLBACK = False  # If True, use CARD_MAP when NDEF text/URI is unavailable.
REMOVAL_CONFIRM_SECONDS = 1.0  # Ignore brief no-card glitches before treating as real removal.

# Fallback mapping when NDEF cannot be read/decoded.
CARD_MAP = {
    # "043046F2427081": "123456",
}

# Polling / read behavior.
POLL_INTERVAL_SECONDS = 0.20
MAX_PAGES_TO_READ = 45  # NTAG213 has pages 0..44; NDEF user area starts at page 4.


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def to_hex(data: Sequence[int]) -> str:
    return "".join(f"{b:02X}" for b in data)


def sanitize_uid(uid: str) -> str:
    return "".join(ch for ch in uid.upper() if ch in "0123456789ABCDEF")


def get_reader_list() -> List:
    try:
        return list(readers())
    except Exception as exc:
        log(f"Error listing readers: {exc}")
        return []


def choose_reader_name(initial_readers: List) -> str:
    if not initial_readers:
        raise RuntimeError("No PC/SC readers found.")

    log("Available readers:")
    for idx, reader in enumerate(initial_readers):
        log(f"  [{idx}] {reader}")

    if READER_INDEX is not None:
        if 0 <= READER_INDEX < len(initial_readers):
            selected = str(initial_readers[READER_INDEX])
            log(f"Selected reader from READER_INDEX={READER_INDEX}: {selected}")
            return selected
        raise RuntimeError(
            f"READER_INDEX={READER_INDEX} is out of range (0..{len(initial_readers)-1})."
        )

    if len(initial_readers) == 1:
        selected = str(initial_readers[0])
        log(f"Only one reader found. Auto-selected: {selected}")
        return selected

    while True:
        raw = input("Select reader index: ").strip()
        if not raw.isdigit():
            print("Please enter a numeric index.", flush=True)
            continue
        idx = int(raw)
        if 0 <= idx < len(initial_readers):
            selected = str(initial_readers[idx])
            log(f"Selected reader: {selected}")
            return selected
        print(f"Index out of range. Use 0..{len(initial_readers)-1}.", flush=True)


def resolve_reader_by_name(reader_name: str):
    for reader in get_reader_list():
        if str(reader) == reader_name:
            return reader
    return None


def transmit_apdu(connection, apdu: Sequence[int]) -> Tuple[List[int], int, int]:
    data, sw1, sw2 = connection.transmit(list(apdu))
    return list(data), int(sw1), int(sw2)


def apdu_ok(sw1: int, sw2: int) -> bool:
    return (sw1, sw2) == (0x90, 0x00)


def parse_raw_passthrough(payload: Sequence[int]) -> Optional[List[int]]:
    """
    Parse payload returned by passthrough style commands.
    Expected prefixes include:
    - PN53x InDataExchange response: D5 43 00 ...
    - PN53x InCommunicateThru response: D5 41 00 ...
    """
    if len(payload) >= 3 and payload[0] == 0xD5 and payload[2] == 0x00:
        return list(payload[3:])
    if len(payload) >= 2 and payload[0] == 0x00:
        return list(payload[1:])
    return list(payload) if payload else None


def read_page_4_bytes(connection, page: int) -> Optional[List[int]]:
    """
    Try several reader command variants and return exactly one page (4 bytes).
    """
    strategies: List[Tuple[str, Sequence[int], Callable[[Sequence[int]], Optional[List[int]]]]] = [
        (
            "PCSC_READ_BINARY_4B",
            [0xFF, 0xB0, 0x00, page & 0xFF, 0x04],
            lambda data: list(data[:4]) if len(data) >= 4 else None,
        ),
        (
            "PCSC_READ_BINARY_16B",
            [0xFF, 0xB0, 0x00, page & 0xFF, 0x10],
            lambda data: list(data[:4]) if len(data) >= 4 else None,
        ),
        (
            "IDENTIV_PASSTHRU_30",
            [0xFF, 0xEF, 0x00, 0x00, 0x02, 0x30, page & 0xFF],
            lambda data: (
                parsed[:4]
                if (parsed := parse_raw_passthrough(data)) is not None and len(parsed) >= 4
                else None
            ),
        ),
        (
            "PN532_INDATAEXCHANGE_30",
            [0xFF, 0x00, 0x00, 0x00, 0x04, 0xD4, 0x42, 0x30, page & 0xFF],
            lambda data: (
                parsed[:4]
                if (parsed := parse_raw_passthrough(data)) is not None and len(parsed) >= 4
                else None
            ),
        ),
        (
            "PN532_INCOMMUNICATETHRU_30",
            [0xFF, 0x00, 0x00, 0x00, 0x05, 0xD4, 0x40, 0x01, 0x30, page & 0xFF],
            lambda data: (
                parsed[:4]
                if (parsed := parse_raw_passthrough(data)) is not None and len(parsed) >= 4
                else None
            ),
        ),
    ]

    for _, apdu, parser in strategies:
        try:
            data, sw1, sw2 = transmit_apdu(connection, apdu)
            if not apdu_ok(sw1, sw2):
                continue
            parsed = parser(data)
            if parsed is not None and len(parsed) == 4:
                return parsed
        except Exception:
            continue

    return None


def get_uid(connection) -> Optional[str]:
    """
    Standard PC/SC Get Data command for UID.
    """
    apdu = [0xFF, 0xCA, 0x00, 0x00, 0x00]
    try:
        data, sw1, sw2 = transmit_apdu(connection, apdu)
    except Exception:
        return None
    if not apdu_ok(sw1, sw2) or not data:
        return None
    return to_hex(data)


def read_ntag_memory_from_page_4(connection) -> Optional[List[int]]:
    """
    Reads pages 4..MAX_PAGES_TO_READ-1 as raw bytes using reader-specific fallbacks.
    """
    all_bytes: List[int] = []
    any_success = False

    for page in range(4, MAX_PAGES_TO_READ):
        page_data = read_page_4_bytes(connection, page)
        if page_data is None:
            # Some readers may fail for pages after readable range. Stop after
            # we have at least some data.
            if any_success:
                break
            continue
        any_success = True
        all_bytes.extend(page_data)

    return all_bytes if any_success else None


def extract_ndef_message_from_tlv(raw: Sequence[int]) -> Optional[bytes]:
    """
    Parse NFC Type 2 Tag TLV area and return raw NDEF message bytes (TLV type 0x03).
    """
    i = 0
    total = len(raw)

    while i < total:
        t = raw[i]

        # NULL TLV
        if t == 0x00:
            i += 1
            continue

        # Terminator TLV
        if t == 0xFE:
            break

        if i + 1 >= total:
            return None

        length = raw[i + 1]
        value_start = i + 2

        if length == 0xFF:
            if i + 3 >= total:
                return None
            length = (raw[i + 2] << 8) | raw[i + 3]
            value_start = i + 4

        value_end = value_start + length
        if value_end > total:
            return None

        if t == 0x03:
            return bytes(raw[value_start:value_end])

        i = value_end

    return None


URI_PREFIX_MAP = [
    "",
    "http://www.",
    "https://www.",
    "http://",
    "https://",
    "tel:",
    "mailto:",
    "ftp://anonymous:anonymous@",
    "ftp://ftp.",
    "ftps://",
    "sftp://",
    "smb://",
    "nfs://",
    "ftp://",
    "dav://",
    "news:",
    "telnet://",
    "imap:",
    "rtsp://",
    "urn:",
    "pop:",
    "sip:",
    "sips:",
    "tftp:",
    "btspp://",
    "btl2cap://",
    "btgoep://",
    "tcpobex://",
    "irdaobex://",
    "file://",
    "urn:epc:id:",
    "urn:epc:tag:",
    "urn:epc:pat:",
    "urn:epc:raw:",
    "urn:epc:",
    "urn:nfc:",
]


def decode_single_ndef_record(record: bytes) -> Optional[str]:
    if len(record) < 3:
        return None

    header = record[0]
    sr = bool(header & 0x10)
    il = bool(header & 0x08)
    tnf = header & 0x07

    index = 1
    type_len = record[index]
    index += 1

    if sr:
        if index >= len(record):
            return None
        payload_len = record[index]
        index += 1
    else:
        if index + 3 >= len(record):
            return None
        payload_len = int.from_bytes(record[index : index + 4], byteorder="big")
        index += 4

    id_len = 0
    if il:
        if index >= len(record):
            return None
        id_len = record[index]
        index += 1

    type_end = index + type_len
    if type_end > len(record):
        return None
    type_field = record[index:type_end]
    index = type_end

    id_end = index + id_len
    if id_end > len(record):
        return None
    index = id_end

    payload_end = index + payload_len
    if payload_end > len(record):
        return None
    payload = record[index:payload_end]

    # TNF 0x01 = Well-known type
    if tnf != 0x01:
        return None

    # Text record (RTD_TEXT = "T")
    if type_field == b"T" and payload:
        status = payload[0]
        lang_len = status & 0x3F
        is_utf16 = bool(status & 0x80)
        text_bytes = payload[1 + lang_len :]
        if is_utf16:
            try:
                return text_bytes.decode("utf-16")
            except Exception:
                return None
        try:
            return text_bytes.decode("utf-8")
        except Exception:
            return None

    # URI record (RTD_URI = "U")
    if type_field == b"U" and payload:
        prefix_code = payload[0]
        remainder = payload[1:]
        prefix = URI_PREFIX_MAP[prefix_code] if prefix_code < len(URI_PREFIX_MAP) else ""
        try:
            return prefix + remainder.decode("utf-8")
        except Exception:
            return None

    return None


def decode_ndef_message(ndef_message: bytes) -> Optional[str]:
    """
    Parse an NDEF message and return the first decodable Text/URI record.
    """
    i = 0
    total = len(ndef_message)

    while i < total:
        if i + 2 > total:
            return None

        header = ndef_message[i]
        sr = bool(header & 0x10)
        il = bool(header & 0x08)

        # Determine full record length first
        index = i + 1
        if index >= total:
            return None
        type_len = ndef_message[index]
        index += 1

        if sr:
            if index >= total:
                return None
            payload_len = ndef_message[index]
            index += 1
        else:
            if index + 4 > total:
                return None
            payload_len = int.from_bytes(ndef_message[index : index + 4], byteorder="big")
            index += 4

        id_len = 0
        if il:
            if index >= total:
                return None
            id_len = ndef_message[index]
            index += 1

        record_len = (index - i) + type_len + id_len + payload_len
        if i + record_len > total:
            return None

        record = ndef_message[i : i + record_len]
        decoded = decode_single_ndef_record(record)
        if decoded:
            return decoded

        i += record_len

        # Stop when ME bit is set.
        if header & 0x40:
            break

    return None


def read_ndef_text_or_uri(connection) -> Optional[str]:
    raw = read_ntag_memory_from_page_4(connection)
    if not raw:
        return None

    ndef_message = extract_ndef_message_from_tlv(raw)
    if not ndef_message:
        return None

    return decode_ndef_message(ndef_message)


def type_output(text: str, keyboard: KeyboardController) -> None:
    keyboard.type(text)
    if PRESS_ENTER_AFTER_SCAN:
        keyboard.press(Key.enter)
        keyboard.release(Key.enter)


def is_no_card_exception(exc: Exception) -> bool:
    if isinstance(exc, NoCardException):
        return True
    message = str(exc).lower()
    return "no card" in message or "smart card removed" in message


def _macos_open_accessibility_settings() -> None:
    """
    Open the Accessibility settings page on macOS if possible.
    """
    try:
        subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ],
            check=False,
        )
    except Exception:
        pass


def _macos_accessibility_is_trusted(prompt: bool) -> Optional[bool]:
    """
    Check macOS Accessibility permission using AXIsProcessTrustedWithOptions.
    Returns:
      - True/False when check is available
      - None when API is unavailable
    """
    if sys.platform != "darwin":
        return True

    application_services = ctypes.util.find_library("ApplicationServices")
    core_foundation = ctypes.util.find_library("CoreFoundation")
    if not application_services or not core_foundation:
        return None

    try:
        app_services = ctypes.CDLL(application_services)
        cf = ctypes.CDLL(core_foundation)
    except Exception:
        return None

    if not hasattr(app_services, "AXIsProcessTrustedWithOptions"):
        return None

    app_services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    app_services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool

    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None

    try:
        k_cf_boolean_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        k_cf_boolean_false = ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse")
    except Exception:
        return None

    # kCFStringEncodingUTF8 = 0x08000100
    key_ref = cf.CFStringCreateWithCString(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
    if not key_ref:
        return None

    value_ref = k_cf_boolean_true if prompt else k_cf_boolean_false
    keys = (ctypes.c_void_p * 1)(key_ref)
    values = (ctypes.c_void_p * 1)(value_ref.value)
    options = cf.CFDictionaryCreate(None, keys, values, 1, None, None)

    try:
        if not options:
            return None
        return bool(app_services.AXIsProcessTrustedWithOptions(options))
    finally:
        if options:
            cf.CFRelease(options)
        cf.CFRelease(key_ref)


def ensure_macos_accessibility_permission() -> None:
    """
    On macOS, request Accessibility permission so keyboard typing can work.
    """
    if sys.platform != "darwin":
        return

    trusted = _macos_accessibility_is_trusted(prompt=True)
    if trusted is True:
        log("macOS Accessibility permission is already granted.")
        return

    log("macOS Accessibility permission is required for keyboard typing.")
    log("Grant access for Terminal (or your Python app) in:")
    log("System Settings -> Privacy & Security -> Accessibility")
    _macos_open_accessibility_settings()

    if trusted is False:
        log("Waiting up to 60s for permission to be granted...")
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(2)
            recheck = _macos_accessibility_is_trusted(prompt=False)
            if recheck:
                log("Accessibility permission granted.")
                return
        log("Permission still not granted. Typing may fail until permission is enabled.")
    else:
        log("Could not verify Accessibility permission automatically.")
        log("Typing may fail until permission is enabled.")


class SingleInstanceLock:
    """
    Prevent multiple app instances from running at the same time.
    """

    def __init__(self, name: str) -> None:
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
        self.path = os.path.join(tempfile.gettempdir(), f"{safe_name}.lock")
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+")
        self._fh.seek(0)
        self._fh.write("1")
        self._fh.flush()
        self._fh.seek(0)

        try:
            if os.name == "nt":
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False

        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def run() -> None:
    log("Starting NFC keyboard wedge.")
    current_date_note = "Press Ctrl+C to stop."
    log(current_date_note)
    ensure_macos_accessibility_permission()

    initial_readers = get_reader_list()
    if not initial_readers:
        log("No PC/SC readers detected at startup. Waiting for reader...")
        while not initial_readers:
            time.sleep(1.0)
            initial_readers = get_reader_list()
        log("Reader(s) detected.")

    selected_reader_name = choose_reader_name(initial_readers)
    keyboard = KeyboardController()

    card_present_uid: Optional[str] = None
    last_typed_by_uid = {}  # uid -> monotonic timestamp
    reader_missing_logged = False
    removal_pending_since: Optional[float] = None

    while True:
        reader_obj = resolve_reader_by_name(selected_reader_name)
        if reader_obj is None:
            if not reader_missing_logged:
                log(f"Selected reader unavailable: {selected_reader_name}")
                log("Waiting for reader to reconnect...")
                reader_missing_logged = True
            if card_present_uid is not None:
                log("Card removal detected (reader unavailable).")
                card_present_uid = None
            time.sleep(1.0)
            continue

        if reader_missing_logged:
            log(f"Reader is back: {selected_reader_name}")
            reader_missing_logged = False

        connection = None
        try:
            connection = reader_obj.createConnection()
            connection.connect()

            uid_raw = get_uid(connection)
            uid = sanitize_uid(uid_raw) if uid_raw else ""
            if not uid:
                # Treat missing UID as a possible removal, but confirm over time
                # to avoid false removals from brief reader glitches.
                now = time.monotonic()
                if card_present_uid is not None:
                    if removal_pending_since is None:
                        removal_pending_since = now
                    elif (now - removal_pending_since) >= REMOVAL_CONFIRM_SECONDS:
                        log("Card removed.")
                        card_present_uid = None
                        removal_pending_since = None
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            removal_pending_since = None

            # New tap event: process only when card transitions from absent->present
            # or changes UID while present.
            if uid != card_present_uid:
                if card_present_uid is not None:
                    log("Card changed or re-presented.")
                card_present_uid = uid

                log("Card detected.")
                log(f"UID: {uid}")

                output: Optional[str] = None
                if ENABLE_NDEF_READ:
                    output = read_ndef_text_or_uri(connection)
                    if output:
                        log(f"NDEF text found: {output}")

                if output is None:
                    if ENABLE_UID_FALLBACK:
                        mapped = CARD_MAP.get(uid)
                        if mapped is not None:
                            output = mapped
                            if ENABLE_NDEF_READ:
                                log(f"NDEF not found. Using UID mapping: {mapped}")
                            else:
                                log(f"Using UID mapping: {mapped}")
                        else:
                            if ENABLE_NDEF_READ:
                                log("No NDEF text and UID not mapped.")
                            else:
                                log("UID not mapped.")
                    else:
                        log("No readable NDEF text/URI found on this card.")

                if output is not None:
                    now = time.monotonic()
                    last = last_typed_by_uid.get(uid)
                    if last is not None and (now - last) < COOLDOWN_SECONDS:
                        remaining = COOLDOWN_SECONDS - (now - last)
                        log(
                            f"Cooldown active ({remaining:.2f}s left). Skipping type for UID {uid}."
                        )
                    else:
                        try:
                            type_output(output, keyboard)
                            last_typed_by_uid[uid] = now
                            log(f"Typed output: {output}")
                        except Exception as type_exc:
                            log(f"Typing failed: {type_exc}")

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if is_no_card_exception(exc):
                if card_present_uid is not None:
                    now = time.monotonic()
                    if removal_pending_since is None:
                        removal_pending_since = now
                    elif (now - removal_pending_since) >= REMOVAL_CONFIRM_SECONDS:
                        log("Card removed.")
                        card_present_uid = None
                        removal_pending_since = None
            elif isinstance(exc, CardConnectionException):
                # Covers many transient reader/card errors.
                if card_present_uid is not None:
                    now = time.monotonic()
                    if removal_pending_since is None:
                        removal_pending_since = now
                    elif (now - removal_pending_since) >= REMOVAL_CONFIRM_SECONDS:
                        log("Card removed or communication error.")
                        card_present_uid = None
                        removal_pending_since = None
            else:
                log(f"Read error: {exc}")
                if card_present_uid is not None:
                    now = time.monotonic()
                    if removal_pending_since is None:
                        removal_pending_since = now
                    elif (now - removal_pending_since) >= REMOVAL_CONFIRM_SECONDS:
                        log("Card removed after repeated read errors.")
                        card_present_uid = None
                        removal_pending_since = None
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:
                    pass

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    instance_lock = SingleInstanceLock("nfc_keyboard_wedge_main")
    if not instance_lock.acquire():
        log("Another instance is already running. Close it first, then run again.")
        sys.exit(1)

    try:
        run()
    except KeyboardInterrupt:
        log("Stopping NFC keyboard wedge (Ctrl+C).")
        sys.exit(0)
    finally:
        instance_lock.release()
