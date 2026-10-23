#!/usr/bin/env python3
"""ME7.5 MultiMap Selective Launch Control — MapSwitch-style Qt GUI.

Requirements: Python 3.10+, PySide6, Wine and the original ME7 tools.
Run: python3 Launch_MultiMap_Selective_GUI_FINAL_v3.py
The adjacent launch_multimap.ui is editable with Qt Designer and optional:
the same complete form is embedded below for single-file use.

The existing patching, ECU, XDF and checksum functions are preserved.
"""
from __future__ import annotations
import os

import argparse
import re
import struct
import sys
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


APP_NAME = "ME7 Launch Control Utility"
APP_VERSION = "1.0"

SUPPORTED_BIN_SIZES = {512 * 1024, 1024 * 1024}

CONFIG_SEARCH_START = 0x17000
CONFIG_SEARCH_END = 0x18000
CONFIG_AREA_SIZE = 64
CODE_CAVE_SIZE = 256
SETZI_FUNCTION_SIZE = 144
SETZI_COMPATIBLE_CODE_START_1MB = 0x70000

DEFAULT_NLS_COUNTER = 0x384FF0

MARKER = b"ME7LC001"
MARKER_OFFSET = 10
METADATA_VERSION_OFFSET = 18
CUT_MODE_RESERVED_OFFSET = 19
ORIGINAL_FTOMN_OFFSET = 20
FTOMN_ADDRESS_OFFSET = 22
CODE_CAVE_ADDRESS_OFFSET = 26
HOOK_ADDRESS_OFFSET = 30
ORIGINAL_HOOK_BYTES_OFFSET = 34
NORMAL_IGNITION_RAW_OFFSET = 38
METADATA_SIZE = 40

DEFAULT_SPEED_KMH = 3.0
DEFAULT_LAUNCH_RPM = 4500
DEFAULT_RPM_THRESHOLD = 5500
DEFAULT_ACCELERATOR_PERCENT = 90.0
DEFAULT_IGNITION_CUT_MS = 200
FTOMN_HARD_CUT = 0x00

TRIGGER_CLUTCH = "Clutch"
TRIGGER_BRAKE = "Brake"
TRIGGER_REFERENCE_OFFSETS = ((1, 3), (43, 45))


class LaunchError(RuntimeError):
    """Raised when the BIN cannot be safely processed."""


@dataclass
class EcuEntry:
    name: str
    columns: List[str]


@dataclass
class Installation:
    config_address: int
    code_cave_address: int
    hook_address: int
    ftomn_address: int
    original_ftomn: int
    original_hook_bytes: bytes
    normal_ignition_raw: int
    managed: bool = True


@dataclass
class Configuration:
    speed_kmh: float
    launch_rpm: int
    rpm_threshold: int
    accelerator_percent: float
    ignition_cut_ms: int
    minimum_temperature: int = 75
    trigger: str = TRIGGER_CLUTCH


def info(message: str) -> None:
    print(f"[INFO] {message}")


def ok(message: str) -> None:
    print(f"[ OK ] {message}")


def warn(message: str) -> None:
    print(f"[WARN] {message}")


def fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)


def banner() -> None:
    line = "=" * 64
    print(line)
    print("ME7 Launch Control CMD V2".center(64))
    print("Bosch ME7 / ME7.5 Launch Control Installer".center(64))
    print(line)


def parse_int(value: str) -> int:
    value = value.strip()
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def output_name(input_path: Path, suffix: str = "_mod") -> Path:
    stem = input_path.stem
    extension = input_path.suffix or ".bin"
    candidate = input_path.with_name(f"{stem}{suffix}{extension}")
    counter = 2
    while candidate.exists():
        candidate = input_path.with_name(f"{stem}{suffix}_{counter}{extension}")
        counter += 1
    return candidate


def parse_ecu_file(path: Path) -> Dict[str, EcuEntry]:
    try:
        text = path.read_text(encoding="latin-1")
    except OSError as exc:
        raise LaunchError(f"Cannot read ECU file: {exc}") from exc

    entries: Dict[str, EcuEntry] = {}
    for raw_line in text.splitlines():
        line = raw_line.replace("\r", "")
        stripped = line.lstrip()
        if not stripped or stripped[0] in ";#/[":
            continue

        comments: List[str] = []

        def keep_comment(match: re.Match[str]) -> str:
            comments.append(match.group(1))
            return f"#COMMENT{len(comments) - 1}"

        line = re.sub(r"\{([^}]*)\}", keep_comment, line)
        line = line.replace("\t", "").replace(" ", "")
        columns = line.split(",")
        if len(columns) < 10:
            continue

        for index, comment in enumerate(comments):
            columns = [item.replace(f"#COMMENT{index}", comment) for item in columns]

        name = columns[0].lower()
        entries[name] = EcuEntry(name=name, columns=columns[1:])

    if not entries:
        raise LaunchError(
            "The ECU file contains no usable entries. Create it with ME7Info using: "
            "me7info -n file.bin"
        )
    return entries


def ecu_address(entries: Dict[str, EcuEntry], name: str) -> Optional[int]:
    entry = entries.get(name.lower())
    if entry is None or len(entry.columns) < 2:
        return None

    value = entry.columns[1].strip()
    try:
        return int(value, 16)
    except ValueError:
        return None


def ecu_mask(entries: Dict[str, EcuEntry], name: str) -> Optional[int]:
    entry = entries.get(name.lower())
    if entry is None or len(entry.columns) < 4:
        return None

    value = entry.columns[3].strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    try:
        mask = int(value, 16)
    except ValueError:
        return None

    if mask <= 0 or mask & (mask - 1):
        return None
    return mask.bit_length() - 1


def require_address(entries: Dict[str, EcuEntry], name: str) -> int:
    value = ecu_address(entries, name)
    if value is None:
        raise LaunchError(f"Required ECU variable not found: {name}")
    ok(f"{name} found at 0x{value:06X}")
    return value


def resolve_wped(entries: Dict[str, EcuEntry]) -> int:
    wped = ecu_address(entries, "wped")
    if wped is not None:
        ok(f"wped found at 0x{wped:06X}")
        return wped

    dwped = ecu_address(entries, "dwped")
    if dwped is not None:
        wped = dwped + 2
        ok(f"wped derived from dwped + 2: 0x{wped:06X}")
        return wped

    raise LaunchError("Required ECU variable not found: wped or dwped")


def require_bit(entries: Dict[str, EcuEntry], name: str) -> tuple[int, int]:
    address = ecu_address(entries, name)
    bit = ecu_mask(entries, name)
    if address is None or bit is None:
        raise LaunchError(f"Required ECU bit variable not found or invalid: {name}")
    ok(f"{name} found at 0x{address:06X}.{bit}")
    return address, bit


def is_ecu_address_used(entries: Dict[str, EcuEntry], absolute_address: int) -> Optional[str]:
    wanted = f"0x{absolute_address:X}".lower()
    for name, entry in entries.items():
        if any(item.lower() == wanted for item in entry.columns):
            return name
    return None


def find_hole(
    data: Sequence[int],
    size: int,
    start: int = 0,
    end: Optional[int] = None,
    alignment: int = 16,
) -> Optional[int]:
    if end is None or end >= len(data):
        end = len(data) - 64
    start = max(0, start)

    run_start: Optional[int] = None
    for index in range(start, end):
        if data[index] == 0xFF:
            if run_start is None:
                run_start = index
        else:
            if run_start is not None:
                aligned = (run_start + alignment - 1) & ~(alignment - 1)
                if aligned + size <= index:
                    return aligned
            run_start = None

    if run_start is not None:
        aligned = (run_start + alignment - 1) & ~(alignment - 1)
        if aligned + size <= end:
            return aligned
    return None


def find_free_bool(data: bytes) -> int:
    hex_data = data.hex()

    for safe in (True, False):
        for candidate in range(1, 127):
            token = f"{candidate:x}"
            if len(token) == 1:
                token = "0" + token

            if safe:
                matches = list(re.finditer(rf"9a{token}...0", hex_data))
                if any(match.start() % 2 != 0 for match in matches):
                    continue

            matches = list(re.finditer(rf"8a{token}...0", hex_data))
            if any(match.start() % 2 != 0 for match in matches):
                continue

            return candidate

        if safe:
            warn("No safely unused status flag was found; trying relaxed search.")

    raise LaunchError("Cannot find an unused status flag variable.")


def find_ftomn(data: bytes, memory_layout_kb: int) -> List[int]:
    found: List[int] = []
    limit = len(data)

    for index in range(0, max(0, limit - 26)):
        if (
            data[index] == 0x05
            and data[index + 1] != 0x05
            and data[index + 11] == 0x05
            and data[index + 24] == 0x08
            and data[index + 25] == 0x05
        ):
            found.append(index + 22)

    if not found:
        for index in range(0, max(0, limit - 13)):
            if (
                data[index] == 0x05
                and data[index + 1] != 0x05
                and data[index + 11] == 0x05
                and data[index + 12] == 0x07
            ):
                found.append(index + 11)

    if not found and memory_layout_kb == 512:
        pattern = re.compile(b"\xC2\xF4..\x40\x94\x9D\x02\xC2\xF9", re.DOTALL)
        matches = list(pattern.finditer(data))
        if matches:
            position = matches[-1].start() + 10
            if position + 2 <= len(data):
                raw = data[position : position + 2]
                address = int("1" + raw[::-1].hex(), 16)
                if address < len(data):
                    found.append(address)

    return sorted(set(found))


def find_hook(data: bytes, memory_layout_kb: int) -> int:
    if memory_layout_kb == 1024:
        signature = b"\xD7\x40\x06\x02\x03\xF8"
        locations: List[int] = []
        start = 0
        while True:
            position = data.find(signature, start)
            if position < 0:
                break
            if position >= 4:
                locations.append(position - 4)
            start = position + 1
        if not locations:
            raise LaunchError("Cannot find the Launch Control hook signature.")
        return locations[-1]

    pattern = re.compile(b"\xF0\x49\xF7\xF8..\xF3\xF8", re.DOTALL)
    matches = list(pattern.finditer(data))
    if not matches:
        raise LaunchError("Cannot find the Launch Control hook signature.")
    return matches[-1].start() + 6


def encode_calls_address(code_cave: int) -> bytes:
    address = code_cave + 0x800000
    text = f"{address:06x}"
    return bytes.fromhex(text[0:2] + text[4:6] + text[2:4])


def word_little(value: int) -> bytes:
    return struct.pack("<H", value & 0xFFFF)


def flash_operand(value: int) -> bytes:
    return word_little(value)


def ram_operand_from_absolute(value: int) -> bytes:
    return word_little(value + 0x8000)


def direct_operand_from_hex_address(value: int) -> bytes:
    return word_little(value)


def bit_register_offset(address: int) -> int:
    relative = address - 0xFD00
    if relative < 0 or relative % 2:
        raise LaunchError(f"Invalid bit-register address: 0x{address:06X}")
    result = relative // 2
    if not 0 <= result <= 0xFF:
        raise LaunchError(f"Bit-register address is outside supported range: 0x{address:06X}")
    return result


def bit_nibble(bit: int) -> int:
    if not 0 <= bit <= 15:
        raise LaunchError(f"Invalid bit number: {bit}")
    return bit << 4


def trigger_reference_bytes(address: int, bit: int) -> tuple[int, int]:
    """Return the C167 bit-register operand bytes for a trigger input."""
    return bit_register_offset(address), bit_nibble(bit)


def detect_launch_trigger(
    data: bytes | bytearray,
    function_address: int,
    clutch_address: int,
    clutch_bit: int,
    brake_address: int,
    brake_bit: int,
) -> str:
    """Detect whether the installed Launch Control uses Clutch or Brake."""
    if function_address < 0 or function_address + SETZI_FUNCTION_SIZE > len(data):
        raise LaunchError("The Launch Control function is outside the BIN file.")

    clutch_reference = trigger_reference_bytes(clutch_address, clutch_bit)
    brake_reference = trigger_reference_bytes(brake_address, brake_bit)

    references = []
    for address_offset, bit_offset in TRIGGER_REFERENCE_OFFSETS:
        references.append(
            (
                data[function_address + address_offset],
                data[function_address + bit_offset],
            )
        )

    if all(reference == clutch_reference for reference in references):
        return TRIGGER_CLUTCH
    if all(reference == brake_reference for reference in references):
        return TRIGGER_BRAKE

    raise LaunchError(
        "The current Launch Control trigger could not be identified safely. "
        "The trigger references in the installed function are inconsistent."
    )


def write_launch_trigger(
    data: bytearray,
    function_address: int,
    trigger: str,
    clutch_address: int,
    clutch_bit: int,
    brake_address: int,
    brake_bit: int,
) -> None:
    """Change the installed Launch Control trigger without rebuilding the patch."""
    if function_address < 0 or function_address + SETZI_FUNCTION_SIZE > len(data):
        raise LaunchError("The Launch Control function is outside the BIN file.")

    # These are the two trigger bit-test instructions in the Setzi function.
    if data[function_address] not in (0x9A, 0x8A):
        raise LaunchError("Unexpected first trigger instruction in Launch Control code.")
    if data[function_address + 42] not in (0x9A, 0x8A):
        raise LaunchError("Unexpected second trigger instruction in Launch Control code.")

    if trigger == TRIGGER_CLUTCH:
        address, bit = clutch_address, clutch_bit
    elif trigger == TRIGGER_BRAKE:
        address, bit = brake_address, brake_bit
    else:
        raise LaunchError(f"Unsupported trigger selection: {trigger}")

    address_byte, bit_byte = trigger_reference_bytes(address, bit)
    for address_offset, bit_offset in TRIGGER_REFERENCE_OFFSETS:
        data[function_address + address_offset] = address_byte
        data[function_address + bit_offset] = bit_byte

    verified = detect_launch_trigger(
        data,
        function_address,
        clutch_address,
        clutch_bit,
        brake_address,
        brake_bit,
    )
    if verified != trigger:
        raise LaunchError(
            f"Trigger verification failed: expected {trigger}, detected {verified}."
        )


def nls_counter_operand(absolute_address: int) -> bytes:
    relative = absolute_address - 0x380000
    if not 0 <= relative <= 0xFFFF:
        raise LaunchError(
            f"NLS counter 0x{absolute_address:06X} is outside the supported 0x38xxxx range."
        )
    return struct.pack("<H", relative)


def append(buffer: bytearray, *parts: int | bytes) -> None:
    for part in parts:
        if isinstance(part, int):
            buffer.append(part & 0xFF)
        else:
            buffer.extend(part)


def build_launch_code(
    *,
    config_address: int,
    tsrldyn: int,
    vfil_w: int,
    nmot_w: int,
    wped: int,
    clutch_address: int,
    clutch_bit: int,
    brake_address: int,
    brake_bit: int,
    nls_counter_address: int,
    original_hook_bytes_2_3: bytes,
    replay_displaced_instruction: bool = True,
    clutch_only: bool = False,
) -> bytes:
    """
    Build the C167 machine code used by the original PHP utility.

    The temperature precondition from the PHP modification is deliberately
    omitted. This version starts directly with the LC/NLS conditions.
    """
    code = bytearray()
    nls = nls_counter_operand(nls_counter_address)

    append(
        code,
        0x9A,
        bit_register_offset(clutch_address),
        0x13,
        bit_nibble(clutch_bit),
        0xF2,
        0xF4,
        ram_operand_from_absolute(vfil_w),
        0xD7,
        0x00,
        0x81,
        0x00,
        0xF2,
        0xF9,
        flash_operand(config_address),
    )

    append(
        code,
        0x40,
        0x49,
        0x9D,
        0x0B,
        0xF2,
        0xF4,
        direct_operand_from_hex_address(nmot_w),
        0xD7,
        0x00,
        0x81,
        0x00,
        0xF2,
        0xF9,
        flash_operand(config_address + 2),
    )

    append(
        code,
        0x40,
        0x49,
        0xFD,
        0x03,
        0xF7,
        0x8E,
        ram_operand_from_absolute(tsrldyn),
        0x0D,
        0x2F,
        0x9A,
        bit_register_offset(clutch_address),
        0x29,
        bit_nibble(clutch_bit),
    )

    # The original Setzi/NLS routine has a 4-byte brake-bit test here.
    # In selective MultiMap LC mode the activation logic is clutch-only.
    # Keep the exact instruction footprint with two C167 NOPs so all
    # existing relative branch displacements remain valid.
    if clutch_only:
        append(code, 0xCC, 0x00, 0xCC, 0x00)
    else:
        append(
            code,
            0x8A,
            bit_register_offset(brake_address),
            0x22,
            bit_nibble(brake_bit),
        )

    append(
        code,
        0xF2,
        0xF4,
        direct_operand_from_hex_address(nmot_w),
        0xD7,
        0x00,
        0x81,
        0x00,
        0xF2,
        0xF9,
        flash_operand(config_address + 6),
        0x40,
        0x49,
    )

    append(
        code,
        0xFD,
        0x1A,
        0xC2,
        0xF4,
        ram_operand_from_absolute(wped),
        0xD7,
        0x00,
        0x81,
        0x00,
        0xC2,
        0xF9,
        flash_operand(config_address + 8),
        0x40,
        0x49,
    )

    append(
        code,
        0xFD,
        0x12,
        0xD7,
        0x00,
        0x38,
        0x00,
        0xF2,
        0xF4,
        nls,
        0xD7,
        0x00,
        0x81,
        0x00,
        0xF2,
        0xF9,
    )

    append(
        code,
        flash_operand(config_address + 4),
        0x40,
        0x49,
        0x9D,
        0x11,
        0xF7,
        0x8E,
        ram_operand_from_absolute(tsrldyn),
        0x08,
        0x41,
        0xD7,
        0x00,
        0x38,
    )

    append(
        code,
        0x00,
        0xF7,
        0xF8,
        nls,
        0x0D,
        0x09,
        0xD7,
        0x00,
        0x38,
        0x00,
        0xF6,
        0x8F,
        nls,
        0x0D,
        0x04,
    )

    append(
        code,
        0xD7,
        0x00,
        0x38,
        0x00,
        0xF6,
        0x8E,
        nls,
    )

    # In the normal Setzi installation the LC function must replay the
    # displaced 4-byte stock instruction (F3 F8 xx xx) before RETS.
    # When we chain behind an existing CALLS hook (e.g. MultiMap runtime),
    # that previous hook already performs the displaced stock instruction,
    # so replaying it a second time would be incorrect.
    if replay_displaced_instruction:
        append(code, 0xF3, 0xF8, original_hook_bytes_2_3)
    append(code, 0xDB, 0x00)

    if len(code) > CODE_CAVE_SIZE:
        raise LaunchError(
            f"Generated code is too large ({len(code)} bytes; maximum is {CODE_CAVE_SIZE})."
        )
    return bytes(code)


def encode_speed(value: float) -> int:
    raw = round(value / 0.0078125)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError("Speed Threshold is outside the supported range.")
    return raw


def decode_speed(raw: int) -> float:
    return raw * 0.0078125


def encode_rpm(value: int) -> int:
    raw = round(value / 0.25)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError("RPM value is outside the supported range.")
    return raw


def decode_rpm(raw: int) -> int:
    return round(raw * 0.25)


def encode_ignition_ms(value: int) -> int:
    if value < 0 or value % 20 != 0:
        raise ValueError("Ignition Cut Duration must be a multiple of 20 ms.")
    raw = value // 20
    if not 0 <= raw <= 0xFFFF:
        raise ValueError("Ignition Cut Duration is outside the supported range.")
    return raw


def decode_ignition_ms(raw: int) -> int:
    return raw * 20


def encode_pedal(value: float) -> int:
    raw = round(value / 0.392157)
    if not 0 <= raw <= 0xFF:
        raise ValueError("Accelerator Threshold must be between 0 and 100 percent.")
    return raw


def decode_pedal(raw: int) -> float:
    return raw * 0.392157




def encode_temperature(value: int) -> int:
    raw = round((value + 48) / 0.75)
    if not 0 <= raw <= 0xFF:
        raise ValueError("Minimum Temperature is outside the supported range.")
    return raw


def decode_temperature(raw: int) -> int:
    return round(raw * 0.75 - 48)

def read_configuration(data: bytes | bytearray, address: int) -> Configuration:
    speed_raw, launch_raw, ignition_raw, rpm_threshold_raw = struct.unpack_from(
        "<HHHH", data, address
    )
    pedal_raw = data[address + 8]
    temperature_raw = data[address + 9]

    return Configuration(
        speed_kmh=decode_speed(speed_raw),
        launch_rpm=decode_rpm(launch_raw),
        rpm_threshold=decode_rpm(rpm_threshold_raw),
        accelerator_percent=decode_pedal(pedal_raw),
        ignition_cut_ms=decode_ignition_ms(ignition_raw),
        minimum_temperature=decode_temperature(temperature_raw),
    )


def write_configuration(
    data: bytearray,
    address: int,
    configuration: Configuration,
    normal_ignition_raw: int,
) -> int:
    ignition_raw = encode_ignition_ms(configuration.ignition_cut_ms)

    struct.pack_into(
        "<HHHHB",
        data,
        address,
        encode_speed(configuration.speed_kmh),
        encode_rpm(configuration.launch_rpm),
        ignition_raw,
        encode_rpm(configuration.rpm_threshold),
        encode_pedal(configuration.accelerator_percent),
    )
    data[address + 9] = encode_temperature(configuration.minimum_temperature)
    data[address + CUT_MODE_RESERVED_OFFSET] = 0
    struct.pack_into("<H", data, address + NORMAL_IGNITION_RAW_OFFSET, normal_ignition_raw)
    return ignition_raw


def write_metadata(
    data: bytearray,
    *,
    config_address: int,
    code_cave_address: int,
    hook_address: int,
    ftomn_address: int,
    original_ftomn: int,
    original_hook_bytes: bytes,
    normal_ignition_raw: int,
) -> None:
    data[config_address + MARKER_OFFSET : config_address + MARKER_OFFSET + len(MARKER)] = MARKER
    data[config_address + METADATA_VERSION_OFFSET] = 1
    data[config_address + CUT_MODE_RESERVED_OFFSET] = 0
    data[config_address + ORIGINAL_FTOMN_OFFSET] = original_ftomn
    data[config_address + 21] = 0xFF
    struct.pack_into("<I", data, config_address + FTOMN_ADDRESS_OFFSET, ftomn_address)
    struct.pack_into("<I", data, config_address + CODE_CAVE_ADDRESS_OFFSET, code_cave_address)
    struct.pack_into("<I", data, config_address + HOOK_ADDRESS_OFFSET, hook_address)
    data[
        config_address + ORIGINAL_HOOK_BYTES_OFFSET :
        config_address + ORIGINAL_HOOK_BYTES_OFFSET + 4
    ] = original_hook_bytes
    struct.pack_into("<H", data, config_address + NORMAL_IGNITION_RAW_OFFSET, normal_ignition_raw)


def detect_installation(data: bytes) -> Optional[Installation]:
    start = 0
    while True:
        marker_position = data.find(MARKER, start)
        if marker_position < 0:
            return None

        config_address = marker_position - MARKER_OFFSET
        start = marker_position + 1

        if config_address < 0 or config_address + METADATA_SIZE > len(data):
            continue

        try:
            ftomn_address = struct.unpack_from(
                "<I", data, config_address + FTOMN_ADDRESS_OFFSET
            )[0]
            code_cave_address = struct.unpack_from(
                "<I", data, config_address + CODE_CAVE_ADDRESS_OFFSET
            )[0]
            hook_address = struct.unpack_from(
                "<I", data, config_address + HOOK_ADDRESS_OFFSET
            )[0]
            normal_ignition_raw = struct.unpack_from(
                "<H", data, config_address + NORMAL_IGNITION_RAW_OFFSET
            )[0]
        except struct.error:
            continue

        if not (
            0 <= ftomn_address < len(data)
            and 0 <= code_cave_address < len(data)
            and 0 <= hook_address + 4 <= len(data)
        ):
            continue

        if data[hook_address] != 0xDA:
            continue

        return Installation(
            config_address=config_address,
            code_cave_address=code_cave_address,
            hook_address=hook_address,
            ftomn_address=ftomn_address,
            original_ftomn=data[config_address + ORIGINAL_FTOMN_OFFSET],
            original_hook_bytes=data[
                config_address + ORIGINAL_HOOK_BYTES_OFFSET :
                config_address + ORIGINAL_HOOK_BYTES_OFFSET + 4
            ],
            normal_ignition_raw=normal_ignition_raw,
            managed=True,
        )



LEGACY_FUNCTION_LENGTH = 144
LEGACY_CONDITION_OFFSETS = {
    "SpeedThreshold": 14,
    "LaunchRPM": 30,
    "RPMThreshold": 60,
    "AcceleratorThreshold": 76,
    "IgnitionCutDuration": 96,
}


def read_legacy_configuration(
    data: bytes | bytearray,
    address: int,
) -> Configuration:
    """Read only the original 9-byte Setzi configuration block."""
    speed_raw, launch_raw, ignition_raw, rpm_threshold_raw = struct.unpack_from(
        "<HHHH", data, address
    )
    pedal_raw = data[address + 8]

    return Configuration(
        speed_kmh=decode_speed(speed_raw),
        launch_rpm=decode_rpm(launch_raw),
        rpm_threshold=decode_rpm(rpm_threshold_raw),
        accelerator_percent=decode_pedal(pedal_raw),
        ignition_cut_ms=decode_ignition_ms(ignition_raw),
    )


def write_legacy_configuration(
    data: bytearray,
    address: int,
    configuration: Configuration,
) -> int:
    """
    Update only the original Setzi configuration bytes.

    No ME7LC001 marker or restoration metadata is written because the area after
    the 9-byte legacy block may contain unrelated calibration data.
    """
    ignition_raw = encode_ignition_ms(configuration.ignition_cut_ms)
    struct.pack_into(
        "<HHHHB",
        data,
        address,
        encode_speed(configuration.speed_kmh),
        encode_rpm(configuration.launch_rpm),
        ignition_raw,
        encode_rpm(configuration.rpm_threshold),
        encode_pedal(configuration.accelerator_percent),
    )
    return ignition_raw


def decode_calls_target(data: bytes | bytearray, hook_address: int) -> Optional[int]:
    """Decode C167 CALLS bytes DA bank low high to a BIN file offset."""
    if hook_address < 0 or hook_address + 4 > len(data):
        return None
    if data[hook_address] != 0xDA:
        return None

    bank = data[hook_address + 1]
    low = data[hook_address + 2]
    high = data[hook_address + 3]
    return ((bank << 16) | (high << 8) | low) & 0xFFFFF


def normalize_legacy_config_address(
    data: bytes | bytearray,
    reference: int,
) -> Optional[int]:
    candidates: List[int] = []

    if reference < 0x10000:
        candidates.append(reference + 0x10000)
    candidates.append(reference)

    for candidate in candidates:
        if not (0 <= candidate <= len(data) - 9):
            continue

        block = bytes(data[candidate : candidate + 9])
        if block in (b"\x00" * 9, b"\xFF" * 9):
            continue

        try:
            speed_raw, launch_raw, ignition_raw, rpm_raw = struct.unpack_from(
                "<HHHH", data, candidate
            )
            pedal_raw = data[candidate + 8]
        except (struct.error, IndexError):
            continue

        # Broad plausibility checks prevent random machine code from being
        # mistaken for a configuration block.
        speed = decode_speed(speed_raw)
        launch_rpm = decode_rpm(launch_raw)
        ignition_ms = decode_ignition_ms(ignition_raw)
        rpm_threshold = decode_rpm(rpm_raw)
        pedal = decode_pedal(pedal_raw)

        if not (0 <= speed <= 511):
            continue
        if not (500 <= launch_rpm <= 16000):
            continue
        if not (0 <= ignition_ms <= 5000):
            continue
        if not (500 <= rpm_threshold <= 16000):
            continue
        if not (0 <= pedal <= 100.5):
            continue

        return candidate

    return None


def find_compatible_setzi_installation(
    data: bytes,
    memory_layout_kb: int,
) -> Optional[Installation]:
    """
    Detect a Launch Control patch installed by launch.php/launch.exe or another
    compatible Setzi-based tool, even when the ME7LC001 marker is absent.
    """
    scan_start = 0x70000 if len(data) > 0x80000 else 0
    scan_end = max(scan_start, len(data) - LEGACY_FUNCTION_LENGTH)

    ftomn_candidates = find_ftomn(data, memory_layout_kb)
    if not ftomn_candidates:
        return None

    for function_address in range(scan_start, scan_end):
        if data[function_address] not in (0x9A, 0x8A):
            continue

        end = function_address + LEGACY_FUNCTION_LENGTH
        if end > len(data):
            break

        # Structural signatures used by the Setzi LC/NLS implementation.
        if data[function_address + 4 : function_address + 6] != b"\xF2\xF4":
            continue
        if data[function_address + 16 : function_address + 20] != b"\x40\x49\x9D\x0B":
            continue

        refs = {
            name: int.from_bytes(
                data[
                    function_address + offset :
                    function_address + offset + 2
                ],
                "little",
            )
            for name, offset in LEGACY_CONDITION_OFFSETS.items()
        }

        base_ref = refs["SpeedThreshold"]
        if not (
            refs["LaunchRPM"] == ((base_ref + 2) & 0xFFFF)
            and refs["IgnitionCutDuration"] == ((base_ref + 4) & 0xFFFF)
            and refs["RPMThreshold"] == ((base_ref + 6) & 0xFFFF)
            and refs["AcceleratorThreshold"] == ((base_ref + 8) & 0xFFFF)
        ):
            continue

        config_address = normalize_legacy_config_address(data, base_ref)
        if config_address is None:
            continue

        hook_address: Optional[int] = None
        search_from = 0
        signature = b"\xD7\x40\x06\x02\x03\xF8"
        while True:
            signature_position = data.find(signature, search_from)
            if signature_position < 0:
                break
            candidate_hook = signature_position - 4
            if (
                candidate_hook >= 0
                and decode_calls_target(data, candidate_hook) == function_address
            ):
                hook_address = candidate_hook
                break
            search_from = signature_position + 1

        if hook_address is None:
            continue

        ftomn_address = ftomn_candidates[0]
        ftomn_value = data[ftomn_address]
        ignition_raw = int.from_bytes(
            data[config_address + 4 : config_address + 6],
            "little",
        )

        return Installation(
            config_address=config_address,
            code_cave_address=function_address,
            hook_address=hook_address,
            ftomn_address=ftomn_address,
            original_ftomn=ftomn_value,
            original_hook_bytes=b"",
                normal_ignition_raw=ignition_raw,
            managed=False,
        )

    return None

def default_configuration() -> Configuration:
    return Configuration(
        speed_kmh=DEFAULT_SPEED_KMH,
        launch_rpm=DEFAULT_LAUNCH_RPM,
        rpm_threshold=DEFAULT_RPM_THRESHOLD,
        accelerator_percent=DEFAULT_ACCELERATOR_PERCENT,
        ignition_cut_ms=DEFAULT_IGNITION_CUT_MS,
        minimum_temperature=75,
    )



def safe_input(prompt: str) -> str:
    """Read console input and provide a clear error when stdin is unavailable."""
    try:
        return input(prompt)
    except EOFError as exc:
        raise LaunchError(
            "Console input is unavailable. Run under Wine with: "
            "wineconsole --backend=curses launch.exe test.bin test.ecu"
        ) from exc


def prompt_float(label: str, current: float, minimum: float, maximum: float) -> float:
    while True:
        raw = safe_input(f"{label} [{current:g}]: ").strip()
        if not raw:
            return current
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            print("  Invalid number. Try again.")
            continue
        if not minimum <= value <= maximum:
            print(f"  Enter a value from {minimum:g} to {maximum:g}.")
            continue
        return value


def prompt_int(
    label: str,
    current: int,
    minimum: int,
    maximum: int,
    multiple: Optional[int] = None,
) -> int:
    while True:
        raw = safe_input(f"{label} [{current}]: ").strip()
        if not raw:
            return current
        try:
            value = int(raw)
        except ValueError:
            print("  Invalid integer. Try again.")
            continue
        if not minimum <= value <= maximum:
            print(f"  Enter a value from {minimum} to {maximum}.")
            continue
        if multiple and value % multiple:
            print(f"  The value must be a multiple of {multiple}.")
            continue
        return value


def prompt_yes_no(label: str, current: bool) -> bool:
    default = "Y" if current else "N"
    while True:
        raw = safe_input(f"{label} [Y/N] [{default}]: ").strip().lower()
        if not raw:
            return current
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  Enter Y or N.")


def prompt_trigger(current: str) -> str:
    """Prompt for the Launch Control activation trigger."""
    while True:
        print()
        print("ACTIVATION TRIGGER")
        print("  1. Clutch Manual Transmission")
        print("  2. Brake >>DSG Only<<")
        default = "1" if current == TRIGGER_CLUTCH else "2"
        raw = safe_input(
            f"Select Activation Trigger [1/2] [{default}]: "
        ).strip().lower()

        if not raw:
            return current
        if raw in {"1", "clutch", "c"}:
            return TRIGGER_CLUTCH
        if raw in {"2", "brake", "b"}:
            return TRIGGER_BRAKE
        print("  Enter 1 for Clutch or 2 for Brake.")


def configure(current: Configuration, normal_ignition_ms: int) -> Configuration:
    print()
    print("=" * 64)
    print("LAUNCH CONTROL CONFIGURATION".center(64))
    print("=" * 64)
    print()
    print("Current configuration:")
    print(f"  Speed Threshold           : {current.speed_kmh:.2f} km/h")
    print(f"  Launch RPM                : {current.launch_rpm} rpm")
    print(f"  RPM Threshold             : {current.rpm_threshold} rpm")
    print(f"  Accelerator Threshold     : {current.accelerator_percent:.1f} %")
    print(f"  Ignition Cut Duration     : {current.ignition_cut_ms} ms")
    print(f"  Minimum Temperature       : {current.minimum_temperature} °C")
    print(f"  Activation Trigger        : {current.trigger}")
    print(
        "  Cut Mode                  : Hard Cut (FTOMN 0x00)"
    )
    print()
    print("Press ENTER to keep the current value.")
    print()

    speed = prompt_float("Speed Threshold (km/h)", current.speed_kmh, 0.0, 511.0)
    launch_rpm = prompt_int("Launch RPM", current.launch_rpm, 500, 16000)
    rpm_threshold = prompt_int(
        "RPM Threshold", current.rpm_threshold, 500, 16000
    )
    accelerator = prompt_float(
        "Accelerator Threshold (%)", current.accelerator_percent, 0.0, 100.0
    )
    ignition_ms = prompt_int(
        "Ignition Cut Duration (ms)",
        current.ignition_cut_ms if current.ignition_cut_ms > 0 else normal_ignition_ms,
        20,
        5000,
        multiple=20,
    )

    minimum_temperature = prompt_int(
        "Minimum Temperature (°C)",
        current.minimum_temperature,
        0,
        120,
    )

    trigger = prompt_trigger(current.trigger)


    result = Configuration(
        speed_kmh=speed,
        launch_rpm=launch_rpm,
        rpm_threshold=rpm_threshold,
        accelerator_percent=accelerator,
        ignition_cut_ms=ignition_ms,
        minimum_temperature=minimum_temperature,
        trigger=trigger,
    )

    selected_ftomn = FTOMN_HARD_CUT

    print()
    print("=" * 64)
    print("CONFIGURATION SUMMARY".center(64))
    print("=" * 64)
    print(f"  Speed Threshold        : {result.speed_kmh:.2f} km/h")
    print(f"  Launch RPM             : {result.launch_rpm} rpm")
    print(f"  RPM Threshold          : {result.rpm_threshold} rpm")
    print(f"  Accelerator Threshold  : {result.accelerator_percent:.1f} %")
    print(f"  Ignition Cut Duration  : {result.ignition_cut_ms} ms")
    print(f"  Minimum Temperature    : {result.minimum_temperature} °C")
    print(f"  Activation Trigger     : {result.trigger}")
    print("  Cut Mode               : Hard Cut")
    print(f"  FTOMN Value            : 0x{selected_ftomn:02X}")
    print()

    if not prompt_yes_no("Save this configuration?", True):
        raise LaunchError("Operation cancelled by the user.")

    return result



def find_compatible_code_cave(
    data: bytes | bytearray,
    memory_layout_kb: int,
    *,
    exclude_start: Optional[int] = None,
    exclude_size: int = 0,
) -> Optional[int]:
    """
    Find a code cave in the range scanned by common Setzi/ME7 desktop tools.

    For a 1 MB BIN, compatible tools normally scan the upper calibration/code
    area from 0x70000. A cave below that range can work in the ECU but remain
    invisible to those tools.
    """
    start = SETZI_COMPATIBLE_CODE_START_1MB if memory_layout_kb == 1024 else 0
    end = len(data)

    position = start
    while position + CODE_CAVE_SIZE <= end:
        candidate = find_hole(
            data,
            CODE_CAVE_SIZE,
            start=position,
            end=end,
        )
        if candidate is None:
            return None

        if (
            exclude_start is not None
            and candidate < exclude_start + exclude_size
            and exclude_start < candidate + CODE_CAVE_SIZE
        ):
            position = candidate + CODE_CAVE_SIZE
            continue

        return candidate

    return None


def migrate_native_installation_to_setzi_visible_area(
    data: bytearray,
    installation: Installation,
    memory_layout_kb: int,
) -> Installation:
    """
    Move a native ME7LC001 installation to a code cave visible to compatible
    Setzi scanners when an older CMD version placed it too low in the BIN.

    The Launch function contains no self-relative reference to its own location,
    so copying the function and changing CALLS is sufficient.
    """
    if not installation.managed:
        return installation

    if memory_layout_kb != 1024:
        return installation

    if installation.code_cave_address >= SETZI_COMPATIBLE_CODE_START_1MB:
        return installation

    info(
        f"Native code cave 0x{installation.code_cave_address:X} is below "
        "the range scanned by compatible Setzi tools."
    )
    info("Migrating Launch Control to a compatible upper code cave.")

    new_code_cave = find_compatible_code_cave(
        data,
        memory_layout_kb,
        exclude_start=installation.config_address,
        exclude_size=CONFIG_AREA_SIZE,
    )
    if new_code_cave is None:
        raise LaunchError(
            "Cannot find a compatible 256-byte code cave above 0x70000."
        )

    old_code_cave = installation.code_cave_address
    function = bytes(
        data[old_code_cave : old_code_cave + SETZI_FUNCTION_SIZE]
    )
    if len(function) != SETZI_FUNCTION_SIZE:
        raise LaunchError("Cannot read the complete existing Launch function.")

    data[new_code_cave : new_code_cave + SETZI_FUNCTION_SIZE] = function
    data[
        new_code_cave + SETZI_FUNCTION_SIZE :
        new_code_cave + CODE_CAVE_SIZE
    ] = b"\xFF" * (CODE_CAVE_SIZE - SETZI_FUNCTION_SIZE)

    data[
        installation.hook_address :
        installation.hook_address + 4
    ] = b"\xDA" + encode_calls_address(new_code_cave)

    # Clear only the old code cave. Configuration and metadata live elsewhere.
    data[
        old_code_cave :
        old_code_cave + CODE_CAVE_SIZE
    ] = b"\xFF" * CODE_CAVE_SIZE

    installation.code_cave_address = new_code_cave

    write_metadata(
        data,
        config_address=installation.config_address,
        code_cave_address=new_code_cave,
        hook_address=installation.hook_address,
        ftomn_address=installation.ftomn_address,
        original_ftomn=installation.original_ftomn,
        original_hook_bytes=installation.original_hook_bytes,
        normal_ignition_raw=installation.normal_ignition_raw,
    )

    verified_native = detect_installation(bytes(data))
    if verified_native is None:
        raise LaunchError("Native installation migration verification failed.")

    verified_legacy = find_compatible_setzi_installation(
        bytes(data),
        memory_layout_kb,
    )
    if verified_legacy is None:
        raise LaunchError(
            "The migrated installation is still not visible to a compatible "
            "Setzi detector."
        )

    ok(
        f"Launch function migrated: 0x{old_code_cave:X} "
        f"-> 0x{new_code_cave:X}"
    )
    ok("Compatible Setzi detection verified")
    return verified_native

def install(
    data: bytearray,
    entries: Dict[str, EcuEntry],
    memory_layout_kb: int,
    nls_counter_address: int,
) -> Installation:
    info("Launch Control not detected.")
    info("Starting installation.")

    tsrldyn = require_address(entries, "tsrldyn")
    vfil_w = require_address(entries, "vfil_w")
    nmot_w = require_address(entries, "nmot_w")
    wped = resolve_wped(entries)
    clutch_address, clutch_bit = require_bit(entries, "b_kuppl")
    brake_address, brake_bit = require_bit(entries, "b_br")

    used_by = is_ecu_address_used(entries, nls_counter_address)
    if used_by:
        raise LaunchError(
            f"NLS counter address 0x{nls_counter_address:06X} is used by '{used_by}'."
        )

    free_bool = find_free_bool(bytes(data))
    ok(f"Unused status flag found at 0x00FD{free_bool * 2:02X}")

    ftomn_candidates = find_ftomn(bytes(data), memory_layout_kb)
    if not ftomn_candidates:
        raise LaunchError("FTOMN could not be found safely.")
    if len(ftomn_candidates) > 1:
        warn(
            "Multiple FTOMN candidates found: "
            + ", ".join(f"0x{x:X}" for x in ftomn_candidates)
        )
        warn(f"Using the first candidate: 0x{ftomn_candidates[0]:X}")

    ftomn_address = ftomn_candidates[0]
    original_ftomn = data[ftomn_address]
    ok(f"FTOMN found at 0x{ftomn_address:X}")
    info(f"Original FTOMN value: 0x{original_ftomn:02X}")

    code_cave = find_compatible_code_cave(data, memory_layout_kb)
    if code_cave is None:
        if memory_layout_kb == 1024:
            raise LaunchError(
                "Cannot find a safe 256-byte code cave in the compatible "
                "0x70000-0xFFFFF range."
            )
        raise LaunchError("Cannot find a safe 256-byte code cave.")
    ok(f"Code cave found at 0x{code_cave:X}")

    config_address = find_hole(
        data,
        CONFIG_AREA_SIZE,
        start=CONFIG_SEARCH_START,
        end=min(CONFIG_SEARCH_END, len(data)),
    )
    if config_address is None:
        raise LaunchError(
            "Cannot find a safe configuration area between 0x17000 and 0x18000."
        )
    ok(f"Configuration area found at 0x{config_address:X}")

    hook_address = find_hook(bytes(data), memory_layout_kb)
    ok(f"Hook location found at 0x{hook_address:X}")

    original_hook = bytes(data[hook_address : hook_address + 4])
    if original_hook[0] == 0xDA:
        raise LaunchError(
            "A CALLS instruction already exists at the hook, but this utility's "
            "installation marker was not found. Use an original BIN."
        )

    code = build_launch_code(
        config_address=config_address,
        tsrldyn=tsrldyn,
        vfil_w=vfil_w,
        nmot_w=nmot_w,
        wped=wped,
        clutch_address=clutch_address,
        clutch_bit=clutch_bit,
        brake_address=brake_address,
        brake_bit=brake_bit,
        nls_counter_address=nls_counter_address,
        original_hook_bytes_2_3=original_hook[2:4],
    )

    data[code_cave : code_cave + len(code)] = code
    data[hook_address : hook_address + 4] = b"\xDA" + encode_calls_address(code_cave)
    data[ftomn_address] = 0x00

    defaults = default_configuration()
    normal_ignition_raw = encode_ignition_ms(DEFAULT_IGNITION_CUT_MS)
    write_configuration(data, config_address, defaults, normal_ignition_raw)
    write_metadata(
        data,
        config_address=config_address,
        code_cave_address=code_cave,
        hook_address=hook_address,
        ftomn_address=ftomn_address,
        original_ftomn=original_ftomn,
        original_hook_bytes=original_hook,
        normal_ignition_raw=normal_ignition_raw,
    )

    installation = detect_installation(bytes(data))
    if installation is None:
        raise LaunchError("Installation verification failed.")

    compatible = find_compatible_setzi_installation(
        bytes(data),
        memory_layout_kb,
    )
    if compatible is None:
        raise LaunchError(
            "Installation is valid for this utility but was not detected by "
            "the compatible Setzi scanner."
        )

    ok(f"Launch Control code installed ({len(code)} bytes)")
    ok("Compatible Setzi detection verified")
    ok("Default cut mode set to Hard Cut (FTOMN 0x00)")
    ok("Original FTOMN and hook bytes stored for restoration")
    ok("Installation verified")
    return installation


def remove_installation(data: bytearray, installation: Installation) -> None:
    info("Removing Launch Control.")

    data[
        installation.hook_address : installation.hook_address + 4
    ] = installation.original_hook_bytes
    data[installation.ftomn_address] = installation.original_ftomn

    code_end = min(installation.code_cave_address + CODE_CAVE_SIZE, len(data))
    data[installation.code_cave_address : code_end] = b"\xFF" * (
        code_end - installation.code_cave_address
    )

    config_end = min(installation.config_address + CONFIG_AREA_SIZE, len(data))
    data[installation.config_address : config_end] = b"\xFF" * (
        config_end - installation.config_address
    )

    ok(f"Original hook restored at 0x{installation.hook_address:X}")
    ok(
        f"Original FTOMN restored to 0x{installation.original_ftomn:02X} "
        f"at 0x{installation.ftomn_address:X}"
    )
    ok("Code cave and configuration area cleared")


def verify_before_save(original: bytes, modified: bytes) -> None:
    if len(original) != len(modified):
        raise LaunchError(
            f"File size changed from {len(original)} to {len(modified)} bytes."
        )
    ok("File size unchanged")


def save_output(input_path: Path, original: bytes, modified: bytearray, suffix: str) -> Path:
    verify_before_save(original, bytes(modified))
    target = output_name(input_path, suffix)
    try:
        target.write_bytes(modified)
    except OSError as exc:
        raise LaunchError(f"Cannot write output file: {exc}") from exc

    reread = target.read_bytes()
    if reread != bytes(modified):
        raise LaunchError("Output verification failed after writing the file.")

    ok("Output file verified")
    ok(f"Saved: {target}")
    return target


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="launch.exe",
        description="Install and configure ME7 Launch Control.",
        epilog=(
            "Examples:\n"
            "  launch.exe test.bin test.ecu\n"
            "  launch.exe --remove test_mod.bin test.ecu"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--remove", action="store_true", help="Remove this utility's LC patch.")
    parser.add_argument(
        "--nls-counter",
        default=f"0x{DEFAULT_NLS_COUNTER:X}",
        help=f"NLS counter RAM address (default: 0x{DEFAULT_NLS_COUNTER:X}).",
    )
    parser.add_argument("bin_file", type=Path, help="Input ECU BIN file.")
    parser.add_argument("ecu_file", type=Path, help="ME7Info ECU definition file.")
    return parser.parse_args()



# ============================================================================
# MultiMap selective-bank Launch Control extension
# ============================================================================

MULTIMAP_MARKER = b"ME7MBLC1"
MULTIMAP_CONFIG_SEARCH_START = 0x17000
MULTIMAP_CONFIG_SEARCH_END = 0x18000
MULTIMAP_META_SIZE = 64
MULTIMAP_BANK_CONFIG_SIZE = 16
MULTIMAP_DISPATCHER_SLOT_SIZE = 128
MULTIMAP_FUNCTION_SLOT_SIZE = CODE_CAVE_SIZE


@dataclass
class MultiMapInfo:
    bank_count: int
    runtime_ram: int
    active_bank_ram: int
    bank_ids: tuple[int, ...]
    csv_path: Path


def parse_multimap_csv(path: Path) -> MultiMapInfo:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise LaunchError(f"Cannot read MultiMap CSV log: {exc}") from exc

    bank_match = re.search(r"Number of banks\s*:\s*(\d+)", text, re.IGNORECASE)
    ram_match = re.search(r"Runtime RAM\s*:\s*0x([0-9A-Fa-f]+)", text, re.IGNORECASE)
    if not bank_match:
        raise LaunchError("MultiMap CSV: 'Number of banks' was not found.")
    if not ram_match:
        raise LaunchError("MultiMap CSV: 'Runtime RAM' was not found.")

    bank_count = int(bank_match.group(1))
    runtime_ram = int(ram_match.group(1), 16)
    if not 2 <= bank_count <= 4:
        raise LaunchError(f"Unsupported MultiMap bank count: {bank_count}. Expected 2-4.")
    if not (0x380000 <= runtime_ram <= 0x38FFFF):
        raise LaunchError(
            f"MultiMap Runtime RAM 0x{runtime_ram:06X} is outside expected 0x38xxxx RAM."
        )

    runtime_installed = re.search(r"Runtime installed\s*:\s*YES", text, re.IGNORECASE)
    if not runtime_installed:
        raise LaunchError(
            "MultiMap CSV does not confirm 'Runtime installed : YES'. "
            "Refusing to patch a BIN that may not contain the expected MultiMap runtime."
        )

    bank_ids = sorted({
        int(x) for x in re.findall(
            r"^\s*Bank\s+(\d+)\s+(?:ORIGINAL\s*/\s*STOCK|0x[0-9A-Fa-f]+-0x[0-9A-Fa-f]+)",
            text, re.IGNORECASE | re.MULTILINE
        )
    })
    expected_bank_ids = list(range(1, bank_count + 1))
    if bank_ids != expected_bank_ids:
        raise LaunchError(
            "MultiMap CSV bank layout is inconsistent: "
            f"header reports {bank_count} banks, layout contains {bank_ids or 'none'}."
        )

    # MultiMap runtime layout: current bank WORD is the first word at RAM + 0.
    active_bank_ram = runtime_ram
    return MultiMapInfo(
        bank_count=bank_count,
        runtime_ram=runtime_ram,
        active_bank_ram=active_bank_ram,
        bank_ids=tuple(bank_ids),
        csv_path=path,
    )


def write_bank_configuration(data: bytearray, address: int, cfg: Configuration) -> None:
    ignition_raw = encode_ignition_ms(cfg.ignition_cut_ms)
    struct.pack_into(
        "<HHHHB",
        data,
        address,
        encode_speed(cfg.speed_kmh),
        encode_rpm(cfg.launch_rpm),
        ignition_raw,
        encode_rpm(cfg.rpm_threshold),
        encode_pedal(cfg.accelerator_percent),
    )
    data[address + 9] = encode_temperature(cfg.minimum_temperature)
    data[address + 10 : address + MULTIMAP_BANK_CONFIG_SIZE] = b"\xFF" * (
        MULTIMAP_BANK_CONFIG_SIZE - 10
    )


def _jmpr_rel8(jump_offset: int, target_offset: int) -> int:
    delta = target_offset - (jump_offset + 2)
    if delta % 2:
        raise LaunchError("Internal dispatcher jump is not word aligned.")
    rel = delta // 2
    if not -128 <= rel <= 127:
        raise LaunchError("Internal dispatcher jump is out of JMPR range.")
    return rel & 0xFF


def build_multimap_dispatcher(
    *,
    active_bank_ram: int,
    bank_targets: Dict[int, int],
    displaced_hook_instruction: Optional[bytes] = None,
    chained_hook_target: Optional[int] = None,
) -> bytes:
    """
    Dispatch LC only for enabled MultiMap banks.

    Two safe modes are supported:
    - stock hook: replay the complete displaced 4-byte stock instruction on
      inactive banks; active-bank LC functions replay it themselves.
    - chained hook: first CALL the pre-existing hook target (normally the
      MultiMap runtime), then optionally CALL the selected LC function. In this
      mode LC functions must NOT replay the displaced stock instruction.
    """
    code = bytearray()

    if chained_hook_target is not None:
        # Preserve the existing runtime hook exactly once before LC selection.
        append(code, b"\xDA" + encode_calls_address(chained_hook_target))

    # Make bank selection transparent to the code at the original hook.
    # The MultiMap runtime has already restored its own R4/PSW state, so save
    # that post-runtime state before using R4 and condition flags here.
    append(code, 0xEC, 0x88)  # PUSH PSW
    append(code, 0xEC, 0xF4)  # PUSH R4

    # XRAM 0x38xxxx cannot be addressed with a plain 16-bit RAM operand.
    # C167 EXTP page = absolute >> 14, operand = absolute & 0x3FFF.
    # For 0x3847B0 this intentionally becomes EXTP 0xE1 / MOV ...,0x07B0.
    page = active_bank_ram >> 14
    offset = active_bank_ram & 0x3FFF
    if not (0 <= page <= 0x3FF):
        raise LaunchError(f"Active-bank RAM page is out of EXTP range: 0x{active_bank_ram:X}")
    append(code, 0xD7, 0x40, word_little(page))
    append(code, 0xF2, 0xF4, word_little(offset))

    jump_patch_positions: List[tuple[int, int]] = []
    for bank in sorted(bank_targets):
        append(code, 0x46, 0xF4, word_little(bank))  # CMP R4, #bank
        jump_pos = len(code)
        append(code, 0x2D, 0x00)  # JMPR cc_EQ, bank_handler
        jump_patch_positions.append((jump_pos, bank))

    # Inactive bank: restore the exact state present after the preserved
    # MultiMap hook, then return without executing LC.
    append(code, 0xFC, 0xF4)  # POP R4
    append(code, 0xFC, 0x88)  # POP PSW
    if chained_hook_target is not None:
        append(code, 0xDB, 0x00)
    else:
        if displaced_hook_instruction is None or len(displaced_hook_instruction) != 4:
            raise LaunchError("Internal error: complete 4-byte displaced hook instruction is required.")
        append(code, displaced_hook_instruction, 0xDB, 0x00)

    label_offsets: Dict[int, int] = {}
    for bank in sorted(bank_targets):
        label_offsets[bank] = len(code)
        # Remove dispatcher-only side effects before entering the original
        # Setzi-derived LC function.
        append(code, 0xFC, 0xF4)  # POP R4
        append(code, 0xFC, 0x88)  # POP PSW
        append(code, b"\xDA" + encode_calls_address(bank_targets[bank]))
        append(code, 0xDB, 0x00)

    for jump_pos, bank in jump_patch_positions:
        code[jump_pos + 1] = _jmpr_rel8(jump_pos, label_offsets[bank])

    if len(code) > MULTIMAP_DISPATCHER_SLOT_SIZE:
        raise LaunchError(
            f"Generated bank dispatcher is too large ({len(code)} bytes)."
        )
    return bytes(code)


def _find_and_reserve_hole(
    scratch: bytearray,
    size: int,
    *,
    start: int,
    end: Optional[int] = None,
    alignment: int = 16,
) -> int:
    pos = find_hole(scratch, size, start=start, end=end, alignment=alignment)
    if pos is None:
        raise LaunchError(
            f"Cannot find a safe free area of {size} bytes in 0x{start:X}-"
            f"0x{(end if end is not None else len(scratch)):X}."
        )
    scratch[pos : pos + size] = b"\x00" * size
    return pos


def install_multimap_launch(
    data: bytearray,
    entries: Dict[str, EcuEntry],
    multimap: MultiMapInfo,
    bank_configs: Dict[int, Configuration],
    nls_counter_address: int = DEFAULT_NLS_COUNTER,
) -> dict:
    if not bank_configs:
        raise LaunchError("Select at least one bank for Launch Control.")
    if any(bank < 1 or bank > multimap.bank_count for bank in bank_configs):
        raise LaunchError("Selected bank is outside the bank count reported by MultiMap CSV.")

    memory_layout_kb = len(data) // 1024
    if memory_layout_kb != 1024:
        raise LaunchError("MultiMap selective-bank LC currently requires a 1 MB ME7.5 BIN.")

    # Avoid stacking another LC implementation on top of an existing Setzi patch.
    if detect_installation(bytes(data)) is not None:
        raise LaunchError("A native ME7LC001 Launch Control patch is already present in this BIN.")
    if find_compatible_setzi_installation(bytes(data), memory_layout_kb) is not None:
        raise LaunchError(
            "A compatible Setzi Launch Control patch is already present. Use a MultiMap BIN without LC."
        )
    if data.find(MULTIMAP_MARKER) >= 0:
        raise LaunchError("This BIN already contains the MultiMap selective-bank LC marker.")

    tsrldyn = require_address(entries, "tsrldyn")
    vfil_w = require_address(entries, "vfil_w")
    nmot_w = require_address(entries, "nmot_w")
    wped = resolve_wped(entries)
    clutch_address, clutch_bit = require_bit(entries, "b_kuppl")
    # Selective MultiMap LC is clutch-only. No brake variable is required.
    brake_address, brake_bit = clutch_address, clutch_bit

    used_by = is_ecu_address_used(entries, nls_counter_address)
    if used_by:
        raise LaunchError(
            f"NLS counter address 0x{nls_counter_address:06X} is used by '{used_by}'."
        )

    ftomn_candidates = find_ftomn(bytes(data), memory_layout_kb)
    if not ftomn_candidates:
        raise LaunchError("FTOMN could not be found safely.")
    ftomn_address = ftomn_candidates[0]
    original_ftomn = data[ftomn_address]

    hook_address = find_hook(bytes(data), memory_layout_kb)
    original_hook = bytes(data[hook_address : hook_address + 4])
    if len(original_hook) != 4:
        raise LaunchError("Cannot read the complete Launch Control hook bytes.")

    chained_hook_target: Optional[int] = None
    if original_hook[0] == 0xDA:
        chained_hook_target = decode_calls_target(data, hook_address)
        if chained_hook_target is None or not (0 <= chained_hook_target < len(data)):
            raise LaunchError("Existing CALLS hook target could not be decoded safely.")
        info(
            f"Existing CALLS hook detected at 0x{hook_address:X}; "
            f"preserving target 0x{chained_hook_target:X} in a chained dispatcher."
        )
    else:
        # The stock Setzi hook is the 4-byte F3 F8 xx xx instruction.
        if original_hook[0:2] != b"\xF3\xF8":
            raise LaunchError(
                "Unexpected stock hook instruction. Expected F3 F8 xx xx or an existing CALLS hook."
            )

    scratch = bytearray(data)

    config_total = MULTIMAP_META_SIZE + multimap.bank_count * MULTIMAP_BANK_CONFIG_SIZE
    config_base = _find_and_reserve_hole(
        scratch,
        config_total,
        start=MULTIMAP_CONFIG_SEARCH_START,
        end=min(MULTIMAP_CONFIG_SEARCH_END, len(scratch)),
        alignment=16,
    )

    dispatcher_address = _find_and_reserve_hole(
        scratch,
        MULTIMAP_DISPATCHER_SLOT_SIZE,
        start=SETZI_COMPATIBLE_CODE_START_1MB,
        end=len(scratch),
        alignment=16,
    )

    function_addresses: Dict[int, int] = {}
    for bank in sorted(bank_configs):
        function_addresses[bank] = _find_and_reserve_hole(
            scratch,
            MULTIMAP_FUNCTION_SLOT_SIZE,
            start=SETZI_COMPATIBLE_CODE_START_1MB,
            end=len(scratch),
            alignment=16,
        )

    bank_config_addresses: Dict[int, int] = {}
    for bank in range(1, multimap.bank_count + 1):
        bank_config_addresses[bank] = (
            config_base + MULTIMAP_META_SIZE + (bank - 1) * MULTIMAP_BANK_CONFIG_SIZE
        )

    for bank, cfg in bank_configs.items():
        write_bank_configuration(data, bank_config_addresses[bank], cfg)

        function = build_launch_code(
            config_address=bank_config_addresses[bank],
            tsrldyn=tsrldyn,
            vfil_w=vfil_w,
            nmot_w=nmot_w,
            wped=wped,
            clutch_address=clutch_address,
            clutch_bit=clutch_bit,
            brake_address=brake_address,
            brake_bit=brake_bit,
            nls_counter_address=nls_counter_address,
            original_hook_bytes_2_3=(original_hook[2:4] if chained_hook_target is None else b"\x00\x00"),
            replay_displaced_instruction=(chained_hook_target is None),
            clutch_only=True,
        )
        function_address = function_addresses[bank]
        data[function_address : function_address + len(function)] = function
        data[
            function_address + len(function) : function_address + MULTIMAP_FUNCTION_SLOT_SIZE
        ] = b"\xFF" * (MULTIMAP_FUNCTION_SLOT_SIZE - len(function))

    dispatcher = build_multimap_dispatcher(
        active_bank_ram=multimap.active_bank_ram,
        bank_targets=function_addresses,
        displaced_hook_instruction=(original_hook if chained_hook_target is None else None),
        chained_hook_target=chained_hook_target,
    )
    data[dispatcher_address : dispatcher_address + len(dispatcher)] = dispatcher
    data[
        dispatcher_address + len(dispatcher) : dispatcher_address + MULTIMAP_DISPATCHER_SLOT_SIZE
    ] = b"\xFF" * (MULTIMAP_DISPATCHER_SLOT_SIZE - len(dispatcher))

    # Hook once. Dispatcher decides whether LC executes for the current map bank.
    data[hook_address : hook_address + 4] = b"\xDA" + encode_calls_address(dispatcher_address)
    data[ftomn_address] = FTOMN_HARD_CUT

    # Compact metadata for diagnostics/reproducibility (not used by ECU runtime).
    meta = bytearray(b"\xFF" * MULTIMAP_META_SIZE)
    meta[0 : len(MULTIMAP_MARKER)] = MULTIMAP_MARKER
    meta[8] = 2  # metadata version (v3 dispatcher / clutch-only LC)
    meta[9] = multimap.bank_count
    selected_mask = 0
    for bank in bank_configs:
        selected_mask |= 1 << (bank - 1)
    meta[10] = selected_mask
    meta[11] = original_ftomn
    struct.pack_into("<I", meta, 12, multimap.active_bank_ram)
    struct.pack_into("<I", meta, 16, dispatcher_address)
    struct.pack_into("<I", meta, 20, hook_address)
    struct.pack_into("<I", meta, 24, ftomn_address)
    meta[28:32] = original_hook
    for idx, bank in enumerate(range(1, multimap.bank_count + 1)):
        struct.pack_into("<I", meta, 32 + idx * 4, function_addresses.get(bank, 0))
    data[config_base : config_base + MULTIMAP_META_SIZE] = meta

    # Basic byte-level verification.
    if data[hook_address] != 0xDA:
        raise LaunchError("Final hook verification failed.")
    if decode_calls_target(data, hook_address) != dispatcher_address:
        raise LaunchError("Dispatcher CALLS target verification failed.")
    if data[ftomn_address] != FTOMN_HARD_CUT:
        raise LaunchError("FTOMN Hard Cut verification failed.")

    return {
        "config_base": config_base,
        "dispatcher_address": dispatcher_address,
        "function_addresses": function_addresses,
        "hook_address": hook_address,
        "ftomn_address": ftomn_address,
        "active_bank_ram": multimap.active_bank_ram,
        "selected_banks": sorted(bank_configs),
        "bank_config_addresses": bank_config_addresses,
        "chained_hook_target": chained_hook_target,
    }


def save_multimap_output(input_path: Path, original: bytes, modified: bytearray) -> Path:
    verify_before_save(original, bytes(modified))
    return save_output(input_path, original, modified, "_LC_MULTIMAP")



def parse_multimap_cli_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Launch_MultiMap_ME7Web_Pipeline.py",
        description=(
            "ME7Web pipeline: ME7Check -> ME7Info ECU generation -> selective MultiMap LC "
            "-> XDF -> ME7Sum CSOK. Input requires only BIN + MultiMap CSV."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ME7Web example:\n"
            "  python3 Launch_MultiMap_ME7Web_Pipeline.py dump.bin multimap_log.csv "
            "--bank 2:3500:3:5500:70:200:75 "
            "--bank 3:4000:3:5500:70:200:75 "
            "--bank 4:4500:3:5500:70:200:75 "
            "--tools-dir /opt/me7web/tools --yes --non-interactive\n"
        ),
    )
    parser.add_argument("bin_file", type=Path, help="1 MB ME7.5 MultiMap BIN")
    parser.add_argument("csv_file", type=Path, help="MultiMap builder/log .csv")
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=Path("."),
        help="Directory containing ME7Check.exe, ME7Info.exe and me7sum.exe (default: current directory)",
    )
    parser.add_argument("--wine", default="wine", help="Wine executable/command (default: wine)")
    parser.add_argument("--me7check", default="ME7Check.exe", help="ME7Check executable name")
    parser.add_argument("--me7info", default="ME7Info.exe", help="ME7Info executable name")
    parser.add_argument("--me7sum", default="me7sum.exe", help="ME7Sum executable name")
    parser.add_argument(
        "--ecus-dir",
        type=Path,
        default=Path("ecus"),
        help="Directory where ME7Info-created ECU files are expected (default: ecus)",
    )
    parser.add_argument(
        "--keep-ecu-in-ecus",
        action="store_true",
        help="Copy rather than move generated .ecu from ecus to BIN directory.",
    )
    parser.add_argument(
        "--nls-counter",
        default=f"0x{DEFAULT_NLS_COUNTER:X}",
        help=f"NLS counter RAM address (default: 0x{DEFAULT_NLS_COUNTER:X})",
    )
    parser.add_argument(
        "--bank",
        action="append",
        default=[],
        metavar="BANK:RPM:SPEED:RPMTHR:PEDAL:CUT:TEMP",
        help=(
            "Per-bank LC configuration. Repeat for each LC bank. "
            "Example: --bank 2:3500:3:5500:70:200:75"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="Apply patch without confirmation.")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable all prompts; requires --bank and --yes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Patched BIN before checksum. Default: *_LC_MULTIMAP.bin",
    )
    parser.add_argument(
        "--xdf-output",
        type=Path,
        help="Output XDF. Default: same basename as patched BIN with .xdf",
    )
    parser.add_argument(
        "--csok-output",
        type=Path,
        help="Final checksum-corrected BIN. Default: patched BIN basename + _CSOK.bin",
    )
    return parser.parse_args()


def _resolve_tool(tools_dir: Path, executable: str) -> Path:
    candidate = Path(executable)
    if candidate.is_absolute():
        path = candidate
    else:
        path = tools_dir / candidate
    if not path.is_file():
        raise LaunchError(f"Required tool not found: {path}")
    return path.resolve()


def _run_wine_tool(wine: str, exe: Path, args: List[str], *, cwd: Path, stage: str, strict: bool = True) -> str:
    cmd = [wine, str(exe), *args]
    print()
    print("=" * 72)
    print(stage.center(72))
    print("=" * 72)
    print("Command: " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise LaunchError(f"Cannot start {stage}: {exc}") from exc
    output = proc.stdout or ""
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    print(f"[{stage}] EXIT_CODE={proc.returncode}")
    if proc.returncode != 0:
        if strict:
            raise LaunchError(f"{stage} failed with exit code {proc.returncode}.")
        warn(f"{stage} returned exit code {proc.returncode}; output is kept for ME7Web diagnostics.")
    return output


def _generate_ecu_with_me7info(
    *, wine: str, me7info: Path, bin_file: Path, ecus_dir_arg: Path, keep_source: bool
) -> Path:
    workdir = bin_file.parent.resolve()
    stem = bin_file.stem
    final_ecu = workdir / f"{stem}.ecu"

    # Remove a stale same-name ECU so success cannot be falsely inferred.
    if final_ecu.exists():
        final_ecu.unlink()

    if ecus_dir_arg.is_absolute():
        ecus_dir = ecus_dir_arg
    else:
        ecus_dir = workdir / ecus_dir_arg
    ecus_dir.mkdir(parents=True, exist_ok=True)
    source_ecu = ecus_dir / f"{stem}.ecu"
    if source_ecu.exists():
        source_ecu.unlink()

    # ME7Info's standard -n mode generates the ECU definition for the BIN.
    _run_wine_tool(
        wine,
        me7info,
        ["-n", str(bin_file.resolve())],
        cwd=workdir,
        stage="ME7INFO / ECU GENERATION",
    )

    # Different ME7Info builds/wrappers can place the .ecu either in ./ecus
    # or beside the BIN. Prefer ./ecus because that is the ME7Web layout.
    candidates = [source_ecu, final_ecu]
    found = next((x for x in candidates if x.is_file() and x.stat().st_size > 0), None)
    if found is None:
        raise LaunchError(
            f"ME7Info finished but no generated ECU was found at {source_ecu} or {final_ecu}."
        )

    if found.resolve() != final_ecu.resolve():
        if keep_source:
            shutil.copy2(found, final_ecu)
            ok(f"Generated ECU copied: {found} -> {final_ecu}")
        else:
            shutil.move(str(found), str(final_ecu))
            ok(f"Generated ECU moved: {found} -> {final_ecu}")
    else:
        ok(f"Generated ECU: {final_ecu}")

    # Parse immediately: a non-empty but invalid ECU should stop the pipeline.
    parse_ecu_file(final_ecu)
    ok("Generated ECU parsed successfully")
    return final_ecu


def _default_csok_path(patched: Path) -> Path:
    return patched.with_name(f"{patched.stem}_CSOK{patched.suffix or '.bin'}")


def _run_me7sum(
    *, wine: str, me7sum: Path, patched_bin: Path, csok_bin: Path
) -> Path:
    csok_bin.parent.mkdir(parents=True, exist_ok=True)
    if csok_bin.exists():
        csok_bin.unlink()

    # The Windows pgetopt implementation used by ME7Sum treats both '-' and
    # '/' as option prefixes. Passing an absolute Linux path such as /home/...
    # through Wine is therefore parsed as an unknown /h option. Run ME7Sum
    # from the patched BIN directory and pass relative paths instead.
    workdir = patched_bin.parent.resolve()
    input_arg = os.path.relpath(patched_bin.resolve(), workdir)
    output_arg = os.path.relpath(csok_bin.resolve(), workdir)

    _run_wine_tool(
        wine,
        me7sum,
        [input_arg, output_arg],
        cwd=workdir,
        stage="ME7SUM / CHECKSUM CORRECTION",
    )
    if not csok_bin.is_file() or csok_bin.stat().st_size == 0:
        raise LaunchError(f"ME7Sum completed but CSOK output was not created: {csok_bin}")
    if csok_bin.stat().st_size != patched_bin.stat().st_size:
        raise LaunchError(
            f"CSOK BIN size mismatch: {csok_bin.stat().st_size} vs {patched_bin.stat().st_size}."
        )
    ok(f"Checksum-corrected BIN verified: {csok_bin}")
    return csok_bin

def parse_bank_cli_spec(spec: str, bank_count: int) -> tuple[int, Configuration]:
    parts = spec.split(":")
    if len(parts) != 7:
        raise LaunchError(
            "Invalid --bank format. Expected "
            "BANK:RPM:SPEED:RPMTHR:PEDAL:CUT:TEMP, e.g. 2:3500:3:5500:70:200:75"
        )
    try:
        bank = int(parts[0])
        launch_rpm = int(parts[1])
        speed = float(parts[2].replace(",", "."))
        rpm_threshold = int(parts[3])
        accelerator = float(parts[4].replace(",", "."))
        ignition_ms = int(parts[5])
        minimum_temperature = int(parts[6])
    except ValueError as exc:
        raise LaunchError(f"Invalid numeric value in --bank '{spec}'.") from exc

    if not 1 <= bank <= bank_count:
        raise LaunchError(f"Bank {bank} is outside available range 1-{bank_count}.")
    if not 500 <= launch_rpm <= 16000:
        raise LaunchError(f"Bank {bank}: Launch RPM must be 500-16000.")
    if not 0.0 <= speed <= 511.0:
        raise LaunchError(f"Bank {bank}: Speed Threshold must be 0-511 km/h.")
    if not 500 <= rpm_threshold <= 16000:
        raise LaunchError(f"Bank {bank}: RPM Threshold must be 500-16000.")
    if not 0.0 <= accelerator <= 100.0:
        raise LaunchError(f"Bank {bank}: Accelerator Threshold must be 0-100%. ")
    if not 20 <= ignition_ms <= 5000 or ignition_ms % 20:
        raise LaunchError(f"Bank {bank}: Ignition Cut must be 20-5000 ms and divisible by 20.")
    if not 0 <= minimum_temperature <= 120:
        raise LaunchError(f"Bank {bank}: Minimum Temperature must be 0-120 C.")

    return bank, Configuration(
        speed_kmh=speed,
        launch_rpm=launch_rpm,
        rpm_threshold=rpm_threshold,
        accelerator_percent=accelerator,
        ignition_cut_ms=ignition_ms,
        minimum_temperature=minimum_temperature,
        trigger=TRIGGER_CLUTCH,
    )


def save_multimap_output_explicit(
    input_path: Path, original: bytes, modified: bytearray, output_path: Optional[Path]
) -> Path:
    if output_path is None:
        return save_multimap_output(input_path, original, modified)
    verify_before_save(original, bytes(modified))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(modified)
    reread = output_path.read_bytes()
    if reread != bytes(modified):
        raise LaunchError("Output verification failed after writing the file.")
    ok("Output file verified")
    ok(f"Saved: {output_path}")
    return output_path



def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _xdf_constant(
    *, uniqueid: int, title: str, description: str, category: int,
    address: int, bits: int, units: str, decimals: int, equation: str
) -> str:
    # Bosch ME7 calibration words are stored little-endian in the BIN.
    # TunerPro needs mmedtypeflags="0x02" on 16-bit XDF elements; relying
    # only on XDFHEADER DEFAULTS lsbfirst="1" can make words display byte-swapped.
    endian_flag = ' mmedtypeflags="0x02"' if bits == 16 else ''
    return (
        f'  <XDFCONSTANT uniqueid="0x{uniqueid:X}">\n'
        f'    <title>{_xml_escape(title)}</title>\n'
        f'    <description>{_xml_escape(description)}</description>\n'
        f'    <CATEGORYMEM index="0" category="{category}" />\n'
        f'    <EMBEDDEDDATA mmedaddress="0x{address:X}" mmedelementsizebits="{bits}"{endian_flag} />\n'
        f'    <units>{_xml_escape(units)}</units>\n'
        f'    <decimalpl>{decimals}</decimalpl>\n'
        f'    <datatype>0</datatype>\n'
        f'    <unittype>0</unittype>\n'
        f'    <DALINK index="0" />\n'
        f'    <MATH equation="{_xml_escape(equation)}"><VAR id="X" /></MATH>\n'
        f'  </XDFCONSTANT>'
    )


def generate_multimap_lc_xdf(
    output_path: Path,
    bin_path: Path,
    multimap: MultiMapInfo,
    bank_configs: Dict[int, Configuration],
    bank_config_addresses: Dict[int, int],
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    selected = sorted(bank_configs)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<XDFFORMAT version="1.60">',
        '  <XDFHEADER>',
        '    <flags>0x1</flags>',
        '    <fileversion>1.0</fileversion>',
        f'    <deftitle>{_xml_escape(bin_path.stem)} - MultiMap Selective LC</deftitle>',
        f'    <description>Auto-generated LC configuration XDF. MultiMap banks: {multimap.bank_count}; active bank WORD: 0x{multimap.active_bank_ram:06X}.</description>',
        '    <author>ME7 MultiMap Selective LC Patcher</author>',
        '    <BASEOFFSET offset="0" subtract="0" />',
        '    <DEFAULTS datasizeinbits="8" sigdigits="2" outputtype="1" signed="0" lsbfirst="1" float="0" />',
        '    <REGION type="0xFFFFFFFF" startaddress="0x0" size="0x100000" regionflags="0x0" name="Binary File" desc="1 MB Bosch ME7.5 binary" />',
    ]
    for idx, bank in enumerate(selected):
        lines.append(f'    <CATEGORY index="0x{idx:X}" name="Bank {bank} - Launch Control" />')
    lines.append('  </XDFHEADER>')

    uid = 0x4C430000
    for category_index, bank in enumerate(selected, start=1):
        base = bank_config_addresses[bank]
        defs = [
            ("Speed Threshold", "Vehicle speed threshold for Launch Control", 0, 16, "km/h", 2, "X*0.0078125"),
            ("Launch RPM", "Launch Control RPM target", 2, 16, "rpm", 0, "X*0.25"),
            ("Ignition Cut Duration", "Ignition cut duration", 4, 16, "ms", 0, "X*20"),
            ("RPM Threshold", "RPM threshold used by LC/NLS logic", 6, 16, "rpm", 0, "X*0.25"),
            ("Accelerator Threshold", "Accelerator pedal threshold", 8, 8, "%", 1, "X*0.392157"),
            ("Minimum Temperature", "Minimum coolant temperature", 9, 8, "deg C", 0, "X*0.75-48"),
        ]
        for name, desc, off, bits, units, decimals, eq in defs:
            uid += 1
            lines.append(_xdf_constant(
                uniqueid=uid,
                title=f"Bank {bank} - {name}",
                description=desc,
                category=category_index,
                address=base + off,
                bits=bits,
                units=units,
                decimals=decimals,
                equation=eq,
            ))
    lines.append('</XDFFORMAT>')
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"XDF generated: {output_path}")
    return output_path


def multimap_cmd_banner() -> None:
    line = "=" * 72
    print(line)
    print("ME7.5 MULTIMAP SELECTIVE LAUNCH CONTROL - CMD".center(72))
    print("Clutch only | Per-bank configuration | MultiMap required".center(72))
    print(line)


def prompt_bank_selection(bank_count: int) -> List[int]:
    print()
    print("AVAILABLE MULTIMAP BANKS")
    for bank in range(1, bank_count + 1):
        label = "ORI" if bank == 1 else "COPY"
        print(f"  Bank {bank}: {label}")
    print()
    print("Enter banks for Launch Control, e.g. 2,3,4 or ALL.")
    while True:
        raw = safe_input("LC banks: ").strip().lower()
        if raw in {"all", "a", "*"}:
            return list(range(1, bank_count + 1))
        try:
            parts = [x.strip() for x in re.split(r"[,; ]+", raw) if x.strip()]
            banks = sorted(set(int(x) for x in parts))
        except ValueError:
            print("  Invalid selection. Example: 2,3,4")
            continue
        if not banks:
            print("  Select at least one bank.")
            continue
        if any(bank < 1 or bank > bank_count for bank in banks):
            print(f"  Banks must be between 1 and {bank_count}.")
            continue
        return banks


def configure_multimap_bank(bank: int) -> Configuration:
    print()
    print("-" * 72)
    print(f"BANK {bank} LAUNCH CONTROL CONFIGURATION")
    print("-" * 72)
    launch_rpm = prompt_int("Launch RPM", DEFAULT_LAUNCH_RPM, 500, 16000)
    speed = prompt_float("Speed Threshold (km/h)", DEFAULT_SPEED_KMH, 0.0, 511.0)
    rpm_threshold = prompt_int("RPM Threshold", DEFAULT_RPM_THRESHOLD, 500, 16000)
    accelerator = prompt_float(
        "Accelerator Threshold (%)", DEFAULT_ACCELERATOR_PERCENT, 0.0, 100.0
    )
    ignition_ms = prompt_int(
        "Ignition Cut Duration (ms)",
        DEFAULT_IGNITION_CUT_MS,
        20,
        5000,
        multiple=20,
    )
    minimum_temperature = prompt_int("Minimum Temperature (C)", 75, 0, 120)
    return Configuration(
        speed_kmh=speed,
        launch_rpm=launch_rpm,
        rpm_threshold=rpm_threshold,
        accelerator_percent=accelerator,
        ignition_cut_ms=ignition_ms,
        minimum_temperature=minimum_temperature,
        trigger=TRIGGER_CLUTCH,
    )


def print_multimap_summary(
    multimap: MultiMapInfo, bank_configs: Dict[int, Configuration]
) -> None:
    print()
    print("=" * 72)
    print("PATCH SUMMARY".center(72))
    print("=" * 72)
    print(f"MultiMap banks           : {multimap.bank_count}")
    print(f"Runtime RAM              : 0x{multimap.runtime_ram:06X}")
    print(f"Active bank WORD         : 0x{multimap.active_bank_ram:06X}")
    print(f"Selected LC banks        : {', '.join(map(str, sorted(bank_configs)))}")
    print("Activation trigger       : Clutch only")
    print("Cut mode                 : Hard Cut (FTOMN 0x00)")
    print()
    for bank in sorted(bank_configs):
        cfg = bank_configs[bank]
        print(
            f"Bank {bank}: LC {cfg.launch_rpm} rpm | speed {cfg.speed_kmh:g} km/h | "
            f"RPM threshold {cfg.rpm_threshold} | pedal {cfg.accelerator_percent:g}% | "
            f"ignition cut {cfg.ignition_cut_ms} ms | temp {cfg.minimum_temperature} C"
        )
    print()


def multimap_cmd_main() -> int:
    args = parse_multimap_cli_arguments()
    multimap_cmd_banner()

    if not args.bin_file.is_file():
        raise LaunchError(f"BIN file not found: {args.bin_file}")
    if not args.csv_file.is_file():
        raise LaunchError(f"CSV file not found: {args.csv_file}")

    bin_file = args.bin_file.resolve()
    csv_file = args.csv_file.resolve()
    tools_dir = args.tools_dir.resolve()
    me7check = _resolve_tool(tools_dir, args.me7check)
    me7info = _resolve_tool(tools_dir, args.me7info)
    me7sum = _resolve_tool(tools_dir, args.me7sum)

    original = bin_file.read_bytes()
    if len(original) != 1024 * 1024:
        raise LaunchError(
            f"Expected a 1 MB ME7.5 MultiMap BIN, received {len(original) / 1024:.0f} KB."
        )

    # Stage 1: always expose ME7Check output first. ME7Web can stream/parse stdout
    # and display ECU identification before the patching stages continue.
    _run_wine_tool(
        args.wine,
        me7check,
        [str(bin_file)],
        cwd=bin_file.parent,
        stage="ME7CHECK / INPUT BIN IDENTIFICATION",
        strict=False,
    )

    # Stage 2: create same-name ECU automatically and place it beside the BIN.
    ecu_file = _generate_ecu_with_me7info(
        wine=args.wine,
        me7info=me7info,
        bin_file=bin_file,
        ecus_dir_arg=args.ecus_dir,
        keep_source=args.keep_ecu_in_ecus,
    )

    # Stage 3: validate MultiMap CSV and generated ECU before touching the BIN.
    multimap = parse_multimap_csv(csv_file)
    entries = parse_ecu_file(ecu_file)

    print()
    print(f"BIN : {bin_file}")
    print(f"ECU : {ecu_file} (auto-generated)")
    print(f"CSV : {csv_file}")
    ok("BIN loaded (1024 KB)")
    ok("Generated ECU file parsed")
    ok(
        f"MultiMap detected: {multimap.bank_count} banks, "
        f"active bank WORD @ 0x{multimap.active_bank_ram:06X}"
    )
    ok(f"CSV bank layout verified: {', '.join(map(str, multimap.bank_ids))}")

    bank_configs: Dict[int, Configuration] = {}
    if args.bank:
        for spec in args.bank:
            bank, cfg = parse_bank_cli_spec(spec, multimap.bank_count)
            if bank in bank_configs:
                raise LaunchError(f"Bank {bank} was specified more than once.")
            bank_configs[bank] = cfg
    else:
        if args.non_interactive:
            raise LaunchError("--non-interactive requires at least one --bank argument.")
        selected_banks = prompt_bank_selection(multimap.bank_count)
        for bank in selected_banks:
            bank_configs[bank] = configure_multimap_bank(bank)

    print_multimap_summary(multimap, bank_configs)
    if not args.yes:
        if args.non_interactive:
            raise LaunchError("--non-interactive also requires --yes.")
        if not prompt_yes_no("Apply this patch?", True):
            raise LaunchError("Operation cancelled by the user.")

    # Stage 4: patch LC and produce XDF.
    data = bytearray(original)
    result = install_multimap_launch(
        data,
        entries,
        multimap,
        bank_configs,
        parse_int(args.nls_counter),
    )
    patched_target = save_multimap_output_explicit(bin_file, original, data, args.output)
    xdf_target = args.xdf_output if args.xdf_output is not None else patched_target.with_suffix(".xdf")
    xdf_target = generate_multimap_lc_xdf(
        xdf_target, patched_target, multimap, bank_configs, result["bank_config_addresses"]
    )

    # Stage 5: checksum correction. The final artifact for flashing is *_CSOK.bin.
    csok_target = args.csok_output if args.csok_output is not None else _default_csok_path(patched_target)
    csok_target = _run_me7sum(
        wine=args.wine,
        me7sum=me7sum,
        patched_bin=patched_target,
        csok_bin=csok_target,
    )

    print()
    print("=" * 72)
    print("PIPELINE COMPLETED".center(72))
    print("=" * 72)
    print(f"Generated ECU             : {ecu_file}")
    print(f"MultiMap banks            : {multimap.bank_count}")
    print(f"Active bank WORD          : 0x{multimap.active_bank_ram:06X}")
    print(f"Hook                      : 0x{result['hook_address']:06X}")
    if result["chained_hook_target"] is not None:
        print(f"Preserved MultiMap CALLS  : 0x{result['chained_hook_target']:06X}")
    print(f"Dispatcher                : 0x{result['dispatcher_address']:06X}")
    for bank, address in sorted(result["function_addresses"].items()):
        print(f"Bank {bank} LC function         : 0x{address:06X}")
    print(f"FTOMN                     : 0x{result['ftomn_address']:06X} -> 0x00")
    print(f"Patched BIN               : {patched_target}")
    print(f"Generated XDF             : {xdf_target}")
    print(f"FINAL CSOK BIN            : {csok_target}")
    print()
    ok("ME7Web pipeline finished successfully")
    return 0



def _parse_me7check_summary(output: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    patterns = {
        "Bootrom": r"Bootrom Version\s*=\s*(.+)",
        "EPK": r"EPK\s*=\s*(.+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, output)
        if m:
            result[key] = m.group(1).strip()

    labels = {
        "SSECUHN": "Bosch ECU",
        "SSECUSN": "Bosch SW",
        "VAG part number": "VAG Part",
        "VAG sw number": "VAG SW",
        "engine id": "Engine",
    }
    for raw_label, pretty in labels.items():
        m = re.search(r"-\s*'([^']*)'\s*\(" + re.escape(raw_label) + r"\)", output, re.I)
        if m:
            result[pretty] = m.group(1).strip()

    m = re.search(r"Found\s+(\d+)\s+errors", output, re.I)
    if m:
        count = int(m.group(1))
        result["Checksums"] = "OK" if count == 0 else f"{count} error(s)"
    elif "checksum failure" in output.lower():
        result["Checksums"] = "Errors detected"
    elif output.strip():
        result["Checksums"] = "No checksum errors reported"
    return result


# Qt Designer form, embedded for standalone use.
GITHUB_URL = 'https://github.com/LDRXN'
MAPSTYLE_UI = '<?xml version="1.0" encoding="UTF-8"?>\n<ui version="4.0">\n <class>LaunchMainWindow</class>\n <widget class="QMainWindow" name="LaunchMainWindow">\n  <property name="geometry">\n   <rect>\n    <x>0</x>\n    <y>0</y>\n    <width>1040</width>\n    <height>760</height>\n   </rect>\n  </property>\n  <property name="minimumSize">\n   <size>\n    <width>900</width>\n    <height>620</height>\n   </size>\n  </property>\n  <property name="windowTitle">\n   <string>ME7.5 MultiMap Selective Launch Control</string>\n  </property>\n  <property name="styleSheet">\n   <string>\nQMainWindow, QWidget#centralWidget, QWidget#setupContent, QWidget#logContent {\n    background: #f5f7fa; color: #20242a;\n    font-family: "Segoe UI", "Inter", "DejaVu Sans", Arial;\n    font-size: 10pt;\n}\nQWidget { color: #20242a; }\nQLabel { background: transparent; border: none; }\nQLabel#titleLabel { color: #3584e4; font-size: 24pt; font-weight: 800; }\nQLabel#subtitleLabel { color: #7a818b; font-size: 8pt; font-weight: 700; }\nQLabel#ecuBadgeLabel, QLabel#sizeBadgeLabel, QLabel#cutBadgeLabel {\n    border-radius: 7px; padding: 5px 10px; font-size: 8.5pt; font-weight: 800;\n}\nQLabel#ecuBadgeLabel { color: #0969da; background: #eaf4ff; border: 1px solid #b6d8ff; }\nQLabel#sizeBadgeLabel { color: #1a7f37; background: #eaf8ee; border: 1px solid #b7e2c2; }\nQLabel#cutBadgeLabel { color: #b9382d; background: #fff0ed; border: 1px solid #efc0ba; }\nQTabWidget::pane { border: none; }\nQTabWidget::tab-bar { left: 0px; }\nQTabBar::tab {\n    background: transparent; color: #7a818b; border: none;\n    border-bottom: 2px solid transparent; padding: 9px 15px; font-weight: 700;\n    margin-right: 10px; margin-bottom: 6px;\n}\nQTabBar::tab:selected { color: #0969da; border-bottom: 2px solid #0969da; }\nQTabBar::tab:hover { color: #0969da; background: #eaf4ff; }\nQScrollArea { background: transparent; border: none; }\nQFrame#fileFrame, QFrame#infoFrame, QFrame#logFrame {\n    background: #ffffff; border: 1px solid #d9dee5; border-radius: 13px;\n}\nQFrame#lcFrame { background: #ffffff; border: 1px solid #cbdff0; border-radius: 13px; }\nQLabel[role="sectionTitle"] { color: #20242a; font-size: 13pt; font-weight: 800; }\nQLabel[role="muted"] { color: #7a818b; font-size: 8.7pt; }\nQLabel[role="fieldLabel"] { color: #59636e; font-size: 9pt; font-weight: 700; }\nQLabel[role="columnLabel"] { color: #0969da; font-size: 8.5pt; font-weight: 800; }\nQLabel#mmStatusLabel { color: #0969da; font-size: 8.6pt; font-weight: 700; }\nQLabel#selectionLabel { color: #0969da; font-size: 8.7pt; font-weight: 700; }\nQLineEdit {\n    min-height: 32px; color: #24292f; background: #f8fafc;\n    border: 1px solid #d0d7de; border-radius: 7px; padding: 0 9px;\n    selection-background-color: #54aeff;\n}\nQLineEdit:focus { background: #ffffff; border-color: #218bff; }\nQLineEdit:disabled { color: #a1a8b0; background: #f4f5f7; border-color: #e3e7ec; }\nQLineEdit[invalid="true"] { background: #fff7f5; border-color: #cf3b2e; }\nQPushButton {\n    min-height: 32px; color: #44515d; background: #ffffff;\n    border: 1px solid #cfd5dc; border-radius: 7px;\n    padding: 0 13px; font-weight: 700;\n}\nQPushButton:hover { background: #f0f6fc; border-color: #78b9ec; color: #075aab; }\nQPushButton:pressed { background: #e3effb; }\nQPushButton:focus { border-color: #218bff; }\nQPushButton#browseBinButton, QPushButton#patchButton {\n    color: #ffffff; background: #0969da; border-color: #0969da;\n}\nQPushButton#browseBinButton:hover, QPushButton#patchButton:hover { background: #0860ca; }\nQPushButton#browseBinButton:pressed, QPushButton#patchButton:pressed { background: #0757ba; }\nQPushButton#patchButton { min-height: 44px; border-radius: 9px; font-size: 10.5pt; padding: 0 22px; }\nQPushButton[role="bank"] {\n    color: #44515d; background: #f4f9fd; border-color: #c8dceb;\n    padding: 0 12px; text-align: left;\n}\nQPushButton[role="bank"]:hover { background: #eaf5ff; border-color: #78b9ec; }\nQPushButton[role="bank"]:checked { color: #ffffff; background: #0969da; border-color: #0969da; }\nQPushButton:disabled, QPushButton[role="bank"]:disabled, QPushButton#patchButton:disabled,\nQPushButton#browseBinButton:disabled {\n    color: #9da3aa; background: #e8eaed; border-color: #d8dce1;\n}\nQLabel#lcDescriptionLabel {\n    color: #59636e; background: #f6fbff; border: 1px solid #d5e7f5;\n    border-radius: 8px; padding: 8px 11px; font-size: 8.8pt;\n}\nQLabel[role="ecuLabel"] { color: #7a818b; font-size: 8.4pt; }\nQLabel[role="ecuValue"] { color: #20242a; font-size: 10pt; font-weight: 700; }\nQPlainTextEdit {\n    color: #344253; background: #f8fafc; border: 1px solid #e3e8ee;\n    border-radius: 8px; padding: 8px; font-family: "DejaVu Sans Mono", Consolas, monospace;\n    font-size: 8.8pt; selection-background-color: #b6d8ff;\n}\nQFrame#actionFrame { background: transparent; border: none; }\nQFrame#resultFrame { background: #eaf8ee; border: 1px solid #b7e2c2; border-radius: 8px; }\nQLabel#resultLabel { color: #1a7f37; font-size: 8.8pt; font-weight: 700; }\nQProgressBar { min-height: 5px; max-height: 5px; background: #dce1e6; border: none; border-radius: 2px; }\nQProgressBar::chunk { background: #218bff; border-radius: 2px; }\nQFrame#footerFrame { background: transparent; border-top: 1px solid #d8dde3; }\nQLabel#statusLabel { color: #59636e; font-size: 8.7pt; }\nQLabel#stateLabel { color: #0969da; font-size: 8.7pt; font-weight: 800; }\nQScrollBar:vertical { background: #edf0f4; width: 8px; border: none; margin: 0; }\nQScrollBar::handle:vertical { background: #c4cdd8; min-height: 25px; border-radius: 4px; }\nQScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }\nQScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }\nQToolTip { background: #ffffff; color: #24292f; border: 1px solid #c9d0d7; padding: 6px; }\n</string>\n  </property>\n  <widget class="QWidget" name="centralWidget">\n   <layout class="QVBoxLayout" name="mainLayout">\n    <property name="leftMargin">\n     <number>20</number>\n    </property>\n    <property name="topMargin">\n     <number>20</number>\n    </property>\n    <property name="rightMargin">\n     <number>20</number>\n    </property>\n    <property name="bottomMargin">\n     <number>20</number>\n    </property>\n    <property name="spacing">\n     <number>10</number>\n    </property>\n    <item alignment="Qt::AlignLeft">\n     <widget class="QWidget" name="githubLinkWidget">\n      <layout class="QHBoxLayout" name="githubLinkLayout">\n       <property name="leftMargin">\n        <number>2</number>\n       </property>\n       <property name="topMargin">\n        <number>0</number>\n       </property>\n       <property name="rightMargin">\n        <number>0</number>\n       </property>\n       <property name="bottomMargin">\n        <number>0</number>\n       </property>\n       <property name="spacing">\n        <number>5</number>\n       </property>\n       <item>\n        <widget class="QLabel" name="githubIconLabel">\n         <property name="minimumSize">\n          <size>\n           <width>18</width>\n           <height>18</height>\n          </size>\n         </property>\n         <property name="maximumSize">\n          <size>\n           <width>18</width>\n           <height>18</height>\n          </size>\n         </property>\n         <property name="scaledContents">\n          <bool>true</bool>\n         </property>\n        </widget>\n       </item>\n       <item>\n        <widget class="QLabel" name="githubLinkLabel">\n         <property name="text">\n          <string>&lt;a href="https://github.com/LDRXN" style="color:#24292f; text-decoration:none;"&gt;GitHub&lt;/a&gt;</string>\n         </property>\n         <property name="textFormat">\n          <enum>Qt::RichText</enum>\n         </property>\n         <property name="textInteractionFlags">\n          <set>Qt::TextBrowserInteraction</set>\n         </property>\n         <property name="openExternalLinks">\n          <bool>true</bool>\n         </property>\n         <property name="toolTip">\n          <string>Open LDRXN on GitHub</string>\n         </property>\n         <property name="styleSheet">\n          <string>QLabel {font-family:\'Segoe UI Variable\',\'Segoe UI\',Arial;font-size:10pt;font-weight:600;background:transparent;}</string>\n         </property>\n        </widget>\n       </item>\n      </layout>\n     </widget>\n    </item>\n    <item>\n     <widget class="QFrame" name="headerFrame">\n      <layout class="QHBoxLayout" name="headerLayout">\n       <property name="leftMargin">\n        <number>0</number>\n       </property>\n       <property name="topMargin">\n        <number>0</number>\n       </property>\n       <property name="rightMargin">\n        <number>0</number>\n       </property>\n       <property name="bottomMargin">\n        <number>0</number>\n       </property>\n       <property name="spacing">\n        <number>14</number>\n       </property>\n       <item>\n        <widget class="QWidget" name="headingWidget">\n         <layout class="QVBoxLayout" name="headingLayout">\n          <property name="leftMargin">\n           <number>0</number>\n          </property>\n          <property name="topMargin">\n           <number>0</number>\n          </property>\n          <property name="rightMargin">\n           <number>0</number>\n          </property>\n          <property name="bottomMargin">\n           <number>0</number>\n          </property>\n          <property name="spacing">\n           <number>3</number>\n          </property>\n          <item>\n           <widget class="QLabel" name="titleLabel">\n            <property name="text">\n             <string>ME7.5 Launch Control</string>\n            </property>\n           </widget>\n          </item>\n          <item>\n           <widget class="QLabel" name="subtitleLabel">\n            <property name="text">\n             <string>S E L E C T I V E   M U L T I M A P   •   A D V A N C E D   E C U   C A L I B R A T I O N</string>\n            </property>\n           </widget>\n          </item>\n         </layout>\n        </widget>\n       </item>\n       <item>\n        <spacer name="spacer0">\n         <property name="orientation">\n          <enum>Qt::Horizontal</enum>\n         </property>\n         <property name="sizeHint">\n          <size>\n           <width>8</width>\n           <height>8</height>\n          </size>\n         </property>\n        </spacer>\n       </item>\n       <item>\n        <widget class="QLabel" name="ecuBadgeLabel">\n         <property name="text">\n          <string>ME7.5</string>\n         </property>\n         <property name="maximumSize">\n          <size>\n           <width>16777215</width>\n           <height>30</height>\n          </size>\n         </property>\n        </widget>\n       </item>\n       <item>\n        <widget class="QLabel" name="sizeBadgeLabel">\n         <property name="text">\n          <string>1 MB</string>\n         </property>\n         <property name="maximumSize">\n          <size>\n           <width>16777215</width>\n           <height>30</height>\n          </size>\n         </property>\n        </widget>\n       </item>\n       <item>\n        <widget class="QLabel" name="cutBadgeLabel">\n         <property name="text">\n          <string>HARD CUT</string>\n         </property>\n         <property name="maximumSize">\n          <size>\n           <width>16777215</width>\n           <height>30</height>\n          </size>\n         </property>\n        </widget>\n       </item>\n      </layout>\n     </widget>\n    </item>\n    <item>\n     <widget class="QTabWidget" name="mainTabs">\n      <property name="currentIndex">\n       <number>0</number>\n      </property>\n      <widget class="QWidget" name="setupTab">\n       <attribute name="title">\n        <string>Launch Control</string>\n       </attribute>\n       <layout class="QVBoxLayout" name="setupPageLayout">\n        <property name="leftMargin">\n         <number>0</number>\n        </property>\n        <property name="topMargin">\n         <number>0</number>\n        </property>\n        <property name="rightMargin">\n         <number>0</number>\n        </property>\n        <property name="bottomMargin">\n         <number>0</number>\n        </property>\n        <property name="spacing">\n         <number>8</number>\n        </property>\n        <item>\n         <widget class="QScrollArea" name="setupScrollArea">\n          <property name="widgetResizable">\n           <bool>true</bool>\n          </property>\n          <property name="horizontalScrollBarPolicy">\n           <enum>Qt::ScrollBarAlwaysOff</enum>\n          </property>\n          <widget class="QWidget" name="setupContent">\n           <layout class="QVBoxLayout" name="setupContentLayout">\n            <property name="leftMargin">\n             <number>0</number>\n            </property>\n            <property name="topMargin">\n             <number>0</number>\n            </property>\n            <property name="rightMargin">\n             <number>0</number>\n            </property>\n            <property name="bottomMargin">\n             <number>0</number>\n            </property>\n            <property name="spacing">\n             <number>12</number>\n            </property>\n            <property name="rightMargin">\n             <number>3</number>\n            </property>\n            <item>\n             <widget class="QFrame" name="fileFrame">\n              <layout class="QVBoxLayout" name="fileLayout">\n               <property name="leftMargin">\n                <number>15</number>\n               </property>\n               <property name="topMargin">\n                <number>15</number>\n               </property>\n               <property name="rightMargin">\n                <number>15</number>\n               </property>\n               <property name="bottomMargin">\n                <number>15</number>\n               </property>\n               <property name="spacing">\n                <number>8</number>\n               </property>\n               <item>\n                <layout class="QHBoxLayout" name="fileHeadingLayout">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>8</number>\n                 </property>\n                 <item>\n                  <widget class="QLabel" name="fileTitleLabel">\n                   <property name="text">\n                    <string>◆  Project files</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>sectionTitle</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <spacer name="spacer1">\n                   <property name="orientation">\n                    <enum>Qt::Horizontal</enum>\n                   </property>\n                   <property name="sizeHint">\n                    <size>\n                     <width>8</width>\n                     <height>8</height>\n                    </size>\n                   </property>\n                  </spacer>\n                 </item>\n                 <item>\n                  <widget class="QLabel" name="fileHintLabel">\n                   <property name="text">\n                    <string>MultiMap BIN + matching CSV log</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>muted</string>\n                   </property>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n               <item>\n                <layout class="QGridLayout" name="fileGrid" columnstretch="0,1,0">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>7</number>\n                 </property>\n                 <item row="0" column="0">\n                  <widget class="QLabel" name="binPathEditLabel">\n                   <property name="text">\n                    <string>BIN file</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>fieldLabel</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item row="0" column="1">\n                  <widget class="QLineEdit" name="binPathEdit">\n                   <property name="placeholderText">\n                    <string>Select the 1 MB MultiMap BIN…</string>\n                   </property>\n                   <property name="minimumSize">\n                    <size>\n                     <width>0</width>\n                     <height>32</height>\n                    </size>\n                   </property>\n                  </widget>\n                 </item>\n                 <item row="0" column="2">\n                  <widget class="QPushButton" name="browseBinButton">\n                   <property name="text">\n                    <string>Browse…</string>\n                   </property>\n                   <property name="minimumSize">\n                    <size>\n                     <width>105</width>\n                     <height>32</height>\n                    </size>\n                   </property>\n                  </widget>\n                 </item>\n                 <item row="1" column="0">\n                  <widget class="QLabel" name="csvPathEditLabel">\n                   <property name="text">\n                    <string>CSV log</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>fieldLabel</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item row="1" column="1">\n                  <widget class="QLineEdit" name="csvPathEdit">\n                   <property name="placeholderText">\n                    <string>Select the log generated by MultiMap Builder…</string>\n                   </property>\n                   <property name="minimumSize">\n                    <size>\n                     <width>0</width>\n                     <height>32</height>\n                    </size>\n                   </property>\n                  </widget>\n                 </item>\n                 <item row="1" column="2">\n                  <widget class="QPushButton" name="browseCsvButton">\n                   <property name="text">\n                    <string>Browse…</string>\n                   </property>\n                   <property name="minimumSize">\n                    <size>\n                     <width>105</width>\n                     <height>32</height>\n                    </size>\n                   </property>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n               <item>\n                <widget class="QLabel" name="toolsStatusLabel">\n                 <property name="text">\n                  <string>ECU definition and checksums are generated automatically.</string>\n                 </property>\n                 <property name="role" stdset="0">\n                  <string>muted</string>\n                 </property>\n                 <property name="wordWrap">\n                  <bool>true</bool>\n                 </property>\n                </widget>\n               </item>\n              </layout>\n             </widget>\n            </item>\n            <item>\n             <widget class="QFrame" name="lcFrame">\n              <layout class="QVBoxLayout" name="lcLayout">\n               <property name="leftMargin">\n                <number>15</number>\n               </property>\n               <property name="topMargin">\n                <number>15</number>\n               </property>\n               <property name="rightMargin">\n                <number>15</number>\n               </property>\n               <property name="bottomMargin">\n                <number>15</number>\n               </property>\n               <property name="spacing">\n                <number>10</number>\n               </property>\n               <item>\n                <layout class="QHBoxLayout" name="lcHeadingLayout">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>8</number>\n                 </property>\n                 <item>\n                  <widget class="QLabel" name="lcTitleLabel">\n                   <property name="text">\n                    <string>⇄  Launch Control per bank</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>sectionTitle</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <spacer name="spacer2">\n                   <property name="orientation">\n                    <enum>Qt::Horizontal</enum>\n                   </property>\n                   <property name="sizeHint">\n                    <size>\n                     <width>8</width>\n                     <height>8</height>\n                    </size>\n                   </property>\n                  </spacer>\n                 </item>\n                 <item>\n                  <widget class="QLabel" name="selectionLabel">\n                   <property name="text">\n                    <string>3 banks enabled</string>\n                   </property>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n               <item>\n                <widget class="QLabel" name="mmStatusLabel">\n                 <property name="text">\n                  <string>Load a MultiMap CSV to unlock the available banks.</string>\n                 </property>\n                 <property name="wordWrap">\n                  <bool>true</bool>\n                 </property>\n                </widget>\n               </item>\n               <item>\n                <widget class="QWidget" name="banksWidget">\n                 <layout class="QGridLayout" name="bankGrid" columnstretch="0,1,1,1,1,1,1">\n                  <property name="leftMargin">\n                   <number>0</number>\n                  </property>\n                  <property name="topMargin">\n                   <number>0</number>\n                  </property>\n                  <property name="rightMargin">\n                   <number>0</number>\n                  </property>\n                  <property name="bottomMargin">\n                   <number>0</number>\n                  </property>\n                  <property name="spacing">\n                   <number>7</number>\n                  </property>\n                  <item row="0" column="0">\n                   <widget class="QLabel" name="bankHeader0">\n                    <property name="text">\n                     <string>ACTIVE BANK</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="1">\n                   <widget class="QLabel" name="bankHeader1">\n                    <property name="text">\n                     <string>LAUNCH RPM</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="2">\n                   <widget class="QLabel" name="bankHeader2">\n                    <property name="text">\n                     <string>SPEED km/h</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="3">\n                   <widget class="QLabel" name="bankHeader3">\n                    <property name="text">\n                     <string>RPM THRESHOLD</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="4">\n                   <widget class="QLabel" name="bankHeader4">\n                    <property name="text">\n                     <string>PEDAL %</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="5">\n                   <widget class="QLabel" name="bankHeader5">\n                    <property name="text">\n                     <string>CUT ms</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="0" column="6">\n                   <widget class="QLabel" name="bankHeader6">\n                    <property name="text">\n                     <string>TEMP °C</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>columnLabel</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="0">\n                   <widget class="QPushButton" name="bank1Button">\n                    <property name="text">\n                     <string>Bank 1  ·  ORI</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>bank</string>\n                    </property>\n                    <property name="checkable">\n                     <bool>true</bool>\n                    </property>\n                    <property name="checked">\n                     <bool>false</bool>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>128</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Enable or disable Launch Control for bank 1.</string>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="1">\n                   <widget class="QLineEdit" name="bank1_launchEdit">\n                    <property name="text">\n                     <string>3000</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Launch Control RPM for this bank.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: LAUNCH RPM</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="2">\n                   <widget class="QLineEdit" name="bank1_speedEdit">\n                    <property name="text">\n                     <string>3</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Vehicle speed threshold, in km/h.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: SPEED km/h</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="3">\n                   <widget class="QLineEdit" name="bank1_rpmEdit">\n                    <property name="text">\n                     <string>5500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>RPM threshold used by the original LC / NLS routine.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: RPM THRESHOLD</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="4">\n                   <widget class="QLineEdit" name="bank1_pedalEdit">\n                    <property name="text">\n                     <string>70</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Accelerator pedal threshold, in percent.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: PEDAL %</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="5">\n                   <widget class="QLineEdit" name="bank1_cutEdit">\n                    <property name="text">\n                     <string>200</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Ignition cut duration, in ms. Must be a multiple of 20.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: CUT ms</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="1" column="6">\n                   <widget class="QLineEdit" name="bank1_tempEdit">\n                    <property name="text">\n                     <string>75</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Temperature value stored in the bank configuration. The original runtime logic is unchanged.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 1: TEMP °C</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>false</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="0">\n                   <widget class="QPushButton" name="bank2Button">\n                    <property name="text">\n                     <string>Bank 2  ·  LC ON</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>bank</string>\n                    </property>\n                    <property name="checkable">\n                     <bool>true</bool>\n                    </property>\n                    <property name="checked">\n                     <bool>true</bool>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>128</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Enable or disable Launch Control for bank 2.</string>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="1">\n                   <widget class="QLineEdit" name="bank2_launchEdit">\n                    <property name="text">\n                     <string>3500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Launch Control RPM for this bank.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: LAUNCH RPM</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="2">\n                   <widget class="QLineEdit" name="bank2_speedEdit">\n                    <property name="text">\n                     <string>3</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Vehicle speed threshold, in km/h.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: SPEED km/h</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="3">\n                   <widget class="QLineEdit" name="bank2_rpmEdit">\n                    <property name="text">\n                     <string>5500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>RPM threshold used by the original LC / NLS routine.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: RPM THRESHOLD</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="4">\n                   <widget class="QLineEdit" name="bank2_pedalEdit">\n                    <property name="text">\n                     <string>70</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Accelerator pedal threshold, in percent.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: PEDAL %</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="5">\n                   <widget class="QLineEdit" name="bank2_cutEdit">\n                    <property name="text">\n                     <string>200</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Ignition cut duration, in ms. Must be a multiple of 20.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: CUT ms</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="2" column="6">\n                   <widget class="QLineEdit" name="bank2_tempEdit">\n                    <property name="text">\n                     <string>75</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Temperature value stored in the bank configuration. The original runtime logic is unchanged.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 2: TEMP °C</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="0">\n                   <widget class="QPushButton" name="bank3Button">\n                    <property name="text">\n                     <string>Bank 3  ·  LC ON</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>bank</string>\n                    </property>\n                    <property name="checkable">\n                     <bool>true</bool>\n                    </property>\n                    <property name="checked">\n                     <bool>true</bool>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>128</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Enable or disable Launch Control for bank 3.</string>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="1">\n                   <widget class="QLineEdit" name="bank3_launchEdit">\n                    <property name="text">\n                     <string>4000</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Launch Control RPM for this bank.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: LAUNCH RPM</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="2">\n                   <widget class="QLineEdit" name="bank3_speedEdit">\n                    <property name="text">\n                     <string>3</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Vehicle speed threshold, in km/h.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: SPEED km/h</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="3">\n                   <widget class="QLineEdit" name="bank3_rpmEdit">\n                    <property name="text">\n                     <string>5500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>RPM threshold used by the original LC / NLS routine.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: RPM THRESHOLD</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="4">\n                   <widget class="QLineEdit" name="bank3_pedalEdit">\n                    <property name="text">\n                     <string>70</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Accelerator pedal threshold, in percent.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: PEDAL %</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="5">\n                   <widget class="QLineEdit" name="bank3_cutEdit">\n                    <property name="text">\n                     <string>200</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Ignition cut duration, in ms. Must be a multiple of 20.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: CUT ms</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="3" column="6">\n                   <widget class="QLineEdit" name="bank3_tempEdit">\n                    <property name="text">\n                     <string>75</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Temperature value stored in the bank configuration. The original runtime logic is unchanged.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 3: TEMP °C</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="0">\n                   <widget class="QPushButton" name="bank4Button">\n                    <property name="text">\n                     <string>Bank 4  ·  LC ON</string>\n                    </property>\n                    <property name="role" stdset="0">\n                     <string>bank</string>\n                    </property>\n                    <property name="checkable">\n                     <bool>true</bool>\n                    </property>\n                    <property name="checked">\n                     <bool>true</bool>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>128</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Enable or disable Launch Control for bank 4.</string>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="1">\n                   <widget class="QLineEdit" name="bank4_launchEdit">\n                    <property name="text">\n                     <string>4500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Launch Control RPM for this bank.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: LAUNCH RPM</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="2">\n                   <widget class="QLineEdit" name="bank4_speedEdit">\n                    <property name="text">\n                     <string>3</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Vehicle speed threshold, in km/h.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: SPEED km/h</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="3">\n                   <widget class="QLineEdit" name="bank4_rpmEdit">\n                    <property name="text">\n                     <string>5500</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>RPM threshold used by the original LC / NLS routine.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: RPM THRESHOLD</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="4">\n                   <widget class="QLineEdit" name="bank4_pedalEdit">\n                    <property name="text">\n                     <string>70</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Accelerator pedal threshold, in percent.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: PEDAL %</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="5">\n                   <widget class="QLineEdit" name="bank4_cutEdit">\n                    <property name="text">\n                     <string>200</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Ignition cut duration, in ms. Must be a multiple of 20.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: CUT ms</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                  <item row="4" column="6">\n                   <widget class="QLineEdit" name="bank4_tempEdit">\n                    <property name="text">\n                     <string>75</string>\n                    </property>\n                    <property name="alignment">\n                     <set>Qt::AlignCenter</set>\n                    </property>\n                    <property name="minimumSize">\n                     <size>\n                      <width>60</width>\n                      <height>32</height>\n                     </size>\n                    </property>\n                    <property name="toolTip">\n                     <string>Temperature value stored in the bank configuration. The original runtime logic is unchanged.</string>\n                    </property>\n                    <property name="accessibleName">\n                     <string>Bank 4: TEMP °C</string>\n                    </property>\n                    <property name="enabled">\n                     <bool>true</bool>\n                    </property>\n                   </widget>\n                  </item>\n                 </layout>\n                </widget>\n               </item>\n               <item>\n                <widget class="QLabel" name="lcDescriptionLabel">\n                 <property name="text">\n                  <string>Blue banks = LC enabled · Click a bank to toggle it · Clutch activation · Hard Cut</string>\n                 </property>\n                 <property name="wordWrap">\n                  <bool>true</bool>\n                 </property>\n                </widget>\n               </item>\n              </layout>\n             </widget>\n            </item>\n            <item>\n             <spacer name="spacer3">\n              <property name="orientation">\n               <enum>Qt::Vertical</enum>\n              </property>\n              <property name="sizeHint">\n               <size>\n                <width>0</width>\n                <height>0</height>\n               </size>\n              </property>\n             </spacer>\n            </item>\n           </layout>\n          </widget>\n         </widget>\n        </item>\n       </layout>\n      </widget>\n      <widget class="QWidget" name="logTab">\n       <attribute name="title">\n        <string>ECU info / process log</string>\n       </attribute>\n       <layout class="QVBoxLayout" name="logPageLayout">\n        <property name="leftMargin">\n         <number>0</number>\n        </property>\n        <property name="topMargin">\n         <number>0</number>\n        </property>\n        <property name="rightMargin">\n         <number>0</number>\n        </property>\n        <property name="bottomMargin">\n         <number>0</number>\n        </property>\n        <property name="spacing">\n         <number>8</number>\n        </property>\n        <item>\n         <widget class="QScrollArea" name="logScrollArea">\n          <property name="widgetResizable">\n           <bool>true</bool>\n          </property>\n          <property name="horizontalScrollBarPolicy">\n           <enum>Qt::ScrollBarAlwaysOff</enum>\n          </property>\n          <widget class="QWidget" name="logContent">\n           <layout class="QVBoxLayout" name="logContentLayout">\n            <property name="leftMargin">\n             <number>0</number>\n            </property>\n            <property name="topMargin">\n             <number>0</number>\n            </property>\n            <property name="rightMargin">\n             <number>0</number>\n            </property>\n            <property name="bottomMargin">\n             <number>0</number>\n            </property>\n            <property name="spacing">\n             <number>12</number>\n            </property>\n            <property name="rightMargin">\n             <number>3</number>\n            </property>\n            <item>\n             <widget class="QFrame" name="infoFrame">\n              <layout class="QVBoxLayout" name="infoLayout">\n               <property name="leftMargin">\n                <number>15</number>\n               </property>\n               <property name="topMargin">\n                <number>15</number>\n               </property>\n               <property name="rightMargin">\n                <number>15</number>\n               </property>\n               <property name="bottomMargin">\n                <number>15</number>\n               </property>\n               <property name="spacing">\n                <number>12</number>\n               </property>\n               <item>\n                <layout class="QHBoxLayout" name="infoHeadingLayout">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>8</number>\n                 </property>\n                 <item>\n                  <widget class="QLabel" name="infoTitleLabel">\n                   <property name="text">\n                    <string>◆  ECU identification</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>sectionTitle</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <spacer name="spacer4">\n                   <property name="orientation">\n                    <enum>Qt::Horizontal</enum>\n                   </property>\n                   <property name="sizeHint">\n                    <size>\n                     <width>8</width>\n                     <height>8</height>\n                    </size>\n                   </property>\n                  </spacer>\n                 </item>\n                 <item>\n                  <widget class="QLabel" name="infoSourceLabel">\n                   <property name="text">\n                    <string>ME7Check · no BIN analyzed</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>muted</string>\n                   </property>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n               <item>\n                <layout class="QGridLayout" name="infoGrid" columnstretch="1,1,1,1">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>8</number>\n                 </property>\n                 <item row="0" column="0">\n                  <widget class="QWidget" name="boschEcuCell">\n                   <layout class="QVBoxLayout" name="boschEcuLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="boschEcuLabel">\n                      <property name="text">\n                       <string>BOSCH ECU</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="boschEcuValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="0" column="1">\n                  <widget class="QWidget" name="boschSwCell">\n                   <layout class="QVBoxLayout" name="boschSwLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="boschSwLabel">\n                      <property name="text">\n                       <string>BOSCH SOFTWARE</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="boschSwValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="0" column="2">\n                  <widget class="QWidget" name="vagPartCell">\n                   <layout class="QVBoxLayout" name="vagPartLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="vagPartLabel">\n                      <property name="text">\n                       <string>VAG PART NUMBER</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="vagPartValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="0" column="3">\n                  <widget class="QWidget" name="vagSwCell">\n                   <layout class="QVBoxLayout" name="vagSwLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="vagSwLabel">\n                      <property name="text">\n                       <string>VAG SOFTWARE</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="vagSwValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="1" column="0">\n                  <widget class="QWidget" name="engineCell">\n                   <layout class="QVBoxLayout" name="engineLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="engineLabel">\n                      <property name="text">\n                       <string>ENGINE</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="engineValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="1" column="1">\n                  <widget class="QWidget" name="bootromCell">\n                   <layout class="QVBoxLayout" name="bootromLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="bootromLabel">\n                      <property name="text">\n                       <string>BOOTROM</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="bootromValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="1" column="2">\n                  <widget class="QWidget" name="checksumsCell">\n                   <layout class="QVBoxLayout" name="checksumsLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="checksumsLabel">\n                      <property name="text">\n                       <string>CHECKSUMS</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="checksumsValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                 <item row="2" column="0" colspan="4">\n                  <widget class="QWidget" name="epkCell">\n                   <layout class="QVBoxLayout" name="epkLayout">\n                    <property name="leftMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="topMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="rightMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="bottomMargin">\n                     <number>0</number>\n                    </property>\n                    <property name="spacing">\n                     <number>3</number>\n                    </property>\n                    <item>\n                     <widget class="QLabel" name="epkLabel">\n                      <property name="text">\n                       <string>EPK</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuLabel</string>\n                      </property>\n                     </widget>\n                    </item>\n                    <item>\n                     <widget class="QLabel" name="epkValue">\n                      <property name="text">\n                       <string>—</string>\n                      </property>\n                      <property name="role" stdset="0">\n                       <string>ecuValue</string>\n                      </property>\n                      <property name="wordWrap">\n                       <bool>true</bool>\n                      </property>\n                      <property name="textInteractionFlags">\n                       <set>Qt::TextSelectableByMouse</set>\n                      </property>\n                      <property name="textFormat">\n                       <enum>Qt::PlainText</enum>\n                      </property>\n                     </widget>\n                    </item>\n                   </layout>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n              </layout>\n             </widget>\n            </item>\n            <item>\n             <widget class="QFrame" name="logFrame">\n              <layout class="QVBoxLayout" name="logLayout">\n               <property name="leftMargin">\n                <number>15</number>\n               </property>\n               <property name="topMargin">\n                <number>15</number>\n               </property>\n               <property name="rightMargin">\n                <number>15</number>\n               </property>\n               <property name="bottomMargin">\n                <number>15</number>\n               </property>\n               <property name="spacing">\n                <number>10</number>\n               </property>\n               <item>\n                <layout class="QHBoxLayout" name="logHeadingLayout">\n                 <property name="leftMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="topMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="rightMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="bottomMargin">\n                  <number>0</number>\n                 </property>\n                 <property name="spacing">\n                  <number>8</number>\n                 </property>\n                 <item>\n                  <widget class="QLabel" name="logTitleLabel">\n                   <property name="text">\n                    <string>Process log</string>\n                   </property>\n                   <property name="role" stdset="0">\n                    <string>sectionTitle</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <spacer name="spacer5">\n                   <property name="orientation">\n                    <enum>Qt::Horizontal</enum>\n                   </property>\n                   <property name="sizeHint">\n                    <size>\n                     <width>8</width>\n                     <height>8</height>\n                    </size>\n                   </property>\n                  </spacer>\n                 </item>\n                 <item>\n                  <widget class="QPushButton" name="analyzeButton">\n                   <property name="text">\n                    <string>Analyze BIN</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <widget class="QPushButton" name="copyLogButton">\n                   <property name="text">\n                    <string>Copy log</string>\n                   </property>\n                  </widget>\n                 </item>\n                 <item>\n                  <widget class="QPushButton" name="clearLogButton">\n                   <property name="text">\n                    <string>Clear</string>\n                   </property>\n                  </widget>\n                 </item>\n                </layout>\n               </item>\n               <item>\n                <widget class="QPlainTextEdit" name="logText">\n                 <property name="readOnly">\n                  <bool>true</bool>\n                 </property>\n                 <property name="placeholderText">\n                  <string>ME7Check output and the complete patching process will appear here.</string>\n                 </property>\n                 <property name="minimumSize">\n                  <size>\n                   <width>0</width>\n                   <height>170</height>\n                  </size>\n                 </property>\n                </widget>\n               </item>\n              </layout>\n             </widget>\n            </item>\n            <item>\n             <spacer name="spacer6">\n              <property name="orientation">\n               <enum>Qt::Vertical</enum>\n              </property>\n              <property name="sizeHint">\n               <size>\n                <width>0</width>\n                <height>0</height>\n               </size>\n              </property>\n             </spacer>\n            </item>\n           </layout>\n          </widget>\n         </widget>\n        </item>\n       </layout>\n      </widget>\n     </widget>\n    </item>\n    <item>\n     <widget class="QFrame" name="actionFrame">\n      <layout class="QHBoxLayout" name="actionLayout" stretch="1,0,0">\n       <property name="leftMargin">\n        <number>0</number>\n       </property>\n       <property name="topMargin">\n        <number>0</number>\n       </property>\n       <property name="rightMargin">\n        <number>0</number>\n       </property>\n       <property name="bottomMargin">\n        <number>0</number>\n       </property>\n       <property name="spacing">\n        <number>12</number>\n       </property>\n       <item>\n        <widget class="QWidget" name="actionDescription">\n         <layout class="QVBoxLayout" name="actionDescriptionLayout">\n          <property name="leftMargin">\n           <number>0</number>\n          </property>\n          <property name="topMargin">\n           <number>0</number>\n          </property>\n          <property name="rightMargin">\n           <number>0</number>\n          </property>\n          <property name="bottomMargin">\n           <number>0</number>\n          </property>\n          <property name="spacing">\n           <number>4</number>\n          </property>\n          <item>\n           <widget class="QLabel" name="actionTitleLabel">\n            <property name="text">\n             <string>Generate Launch Control firmware</string>\n            </property>\n            <property name="role" stdset="0">\n             <string>fieldLabel</string>\n            </property>\n           </widget>\n          </item>\n          <item>\n           <widget class="QLabel" name="pipelineLabel">\n            <property name="text">\n             <string>ME7Check → Auto ECU → LC patch → XDF → ME7Sum → Final check</string>\n            </property>\n            <property name="role" stdset="0">\n             <string>muted</string>\n            </property>\n            <property name="wordWrap">\n             <bool>true</bool>\n            </property>\n           </widget>\n          </item>\n         </layout>\n        </widget>\n       </item>\n       <item>\n        <spacer name="spacer7">\n         <property name="orientation">\n          <enum>Qt::Horizontal</enum>\n         </property>\n         <property name="sizeHint">\n          <size>\n           <width>8</width>\n           <height>8</height>\n          </size>\n         </property>\n        </spacer>\n       </item>\n       <item>\n        <widget class="QPushButton" name="patchButton">\n         <property name="text">\n          <string>PATCH &amp;&amp; GENERATE CSOK BIN</string>\n         </property>\n         <property name="minimumSize">\n          <size>\n           <width>290</width>\n           <height>44</height>\n          </size>\n         </property>\n        </widget>\n       </item>\n      </layout>\n     </widget>\n    </item>\n    <item>\n     <widget class="QFrame" name="resultFrame">\n      <layout class="QHBoxLayout" name="resultLayout" stretch="1,0,0">\n       <property name="leftMargin">\n        <number>8</number>\n       </property>\n       <property name="topMargin">\n        <number>8</number>\n       </property>\n       <property name="rightMargin">\n        <number>8</number>\n       </property>\n       <property name="bottomMargin">\n        <number>8</number>\n       </property>\n       <property name="spacing">\n        <number>8</number>\n       </property>\n       <item>\n        <widget class="QLabel" name="resultLabel">\n         <property name="text">\n          <string>No output generated yet.</string>\n         </property>\n         <property name="wordWrap">\n          <bool>true</bool>\n         </property>\n        </widget>\n       </item>\n       <item>\n        <spacer name="spacer8">\n         <property name="orientation">\n          <enum>Qt::Horizontal</enum>\n         </property>\n         <property name="sizeHint">\n          <size>\n           <width>8</width>\n           <height>8</height>\n          </size>\n         </property>\n        </spacer>\n       </item>\n       <item>\n        <widget class="QPushButton" name="openOutputButton">\n         <property name="text">\n          <string>Open folder</string>\n         </property>\n        </widget>\n       </item>\n      </layout>\n     </widget>\n    </item>\n    <item>\n     <widget class="QProgressBar" name="progressBar">\n      <property name="value">\n       <number>0</number>\n      </property>\n      <property name="textVisible">\n       <bool>false</bool>\n      </property>\n     </widget>\n    </item>\n    <item>\n     <widget class="QFrame" name="footerFrame">\n      <layout class="QHBoxLayout" name="footerLayout" stretch="1,0,0">\n       <property name="leftMargin">\n        <number>0</number>\n       </property>\n       <property name="topMargin">\n        <number>0</number>\n       </property>\n       <property name="rightMargin">\n        <number>0</number>\n       </property>\n       <property name="bottomMargin">\n        <number>0</number>\n       </property>\n       <property name="spacing">\n        <number>10</number>\n       </property>\n       <property name="topMargin">\n        <number>8</number>\n       </property>\n       <item>\n        <widget class="QLabel" name="statusLabel">\n         <property name="text">\n          <string>Select a MultiMap BIN and its CSV log.</string>\n         </property>\n         <property name="wordWrap">\n          <bool>true</bool>\n         </property>\n        </widget>\n       </item>\n       <item>\n        <spacer name="spacer9">\n         <property name="orientation">\n          <enum>Qt::Horizontal</enum>\n         </property>\n         <property name="sizeHint">\n          <size>\n           <width>8</width>\n           <height>8</height>\n          </size>\n         </property>\n        </spacer>\n       </item>\n       <item>\n        <widget class="QLabel" name="stateLabel">\n         <property name="text">\n          <string>READY</string>\n         </property>\n        </widget>\n       </item>\n      </layout>\n     </widget>\n    </item>\n   </layout>\n  </widget>\n </widget>\n <resources />\n <connections />\n</ui>\n'

@dataclass(frozen=True)
class GuiJob:
    """Snapshot of GUI inputs, taken before the worker starts."""
    mode: str
    bin_path: Path
    tools: tuple[Path, ...]
    multimap: Optional[MultiMapInfo] = None
    configs: Optional[Dict[int, Configuration]] = None


def run_gui_job(job: GuiJob, progress, set_info) -> dict:
    """The original GUI pipeline using the unchanged backend functions.

    No GUI objects are accessed from this worker function.
    """
    bin_path = job.bin_path
    if not bin_path.is_file():
        raise LaunchError("Select a valid BIN file.")
    me7check = job.tools[0]
    if job.mode == "analyze":
        output = _run_wine_tool(
            "wine", me7check, [str(bin_path)], cwd=bin_path.parent,
            stage="ME7CHECK / INPUT BIN IDENTIFICATION", strict=False,
        )
        summary = _parse_me7check_summary(output)
        set_info(summary)
        return {"mode": "analyze", "summary": summary}

    if len(bin_path.read_bytes()) != 1024 * 1024:
        raise LaunchError("Expected a 1 MB ME7.5 MultiMap BIN.")
    _, me7info, me7sum = job.tools
    multimap, configs = job.multimap, job.configs
    if multimap is None or not configs:
        raise LaunchError("Select a valid CSV and at least one Launch Control bank.")

    print("=" * 72)
    print("ME7.5 MULTIMAP SELECTIVE LAUNCH CONTROL - GUI".center(72))
    print("Automatic ECU generation | XDF | Checksum correction".center(72))
    print("=" * 72)

    progress(8, "Reading ECU information with ME7Check…")
    check_in = _run_wine_tool(
        "wine", me7check, [str(bin_path)], cwd=bin_path.parent,
        stage="ME7CHECK / INPUT BIN IDENTIFICATION", strict=False,
    )
    set_info(_parse_me7check_summary(check_in))

    progress(22, "Generating .ecu with ME7Info…")
    ecu_file = _generate_ecu_with_me7info(
        wine="wine", me7info=me7info, bin_file=bin_path,
        ecus_dir_arg=Path("ecus"), keep_source=False,
    )

    progress(35, "Validating MultiMap and configuration…")
    original = bin_path.read_bytes()
    data = bytearray(original)
    entries = parse_ecu_file(ecu_file)
    print(f"[ OK ] MultiMap detected: {multimap.bank_count} banks, active bank WORD @ 0x{multimap.active_bank_ram:06X}")
    print("Selected LC banks: " + ", ".join(str(x) for x in sorted(configs)))
    for bank in sorted(configs):
        cfg = configs[bank]
        print(
            f"  Bank {bank}: LC {cfg.launch_rpm} rpm | speed {cfg.speed_kmh:g} km/h | "
            f"RPM threshold {cfg.rpm_threshold} | pedal {cfg.accelerator_percent:g}% | "
            f"cut {cfg.ignition_cut_ms} ms | temp {cfg.minimum_temperature} C"
        )

    progress(52, "Injecting selective Launch Control…")
    result = install_multimap_launch(data, entries, multimap, configs)
    patched_target = save_multimap_output(bin_path, original, data)

    progress(66, "Generating XDF…")
    xdf_target = patched_target.with_suffix(".xdf")
    generate_multimap_lc_xdf(
        xdf_target, patched_target, multimap, configs, result["bank_config_addresses"],
    )

    progress(78, "Correcting checksums with ME7Sum…")
    csok_target = _run_me7sum(
        wine="wine", me7sum=me7sum,
        patched_bin=patched_target,
        csok_bin=_default_csok_path(patched_target),
    )

    progress(90, "Verifying final CSOK BIN with ME7Check…")
    final_check = _run_wine_tool(
        "wine", me7check, [str(csok_target)], cwd=csok_target.parent,
        stage="ME7CHECK / FINAL CSOK VERIFICATION", strict=False,
    )
    final_summary = _parse_me7check_summary(final_check)
    if "Checksums" in final_summary:
        print(f"[FINAL CHECKSUM STATUS] {final_summary['Checksums']}")
    set_info(final_summary)
    print()
    print("=" * 72)
    print("PIPELINE COMPLETED".center(72))
    print("=" * 72)
    print(f"Generated ECU   : {ecu_file}")
    print(f"Patched BIN     : {patched_target}")
    print(f"Generated XDF   : {xdf_target}")
    print(f"FINAL CSOK BIN  : {csok_target}")
    return {
        "mode": "patch", "ecu": ecu_file, "patched": patched_target,
        "xdf": xdf_target, "csok": csok_target, "summary": final_summary,
        # An absent/ambiguous report must not become a green 'verified' badge.
        "verified": bool(re.search(r"Found\s+0\s+errors\b", final_check, re.I))
                    and "checksum failure" not in final_check.lower(),
    }


def create_mapstyle_window():
    """Create the Qt window; importing the patching backend needs no Qt."""
    import contextlib
    try:
        from PySide6 import QtCore, QtGui, QtWidgets, QtUiTools
    except ImportError as exc:
        raise LaunchError(
            "The new GUI requires PySide6. Install it with:\n"
            "python3 -m pip install PySide6"
        ) from exc

    class Worker(QtCore.QObject):
        log = QtCore.Signal(str)
        progress = QtCore.Signal(int, str)
        ecu_info = QtCore.Signal(dict)
        succeeded = QtCore.Signal(dict)
        failed = QtCore.Signal(str)
        done = QtCore.Signal()

        def __init__(self, job):
            super().__init__()
            self.job = job

        @QtCore.Slot()
        def run(self):
            class Writer:
                def write(writer, text):
                    if text:
                        self.log.emit(text)
                    return len(text)

                def flush(writer):
                    pass

            try:
                with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
                    result = run_gui_job(self.job, self.progress.emit, self.ecu_info.emit)
                self.succeeded.emit(result)
            except Exception as exc:
                self.log.emit(f"\n[ERROR] {exc}\n")
                self.failed.emit(str(exc))
            finally:
                self.done.emit()

    class FormLoader(QtUiTools.QUiLoader):
        def __init__(self, window):
            super().__init__()
            self.window = window

        def createWidget(self, class_name, parent=None, name=""):
            if parent is None and class_name == "QMainWindow":
                self.window.setObjectName(name)
                return self.window
            return super().createWidget(class_name, parent, name)

    class MainWindow(QtWidgets.QMainWindow):
        info_names = {
            "Bosch ECU": "boschEcuValue", "Bosch SW": "boschSwValue",
            "VAG Part": "vagPartValue", "VAG SW": "vagSwValue",
            "Engine": "engineValue", "Bootrom": "bootromValue",
            "EPK": "epkValue", "Checksums": "checksumsValue",
        }
        field_specs = (
            ("launch", "Launch RPM", int, encode_rpm),
            ("speed", "Speed km/h", float, encode_speed),
            ("rpm", "RPM threshold", int, encode_rpm),
            ("pedal", "Pedal %", float, encode_pedal),
            ("cut", "Cut ms", int, encode_ignition_ms),
            ("temp", "Temperature", int, encode_temperature),
        )

        def __init__(self):
            super().__init__()
            self.script_dir = (Path(sys.executable).resolve().parent
                               if getattr(sys, "frozen", False)
                               else Path(__file__).resolve().parent)
            self.running = False
            self.current_mm = None
            self.loaded_csv = None
            self.last_output = None
            self.thread = None
            self.worker = None
            self.job_mode = None
            self.job_error = False
            self.message_box = None
            ui_file = self.script_dir / "launch_multimap.ui"
            form = ui_file.read_text(encoding="utf-8") if ui_file.is_file() else MAPSTYLE_UI
            buffer = QtCore.QBuffer()
            buffer.setData(form.encode("utf-8"))
            buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
            loader = FormLoader(self)
            loaded = loader.load(buffer)
            buffer.close()
            if loaded is None:
                raise LaunchError(f"Cannot load launch_multimap.ui: {loader.errorString()}")
            for child in self.findChildren(QtWidgets.QWidget):
                if child.objectName():
                    setattr(self, child.objectName(), child)
            required = [
                "mainTabs", "binPathEdit", "csvPathEdit", "patchButton",
                "githubLinkLabel", "githubIconLabel",
                "banksWidget", "logText", "stateLabel", "resultFrame", *self.info_names.values(),
            ]
            if any(not hasattr(self, name) for name in required):
                raise LaunchError("The launch_multimap.ui file is incompatible with this patcher.")
            self.setup_github_link()
            self.resultFrame.hide()
            self.logText.setMaximumBlockCount(15000)
            self.bank_rows = {}
            for bank in range(1, 5):
                row = {"enabled": getattr(self, f"bank{bank}Button")}
                for key, _, _, _ in self.field_specs:
                    edit = getattr(self, f"bank{bank}_{key}Edit")
                    row[key] = edit
                    edit.textChanged.connect(lambda _, e=edit: self.clear_invalid(e))
                row["enabled"].toggled.connect(lambda _, b=bank: self.bank_toggled(b))
                self.bank_rows[bank] = row
            self.reset_banks(4)
            self.banksWidget.setEnabled(False)
            self.selectionLabel.setText("Waiting for CSV")
            self.browseBinButton.clicked.connect(self.choose_bin)
            self.browseCsvButton.clicked.connect(self.choose_csv)
            self.binPathEdit.textChanged.connect(self.bin_changed)
            self.binPathEdit.editingFinished.connect(self.analyze_bin)
            self.csvPathEdit.textChanged.connect(self.csv_changed)
            self.csvPathEdit.editingFinished.connect(self.load_csv)
            self.patchButton.clicked.connect(self.start_pipeline)
            self.analyzeButton.clicked.connect(self.analyze_bin)
            self.copyLogButton.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.logText.toPlainText()))
            self.clearLogButton.clicked.connect(self.logText.clear)
            self.openOutputButton.clicked.connect(self.open_output)
            self.refresh_ready()
            self.statusLabel.setText("Select a MultiMap BIN and its CSV log.")

        def setup_github_link(self):
            """MapSwitch-style link; optional icon files live beside the program."""
            self.githubLinkLabel.setText(
                f'<a href="{GITHUB_URL}" style="color:#24292f; text-decoration:none;">GitHub</a>'
            )
            self.githubLinkLabel.setOpenExternalLinks(True)
            self.githubLinkLabel.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            self.githubLinkLabel.setToolTip("Open LDRXN on GitHub")
            for filename in ("github.png", "github.svg"):
                path = self.script_dir / filename
                if not path.is_file():
                    continue
                pixmap = QtGui.QPixmap(str(path))
                if not pixmap.isNull():
                    self.githubIconLabel.setPixmap(pixmap.scaled(
                        18, 18,
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    ))
                    return
            self.githubIconLabel.hide()

        @staticmethod
        def path(edit):
            return Path(edit.text().strip().strip('"')).expanduser().resolve()

        @staticmethod
        def clear_invalid(edit):
            if edit.property("invalid"):
                edit.setProperty("invalid", False)
                edit.style().unpolish(edit)
                edit.style().polish(edit)

        def set_status(self, message, state=None, color="#0969da"):
            self.statusLabel.setText(message)
            if state:
                self.stateLabel.setText(state)
                self.stateLabel.setStyleSheet(f"color: {color};")

        def show_error(self, title, message):
            self.message_box = QtWidgets.QMessageBox(self)
            self.message_box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            self.message_box.setWindowTitle(title)
            self.message_box.setText(message)
            self.message_box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
            self.message_box.open()

        def choose_bin(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Select MultiMap BIN", self.binPathEdit.text(), "Bosch ME7 BIN (*.bin);;All files (*)")
            if path:
                self.binPathEdit.setText(path)
                self.analyze_bin()

        def choose_csv(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Select MultiMap CSV log", self.csvPathEdit.text(), "MultiMap CSV (*.csv);;All files (*)")
            if path:
                self.csvPathEdit.setText(path)
                self.load_csv()

        def bin_changed(self):
            for name in self.info_names.values():
                getattr(self, name).setText("—")
            self.infoSourceLabel.setText("ME7Check · no BIN analyzed")
            self.resultFrame.hide()
            self.last_output = None
            self.progressBar.setValue(0)
            self.refresh_ready()

        def csv_changed(self):
            self.current_mm = None
            self.banksWidget.setEnabled(False)
            self.mmStatusLabel.setText("Load a MultiMap CSV to unlock the available banks.")
            self.selectionLabel.setText("Waiting for CSV")
            self.refresh_ready()

        def load_csv(self):
            if self.running:
                return False
            if not self.csvPathEdit.text().strip():
                self.csv_changed()
                return False
            try:
                path = self.path(self.csvPathEdit)
                mm = parse_multimap_csv(path)
                identity = (path, path.stat().st_mtime_ns, mm.bank_count)
                self.current_mm = mm
                if identity != self.loaded_csv:
                    self.reset_banks(mm.bank_count)
                self.loaded_csv = identity
                self.banksWidget.setEnabled(True)
                self.mmStatusLabel.setText(
                    f"{mm.bank_count} banks detected  ·  Runtime RAM 0x{mm.runtime_ram:06X}"
                    f"  ·  Active bank WORD 0x{mm.active_bank_ram:06X}")
                self.mmStatusLabel.setStyleSheet("")
                self.update_selection()
                self.set_status("MultiMap CSV loaded and validated.", "CSV LOADED")
                self.refresh_ready()
                return True
            except Exception as exc:
                self.current_mm = None
                self.banksWidget.setEnabled(False)
                self.selectionLabel.setText("Invalid CSV")
                self.mmStatusLabel.setText(f"CSV error: {exc}")
                self.mmStatusLabel.setStyleSheet("color: #b9382d;")
                self.set_status("Select a valid MultiMap CSV log.", "CHECK CSV", "#b9382d")
                self.refresh_ready()
                return False

        def reset_banks(self, count):
            for bank, row in self.bank_rows.items():
                for w in row.values():
                    w.setVisible(bank <= count)
                row["enabled"].setChecked(bank >= 2)
                values = {"launch": 3000 + (bank-1)*500, "speed": DEFAULT_SPEED_KMH,
                          "rpm": DEFAULT_RPM_THRESHOLD, "pedal": 70.0,
                          "cut": DEFAULT_IGNITION_CUT_MS, "temp": 75}
                for key, value in values.items():
                    row[key].setText(f"{value:g}")
                self.bank_toggled(bank)

        def bank_toggled(self, bank):
            row = self.bank_rows[bank]
            active = row["enabled"].isChecked()
            label = f"Bank {bank}" + (" · ORI" if bank == 1 else "")
            row["enabled"].setText(label + (" · ON" if active else " · OFF"))
            for key, _, _, _ in self.field_specs:
                row[key].setEnabled(active)
            self.update_selection()
            self.refresh_ready()

        def update_selection(self):
            count = self.current_mm.bank_count if self.current_mm else 4
            selected = [str(b) for b in range(1, count + 1) if self.bank_rows[b]["enabled"].isChecked()]
            self.selectionLabel.setText("LC banks: " + ", ".join(selected) if selected else "No banks enabled")

        def refresh_ready(self, *_):
            if not hasattr(self, "bank_rows"):
                return
            folder = self.script_dir
            names = ("ME7Check.exe", "ME7Info.exe", "me7sum.exe")
            missing = [name for name in names if not (folder / name).is_file()]
            self.toolsStatusLabel.setText(
                "Missing in program folder: " + ", ".join(missing) if missing else
                "ECU definition and checksums are generated automatically.")
            self.toolsStatusLabel.setToolTip(f"Program folder: {folder}\nRequired: " + ", ".join(names))
            self.toolsStatusLabel.setStyleSheet("color: #a56a14;" if missing else "color: #1a7f37;")
            valid_bin = bool(self.binPathEdit.text().strip()) and self.path(self.binPathEdit).is_file()
            selected = self.current_mm and any(self.bank_rows[b]["enabled"].isChecked()
                                               for b in range(1, self.current_mm.bank_count + 1))
            ready = bool(valid_bin and self.current_mm and selected and not missing)
            self.patchButton.setEnabled(ready and not self.running)
            self.patchButton.setToolTip("Generate BIN, XDF and checksum-corrected BIN." if ready else
                                       "Select a BIN, a valid CSV and at least one LC bank. Place the three ME7 tools beside the program.")
            self.analyzeButton.setEnabled(valid_bin and not self.running and (folder / names[0]).is_file())

        def resolve_tools(self, analyze=False):
            names = ("ME7Check.exe",) if analyze else ("ME7Check.exe", "ME7Info.exe", "me7sum.exe")
            return tuple(_resolve_tool(self.script_dir, name) for name in names)

        def collect_configs(self, count):
            import math
            configs = {}
            for bank in range(1, count + 1):
                row = self.bank_rows[bank]
                if not row["enabled"].isChecked():
                    continue
                values = {}
                for key, label, converter, encoder in self.field_specs:
                    edit = row[key]
                    try:
                        # Accept Polish decimal commas as well as decimal points.
                        value = converter(edit.text().strip().replace(",", "."))
                        if not math.isfinite(value):
                            raise ValueError("Enter a finite number.")
                        encoder(value)
                        values[key] = value
                    except (ValueError, OverflowError) as exc:
                        edit.setProperty("invalid", True)
                        edit.style().unpolish(edit)
                        edit.style().polish(edit)
                        self.mainTabs.setCurrentIndex(0)
                        edit.setFocus()
                        edit.selectAll()
                        raise LaunchError(f"Bank {bank} · {label}: {exc}") from exc
                configs[bank] = Configuration(
                    speed_kmh=values["speed"], launch_rpm=values["launch"],
                    rpm_threshold=values["rpm"], accelerator_percent=values["pedal"],
                    ignition_cut_ms=values["cut"], minimum_temperature=values["temp"],
                    trigger=TRIGGER_CLUTCH,
                )
            if not configs:
                raise LaunchError("Select at least one bank for Launch Control.")
            return configs

        def analyze_bin(self):
            if self.running or not self.binPathEdit.text().strip():
                return
            try:
                path = self.path(self.binPathEdit)
                if not path.is_file():
                    raise LaunchError("Select a valid BIN file.")
                job = GuiJob("analyze", path, self.resolve_tools(analyze=True))
                self.start_job(job)
            except Exception as exc:
                self.append_log(f"[ERROR] {exc}\n")
                self.set_status("ME7Check analysis unavailable. Check the BIN and ME7Check.exe beside the program.", "CHECK FILES", "#a56a14")

        def start_pipeline(self):
            if self.running:
                return
            try:
                if not self.load_csv():
                    raise LaunchError("Select a valid MultiMap CSV log.")
                path = self.path(self.binPathEdit)
                if not path.is_file() or path.stat().st_size != 1024 * 1024:
                    raise LaunchError("Select a valid 1 MB ME7.5 MultiMap BIN.")
                configs = self.collect_configs(self.current_mm.bank_count)
                job = GuiJob("patch", path, self.resolve_tools(), self.current_mm, configs)
            except Exception as exc:
                self.show_error("Cannot start patching", str(exc))
                return
            self.logText.clear()
            self.resultFrame.hide()
            self.last_output = None
            self.mainTabs.setCurrentIndex(1)
            self.start_job(job)

        def start_job(self, job):
            # Set the guard before starting the thread, including analysis jobs.
            self.running = True
            self.job_mode = job.mode
            self.job_error = False
            self.fileFrame.setEnabled(False)
            self.banksWidget.setEnabled(False)
            self.clearLogButton.setEnabled(False)
            self.refresh_ready()
            self.progressBar.setRange(0, 0 if job.mode == "analyze" else 100)
            self.progressBar.setValue(0)
            self.infoSourceLabel.setText("ME7Check · input BIN")
            self.set_status("Analyzing BIN…" if job.mode == "analyze" else "Starting patch pipeline…", "WORKING")
            self.thread = QtCore.QThread(self)
            self.worker = Worker(job)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.log.connect(self.append_log)
            self.worker.progress.connect(self.set_progress)
            self.worker.ecu_info.connect(self.set_info)
            self.worker.succeeded.connect(self.job_succeeded)
            self.worker.failed.connect(self.job_failed)
            self.worker.done.connect(self.thread.quit)
            self.worker.done.connect(self.worker.deleteLater)
            self.thread.finished.connect(self.job_finished)
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.start()

        @QtCore.Slot(str)
        def append_log(self, message):
            cursor = self.logText.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertText(message)
            self.logText.setTextCursor(cursor)
            self.logText.ensureCursorVisible()

        @QtCore.Slot(int, str)
        def set_progress(self, value, status):
            self.progressBar.setValue(value)
            self.set_status(status, f"{value}%")

        @QtCore.Slot(dict)
        def set_info(self, summary):
            for key, name in self.info_names.items():
                if key in summary:
                    getattr(self, name).setText(summary[key])
                    getattr(self, name).setToolTip(summary[key])

        @QtCore.Slot(dict)
        def job_succeeded(self, result):
            if result["mode"] == "analyze":
                self.set_status("BIN analyzed. ECU information is available in the log tab.", "ANALYZED", "#1a7f37")
                return
            self.last_output = result["csok"]
            self.infoSourceLabel.setText("ME7Check · final CSOK BIN")
            self.progressBar.setValue(100)
            self.resultLabel.setText(f"Final BIN: {result['csok'].name}\nXDF: {result['xdf'].name}")
            self.resultLabel.setToolTip(f"{result['csok']}\n{result['xdf']}")
            self.resultFrame.show()
            if result["verified"]:
                self.resultFrame.setStyleSheet("")
                self.resultLabel.setStyleSheet("")
                self.set_status("Files generated. Final ME7Check: 0 errors reported.", "COMPLETE", "#1a7f37")
            else:
                self.resultFrame.setStyleSheet("QFrame#resultFrame {background:#fff8e6;border-color:#e7cf91;}")
                self.resultLabel.setStyleSheet("color:#956317;")
                self.set_status("Files generated. Review the final ME7Check report before use.", "REVIEW CHECK", "#a56a14")

        @QtCore.Slot(str)
        def job_failed(self, message):
            self.job_error = True
            self.set_status("Operation failed. See the process log.", "ERROR", "#b9382d")
            self.mainTabs.setCurrentIndex(1)
            self.show_error("Operation failed", message)

        @QtCore.Slot()
        def job_finished(self):
            self.running = False
            self.thread = None
            self.worker = None
            self.fileFrame.setEnabled(True)
            self.banksWidget.setEnabled(self.current_mm is not None)
            self.clearLogButton.setEnabled(True)
            self.progressBar.setRange(0, 100)
            if self.job_mode == "analyze" or self.job_error:
                self.progressBar.setValue(0)
            self.refresh_ready()

        def open_output(self):
            if self.last_output:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.last_output.parent)))

        def closeEvent(self, event):
            if self.running:
                self.set_status("An operation is running. Close the window after it finishes.", "WORKING")
                event.ignore()
            else:
                event.accept()

    return MainWindow()


def final_multimap_gui_main() -> int:
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:
        raise LaunchError("Install the GUI dependency first: python3 -m pip install PySide6") from exc
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setApplicationName("ME7.5 Selective Launch Control")
    window = create_mapstyle_window()
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        window.resize(min(1040, max(900, available.width() - 60)),
                      min(760, max(620, available.height() - 80)))
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(final_multimap_gui_main())
    except KeyboardInterrupt:
        print()
        fail("Operation cancelled by the user.")
        raise SystemExit(130)
    except (LaunchError, OSError, ValueError) as exc:
        fail(str(exc))
        raise SystemExit(1)
