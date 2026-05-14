import asyncio
import dataclasses
import datetime
import json
import pathlib
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from rich.console import Console
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Digits, Footer, Header, Label, ProgressBar, Static

# ── BLE service / characteristic UUIDs ────────────────────────────────────────
CYCLING_POWER_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_SPEED_CADENCE_SERVICE = "00001816-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT = "00002a5b-0000-1000-8000-00805f9b34fb"
FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
FTMS_INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"

# ── ERG settings ──────────────────────────────────────────────────────────────
POWER_STEP = 10
MIN_POWER = 0
MAX_POWER = 1000

console = Console()

# ── Power zones ───────────────────────────────────────────────────────────────
_ZONES = [
    (100, "Z1", "grey62"),
    (160, "Z2", "steel_blue1"),
    (230, "Z3", "green3"),
    (300, "Z4", "yellow3"),
    (375, "Z5", "dark_orange"),
    (450, "Z6", "red1"),
    (9999, "Z7", "magenta"),
]


def parse_power_debug(data: bytearray) -> tuple[int, tuple[int, int] | None, dict[str, str]]:
    debug: dict[str, str] = {
        "flags": "",
        "length": str(len(data)),
        "power_bytes": "",
        "expected_length": "",
        "valid": "no",
        "note": "",
    }
    if len(data) < 4:
        debug["note"] = "packet too short"
        return 0, None, debug

    flags = int.from_bytes(data[0:2], "little")
    power_bytes = data[2:4].hex().upper()
    debug["flags"] = f"0x{flags:04X}"
    debug["power_bytes"] = power_bytes

    (power,) = struct.unpack_from("<h", data, 2)
    offset = 4
    cadence_info: tuple[int, int] | None = None

    expected_length = 4
    if flags & 0x01:
        expected_length += 1
    if flags & 0x04:
        expected_length += 2
    if flags & 0x10:
        expected_length += 6
    if flags & 0x20:
        expected_length += 4
    debug["expected_length"] = str(expected_length)

    if flags & 0x01:
        if len(data) < offset + 1:
            debug["note"] = "missing pedal balance byte"
            return power, None, debug
        offset += 1
    if flags & 0x04:
        if len(data) < offset + 2:
            debug["note"] = "missing accumulated torque"
            return power, None, debug
        offset += 2
    if flags & 0x10:
        if len(data) < offset + 6:
            debug["note"] = "missing wheel revolution data"
            return power, None, debug
        offset += 6
    if flags & 0x20:
        if len(data) < offset + 4:
            debug["note"] = "missing crank revolution data"
            return power, None, debug
        cumulative_crank_revs = struct.unpack_from("<H", data, offset)[0]
        last_crank_event_time = struct.unpack_from("<H", data, offset + 2)[0]
        cadence_info = (cumulative_crank_revs, last_crank_event_time)

    debug["valid"] = "yes"
    debug["note"] = "parsed OK"
    return power, cadence_info, debug


def parse_power(data: bytearray) -> tuple[int, tuple[int, int] | None]:
    power, cadence_info, _ = parse_power_debug(data)
    return power, cadence_info


def parse_csc(data: bytearray) -> tuple[int, int] | None:
    if len(data) < 1:
        return None
    flags = data[0]
    offset = 1
    if flags & 0x01:
        if len(data) < offset + 6:
            return None
        offset += 6
    if flags & 0x02:
        if len(data) < offset + 4:
            return None
        cumulative_crank_revs = struct.unpack_from("<H", data, offset)[0]
        last_crank_event_time = struct.unpack_from("<H", data, offset + 2)[0]
        return cumulative_crank_revs, last_crank_event_time
    return None


def parse_indoor_bike_data(data: bytearray) -> tuple[float | None, int | None]:
    """Parse FTMS Indoor Bike Data (0x2AD2). Returns (speed_kmh, cadence_rpm)."""
    if len(data) < 2:
        return None, None
    flags = int.from_bytes(data[0:2], "little")
    offset = 2
    speed: float | None = None
    cadence: int | None = None
    # Bit 0 = 0: Instantaneous Speed present
    if not (flags & 0x01):
        if len(data) >= offset + 2:
            speed = struct.unpack_from("<H", data, offset)[0] * 0.01
            offset += 2
    # Bit 1: Average Speed present
    if flags & 0x02:
        offset += 2
    # Bit 2: Instantaneous Cadence present
    if flags & 0x04:
        if len(data) >= offset + 2:
            cadence = int(struct.unpack_from("<H", data, offset)[0] * 0.5)
    return speed, cadence


def parse_hr(data: bytearray) -> int:
    if len(data) < 2:
        return 0
    if data[0] & 0x01:  # 16-bit value
        return struct.unpack_from("<H", data, 1)[0] if len(data) >= 3 else 0
    return data[1]  # 8-bit value


def _zone(watts: int) -> tuple[str, str]:
    for limit, label, color in _ZONES:
        if watts <= limit:
            return label, color
    return "Z7", "magenta"


_ZONE_HEX = (
    "#9e9e9e",
    "#4fc3f7",
    "#66bb6a",
    "#ffd54f",
    "#ffa726",
    "#ef5350",
    "#ce93d8",
)


def _zone_hex(watts: int) -> str:
    for i, (limit, _, _) in enumerate(_ZONES):
        if watts <= limit:
            return _ZONE_HEX[i]
    return _ZONE_HEX[-1]


def _hr_hex(bpm: int) -> str:
    if bpm < 100:
        return "#9e9e9e"
    if bpm < 130:
        return "#4fc3f7"
    if bpm < 155:
        return "#66bb6a"
    if bpm < 170:
        return "#ffd54f"
    return "#ef5350"


def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def physics_speed(
    watts: int, mass_kg: float = 75.0, cda: float = 0.32, crr: float = 0.004
) -> float:
    """Estimate flat-road speed (km/h) from power using standard cycling physics.
    Solves: P = v * (Crr*m*g + 0.5*rho*CdA*v^2)
    """
    if watts <= 0:
        return 0.0
    g = 9.81
    rho = 1.225
    f_roll = crr * mass_kg * g
    # Binary search for v in m/s (0 – 30 m/s = 0 – 108 km/h)
    lo, hi = 0.0, 30.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if (0.5 * rho * cda * mid**2 + f_roll) * mid < watts:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0 * 3.6  # m/s → km/h


# ── Workout plan ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Interval:
    duration: int
    power: float
    ramp: bool = False


@dataclasses.dataclass
class Workout:
    name: str
    intervals: list[Interval]
    ftp: int = 250

    @property
    def total_duration(self) -> int:
        return sum(iv.duration for iv in self.intervals)


@dataclasses.dataclass
class WorkoutStatus:
    name: str
    interval_idx: int
    total_intervals: int
    elapsed_in: float
    interval_dur: float
    total_elapsed: float
    total_dur: int
    ramp: bool
    done: bool = False


FTP_DEFAULT = 250


def _power_from_ftp(power: float, ftp: int) -> int:
    if power <= 4.0:
        return int(power * ftp)
    return int(power)


def load_workout(path: str) -> Workout:
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    ftp = raw.get("ftp", FTP_DEFAULT)
    intervals = [
        Interval(
            duration=int(iv["duration"]),
            power=float(iv["power"]),
            ramp=iv.get("type", "steady") == "ramp",
        )
        for iv in raw["intervals"]
    ]
    return Workout(name=raw.get("name", "Workout"), intervals=intervals, ftp=ftp)


def workout_target(workout: Workout, elapsed: float) -> tuple[int, WorkoutStatus]:
    ftp = workout.ftp or FTP_DEFAULT
    t = 0.0
    prev_power: float = 0.0
    for idx, interval in enumerate(workout.intervals):
        end = t + interval.duration
        if elapsed < end or idx == len(workout.intervals) - 1:
            elapsed_in = min(elapsed - t, float(interval.duration))
            interval_power = _power_from_ftp(interval.power, ftp)
            prev_power_watts = _power_from_ftp(prev_power if prev_power > 0.01 else 0.5, ftp)
            if interval.ramp:
                frac = elapsed_in / interval.duration
                power = int(
                    prev_power_watts
                    + (interval_power - prev_power_watts) * min(frac, 1.0)
                )
            else:
                power = interval_power
            return power, WorkoutStatus(
                name=workout.name,
                interval_idx=idx,
                total_intervals=len(workout.intervals),
                elapsed_in=elapsed_in,
                interval_dur=float(interval.duration),
                total_elapsed=elapsed,
                total_dur=workout.total_duration,
                ramp=interval.ramp,
            )
        prev_power = interval.power
        t = end
    last = workout.intervals[-1]
    return _power_from_ftp(last.power, ftp), WorkoutStatus(
        name=workout.name,
        interval_idx=len(workout.intervals) - 1,
        total_intervals=len(workout.intervals),
        elapsed_in=float(last.duration),
        interval_dur=float(last.duration),
        total_elapsed=float(workout.total_duration),
        total_dur=workout.total_duration,
        ramp=False,
        done=True,
    )


# ── TCX export ───────────────────────────────────────────────────────────────

_TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
_EXT_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("", _TCX_NS)
ET.register_namespace("ext", _EXT_NS)
ET.register_namespace("xsi", _XSI_NS)


def _sub(
    parent: ET.Element,
    tag: str,
    ns: str = _TCX_NS,
    text: str | None = None,
    **attrib: str,
) -> ET.Element:
    e = ET.SubElement(parent, f"{{{ns}}}{tag}", attrib)
    if text is not None:
        e.text = text
    return e


def write_tcx(
    trackpoints: list[
        tuple[datetime.datetime, int, int, int, float]
    ],  # (ts, watts, hr, cadence, speed_kmh)
    activity_name: str,
    out_path: pathlib.Path,
) -> None:
    if not trackpoints:
        return

    start = trackpoints[0][0]
    end = trackpoints[-1][0]
    duration = (end - start).total_seconds()
    watts_list = [w for _, w, _, _, _ in trackpoints]
    hr_list = [h for _, _, h, _, _ in trackpoints if h > 0]
    cad_list = [c for _, _, _, c, _ in trackpoints if c > 0]
    avg_watts = int(sum(watts_list) / len(watts_list))
    max_watts = max(watts_list)
    avg_hr = int(sum(hr_list) / len(hr_list)) if hr_list else 0
    max_hr = max(hr_list) if hr_list else 0
    avg_cad = int(sum(cad_list) / len(cad_list)) if cad_list else 0
    max_cad = max(cad_list) if cad_list else 0

    # Accumulate distance from physics speed between trackpoints
    total_distance = 0.0
    distances: list[float] = [0.0]
    for i in range(1, len(trackpoints)):
        dt = (trackpoints[i][0] - trackpoints[i - 1][0]).total_seconds()
        spd_ms = trackpoints[i][4] / 3.6
        total_distance += spd_ms * dt
        distances.append(total_distance)

    root = ET.Element(
        f"{{{_TCX_NS}}}TrainingCenterDatabase",
        {
            f"{{{_XSI_NS}}}schemaLocation": (
                f"{_TCX_NS} "
                "https://www8.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd"
            )
        },
    )
    acts = _sub(root, "Activities")
    activity = _sub(acts, "Activity", Sport="Biking")
    _sub(activity, "Id", text=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(activity, "Notes", text=activity_name)

    lap = _sub(activity, "Lap", StartTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(lap, "TotalTimeSeconds", text=f"{duration:.1f}")
    _sub(lap, "DistanceMeters", text=f"{total_distance:.1f}")
    _sub(lap, "Calories", text="0")
    if avg_hr > 0:
        bpm_avg = _sub(lap, "AverageHeartRateBpm")
        _sub(bpm_avg, "Value", text=str(avg_hr))
        bpm_max = _sub(lap, "MaximumHeartRateBpm")
        _sub(bpm_max, "Value", text=str(max_hr))
    _sub(lap, "Intensity", text="Active")
    _sub(lap, "TriggerMethod", text="Manual")

    track = _sub(lap, "Track")
    for i, (ts, watts, hr, cadence, speed_kmh) in enumerate(trackpoints):
        tp = _sub(track, "Trackpoint")
        _sub(tp, "Time", text=ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _sub(tp, "DistanceMeters", text=f"{distances[i]:.1f}")
        if cadence > 0:
            _sub(tp, "Cadence", text=str(cadence))
        if hr > 0:
            hr_el = _sub(tp, "HeartRateBpm")
            _sub(hr_el, "Value", text=str(hr))
        ext_tp = _sub(tp, "Extensions")
        tpx = _sub(ext_tp, "TPX", ns=_EXT_NS)
        _sub(tpx, "Watts", ns=_EXT_NS, text=str(watts))
        if speed_kmh > 0:
            _sub(tpx, "Speed", ns=_EXT_NS, text=f"{speed_kmh / 3.6:.3f}")

    lap_ext = _sub(lap, "Extensions")
    lx = _sub(lap_ext, "LX", ns=_EXT_NS)
    _sub(lx, "AvgWatts", ns=_EXT_NS, text=str(avg_watts))
    _sub(lx, "MaxWatts", ns=_EXT_NS, text=str(max_watts))
    if avg_cad > 0:
        _sub(lx, "AvgCadence", ns=_EXT_NS, text=str(avg_cad))
        _sub(lx, "MaxCadence", ns=_EXT_NS, text=str(max_cad))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    with out_path.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)


# ── FTMS helpers ──────────────────────────────────────────────────────────────


async def ftms_request_control(client: BleakClient) -> None:
    await client.write_gatt_char(FTMS_CONTROL_POINT, bytearray([0x00]), response=True)


async def ftms_start(client: BleakClient) -> None:
    await client.write_gatt_char(FTMS_CONTROL_POINT, bytearray([0x07]), response=True)


async def ftms_set_power(client: BleakClient, watts: int) -> None:
    await client.write_gatt_char(
        FTMS_CONTROL_POINT, struct.pack("<Bh", 0x05, watts), response=True
    )


async def ftms_reset(client: BleakClient) -> None:
    """Opcode 0x01 — Reset: releases ERG control back to the trainer."""
    await client.write_gatt_char(FTMS_CONTROL_POINT, bytearray([0x01]), response=True)


# ── Pre-app BLE scan ──────────────────────────────────────────────────────────


def _pick_device_by_name(devices: list, name_hint: str) -> str | None:
    hint = name_hint.upper()
    for d in devices:
        if d.name and hint in d.name.upper():
            return d.address
    return None


def _pick_device_by_preferences(devices: list, preferences: list[str]) -> str | None:
    upper_names = [(d.name or "").upper() for d in devices]
    # Prefer exact matches first, then substring matches.
    for pref in preferences:
        pref_upper = pref.upper()
        for idx, d in enumerate(devices):
            if upper_names[idx] == pref_upper:
                return d.address
    for pref in preferences:
        pref_upper = pref.upper()
        for idx, d in enumerate(devices):
            if pref_upper in upper_names[idx]:
                return d.address
    return None


async def find_trainer() -> str | None:
    console.print(
        "[bold cyan]Scanning for trainer (FTMS / Cycling Power)...[/bold cyan]"
    )
    devices = await BleakScanner.discover(
        timeout=10.0,
        service_uuids=[FTMS_SERVICE, CYCLING_POWER_SERVICE],
    )
    if not devices:
        console.print("[red]No devices found. Make sure your trainer is on.[/red]")
        return None

    if len(devices) == 1:
        d = devices[0]
        console.print(f"[green]Found:[/green] {d.name} ({d.address})")
        return d.address

    trainer = _pick_device_by_preferences(devices, ["KICKR MOVE", "KICKR", "MOVE"])
    if trainer is not None:
        selected = next((d.name for d in devices if d.address == trainer), "trainer")
        console.print(f"[green]Auto-selected trainer:[/green] {selected}")
        return trainer

    console.print("\n[bold]Multiple devices found:[/bold]")
    for i, d in enumerate(devices):
        console.print(f"  [{i}] {d.name or 'Unknown'} ({d.address})")
    choice = int(input("Select device number: "))
    return devices[choice].address


async def find_hr_monitor() -> str | None:
    console.print("[bold magenta]Scanning for heart rate monitor...[/bold magenta]")
    devices = await BleakScanner.discover(
        timeout=10.0,
        service_uuids=[HR_SERVICE],
    )
    if not devices:
        console.print(
            "[yellow]No HR monitor found — continuing without heart rate.[/yellow]"
        )
        return None

    if len(devices) == 1:
        d = devices[0]
        console.print(f"[green]HR monitor:[/green] {d.name} ({d.address})")
        return d.address

    console.print("\n[bold]Multiple HR monitors found:[/bold]")
    for i, d in enumerate(devices):
        console.print(f"  [{i}] {d.name or 'Unknown'} ({d.address})")
    choice = int(input("Select HR device number: "))
    return devices[choice].address


async def find_cadence_sensor() -> str | None:
    """Try to find a dedicated cadence sensor. Return None if using trainer's built-in cadence."""
    console.print("[dim]Cadence will be read from the trainer's power sensor.[/dim]")
    return None


async def find_power_meter(primary_address: str | None = None) -> str | None:
    console.print("[bold cyan]Scanning for a secondary power source...[/bold cyan]")
    devices = await BleakScanner.discover(
        timeout=10.0,
        service_uuids=[CYCLING_POWER_SERVICE],
    )
    other = [d for d in devices if d.address != primary_address]
    if not other:
        return None

    if len(other) == 1:
        d = other[0]
        console.print(f"[green]Secondary power source found:[/green] {d.name or 'Unknown'} ({d.address})")
        return d.address

    stages = _pick_device_by_preferences(other, ["STAGES 3648", "STAGES"])
    if stages is not None:
        selected = next((d.name for d in other if d.address == stages), "Stages 3648")
        console.print(f"[green]Auto-selected secondary power source:[/green] {selected}")
        return stages

    console.print("\n[bold]Multiple secondary power sources found:[/bold]")
    for i, d in enumerate(other):
        console.print(f"  [{i}] {d.name or 'Unknown'} ({d.address})")
    choice = int(input("Select secondary power source number: "))
    return other[choice].address


# ── Textual app ───────────────────────────────────────────────────────────────


class PowerBar(Static):
    """Horizontal zone-colored power bar, fixed 0–500 W scale."""

    MAX_W = 500

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._watts: int = 0

    def set_watts(self, watts: int) -> None:
        self._watts = watts
        self.refresh()

    def render(self) -> str:
        width = max(1, self.size.width or 40)
        frac = max(0.0, min(1.0, self._watts / self.MAX_W))
        filled = int(frac * width)
        empty = width - filled
        color = _zone_hex(self._watts)
        return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"


class HistoryChart(Static):
    """Vertical-column zone-colored bar chart, fixed 0–500 W scale."""

    MAX_W = 500

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._data: list[int] = []

    def set_data(self, data: list[int]) -> None:
        self._data = data
        self.refresh()

    def render(self) -> str:
        width = max(1, self.size.width or 40)
        height = max(1, self.size.height or 8)
        data = self._data[-width:] if self._data else []
        pad = width - len(data)
        # Build grid: rows[row][col] — row 0 is top
        rows: list[list[str]] = []
        for row in range(height):
            # threshold: fraction of MAX_W that this row represents
            # row 0 (top) = high power, row height-1 (bottom) = low power
            threshold = (height - row) / height  # e.g. row 0 → 1.0, last row → 1/height
            line: list[str] = [" "] * pad
            for v in data:
                frac = max(0.0, min(1.0, v / self.MAX_W))
                if frac >= threshold:
                    color = _zone_hex(v)
                    line.append(f"[{color}]█[/{color}]")
                else:
                    line.append("[dim]░[/dim]")
            rows.append(line)
        return "\n".join("".join(row) for row in rows)


class WahooApp(App):
    CSS = """
    Screen {
        background: #0d0d0d;
    }

    /* ── Two-column main layout ── */
    #main {
        height: 1fr;
        padding: 1 2;
    }
    #left {
        width: 3fr;
        margin-right: 1;
        height: 1fr;
    }
    #right {
        width: 20;
        margin-left: 1;
        height: 1fr;
    }

    /* ── Generic tile ── */
    .tile {
        border: round #1e3a5f;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    .tile-title {
        width: 1fr;
        text-align: center;
        color: #666666;
        text-style: bold;
    }
    .tile-unit {
        width: 1fr;
        text-align: center;
        color: #555555;
    }

    /* ── Power tiles (three across) ── */
    #power-tiles {
        height: auto;
        margin-bottom: 1;
    }
    .power-tile {
        width: 1fr;
        border: round #1e3a5f;
        padding: 0 1;
        margin-right: 1;
        height: auto;
    }
    .power-tile:last-of-type {
        margin-right: 0;
    }
    Digits {
        width: 1fr;
    }

    /* ── Metrics tile: power bar + sparkline history ── */
    #metrics-tile {
        height: 1fr;
    }
    #metrics-tile PowerBar {
        height: 1;
        width: 1fr;
        margin-top: 1;
    }
    #metrics-tile HistoryChart {
        height: 1fr;
        width: 1fr;
        margin-top: 1;
    }

    /* ── Right-hand tiles: fill column proportionally ── */
    #hr-tile, #cad-tile, #spd-tile, #ftp-tile {
        height: 1fr;
    }
    #mode-tile {
        height: 3;
        content-align: center middle;
        border: round #1a5a6e;
        padding: 0 1;
        margin-bottom: 1;
    }
    #mode-label {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: #42c8f5;
    }
    #workout-tile {
        border: round #1a5a6e;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    #workout-name {
        text-style: bold;
        color: #42c8f5;
        width: 1fr;
    }
    #workout-interval, #workout-time {
        color: #888888;
        width: 1fr;
    }
    .workout-label-lg {
        text-style: bold;
        width: 200%;
    }
    #workout-tile ProgressBar {
        width: 1fr;
        margin-top: 1;
    }

    /* ── Controls ── */
    #controls {
        height: 4;
        align: center middle;
    }
    Button {
        width: 16;
        height: 3;
        margin: 0 1;
    }
    #btn_up          { background: #1a3a1a; color: #5af55a; border: tall #2e6e2e; }
    #btn_up:hover    { background: #2e6e2e; }
    #btn_pause       { background: #3a2e08; color: #f5c842; border: tall #6e5a14; }
    #btn_pause:hover { background: #6e5a14; }
    #btn_down        { background: #3a1a1a; color: #f55a5a; border: tall #6e2e2e; }
    #btn_down:hover  { background: #6e2e2e; }
    #btn_erg         { background: #0d2a3a; color: #42c8f5; border: tall #1a5a6e; }
    #btn_erg:hover   { background: #1a5a6e; }
    #btn_erg.erg_off         { background: #2a1a0d; color: #f5a342; border: tall #6e3a1a; }
    #btn_erg.erg_off:hover   { background: #6e3a1a; }
    """

    BINDINGS = [
        Binding("up", "power_up", "+10 W"),
        Binding("down", "power_down", "−10 W"),
        Binding("f", "ftp_up", "+5 FTP"),
        Binding("d", "ftp_down", "−5 FTP"),
        Binding("e", "toggle_erg", "ERG on/off"),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        address: str,
        workout: Workout | None = None,
        hr_address: str | None = None,
        cadence_address: str | None = None,
        ftp: int = FTP_DEFAULT,
        power_address: str | None = None,
    ) -> None:
        super().__init__()
        self._address = address
        self._hr_address = hr_address
        self._cadence_address = cadence_address
        self._power_address = power_address
        self._workout = workout
        self._ftp = ftp if ftp else FTP_DEFAULT
        self._client: BleakClient | None = None
        self._has_ftms = False
        self._power_meter_connected = False
        self._power_meter_last_watts = 0
        self._power_meter_last_seen: float | None = None
        self._trainer_power = 0
        self._power_meter_power = 0
        self._power_meter_smooth = 0
        self._power_meter_bias = 1.0
        self._trainer_raw = ""
        self._power_meter_raw = ""
        self._trainer_debug = ""
        self._power_meter_debug = ""
        self._target = (
            _power_from_ftp(workout.intervals[0].power, self._ftp) if workout else 200
        )
        self._actual = 0
        self._history: list[int] = []
        self._offset = 0
        self._status: WorkoutStatus | None = None
        self._done = False
        self._paused = False
        self._pause_start: float | None = time.monotonic()  # paused from the start
        self._paused_elapsed: float = 0.0
        self._auto_paused = True  # stay paused until first pedal stroke
        self._zero_since: float | None = None
        self._erg_enabled = True
        self._hr = 0
        self._cadence = 0
        self._cadence_seen = False
        # Crank tracking for power sensor
        self._last_crank_revs: int | None = None
        self._last_crank_event_time: int | None = None
        # Crank tracking for dedicated cadence sensor
        self._csc_last_crank_revs: int | None = None
        self._csc_last_crank_event_time: int | None = None
        self._session_start: datetime.datetime | None = None
        self._trackpoints: list[tuple[datetime.datetime, int, int, int, float]] = []

    def _handle_power_measurement(
        self,
        watts: int,
        crank_data: tuple[int, int] | None,
        source: str,
        raw_data: bytes,
    ) -> None:
        actual_value = watts
        if source == "power_meter":
            self._power_meter_connected = True
            self._power_meter_power = watts
            self._power_meter_last_watts = watts
            self._power_meter_last_seen = time.monotonic()
            self._power_meter_raw = raw_data.hex().upper()
            _, _, debug = parse_power_debug(bytearray(raw_data))
            self._power_meter_debug = (
                f"flags={debug['flags']} len={debug['length']} exp={debug['expected_length']} {debug['note']}"
            )
            if self._power_meter_smooth == 0:
                self._power_meter_smooth = watts
            else:
                self._power_meter_smooth = int(
                    self._power_meter_smooth * 0.85 + watts * 0.15
                )
            if self._trainer_power > 0 and watts > 0:
                ratio = self._trainer_power / watts
                self._power_meter_bias = (
                    self._power_meter_bias * 0.9 + ratio * 0.1
                )
            actual_value = int(self._power_meter_smooth * self._power_meter_bias)

        if source == "trainer":
            self._trainer_power = watts
            self._trainer_raw = raw_data.hex().upper()
            _, _, debug = parse_power_debug(bytearray(raw_data))
            self._trainer_debug = (
                f"flags={debug['flags']} len={debug['length']} exp={debug['expected_length']} {debug['note']}"
            )
            if self._power_meter_power > 0 and watts > 0:
                ratio = watts / self._power_meter_power
                self._power_meter_bias = (
                    self._power_meter_bias * 0.9 + ratio * 0.1
                )

        if source == "trainer" and self._power_address and self._power_meter_connected:
            if (
                self._power_meter_last_seen is not None
                and (time.monotonic() - self._power_meter_last_seen) < 5.0
                and self._power_meter_last_watts > 0
            ):
                return

        self._actual = actual_value
        if not self._is_paused:
            self._history.append(watts)
            if len(self._history) > 300:
                self._history.pop(0)

        if crank_data is not None:
            cumulative_crank_revs, last_crank_event_time = crank_data
            if (
                self._last_crank_revs is not None
                and self._last_crank_event_time is not None
            ):
                delta_revs = (cumulative_crank_revs - self._last_crank_revs) & 0xFFFF
                delta_time = (
                    last_crank_event_time - self._last_crank_event_time
                ) & 0xFFFF
                if delta_time > 0:
                    cadence = int(60.0 * delta_revs / (delta_time / 1024.0))
                    if 0 < cadence < 250:
                        self._cadence = cadence
                        self._cadence_seen = True
            self._last_crank_revs = cumulative_crank_revs
            self._last_crank_event_time = last_crank_event_time

        now = time.monotonic()
        if watts == 0:
            self._cadence = 0
            self._cadence_seen = False
            if self._zero_since is None:
                self._zero_since = now
            elif (
                not self._auto_paused
                and (now - self._zero_since) >= 3.0
            ):
                self._auto_paused = True
                if self._pause_start is None:
                    self._pause_start = now
        else:
            if self._auto_paused:
                if self._pause_start is not None:
                    self._paused_elapsed += now - self._pause_start
                    self._pause_start = None
                self._auto_paused = False
                self._zero_since = None
                self._send_power()
            else:
                self._zero_since = None

        if not self._is_paused:
            self._trackpoints.append(
                (
                    datetime.datetime.now(datetime.timezone.utc),
                    watts,
                    self._hr,
                    self._cadence,
                    physics_speed(watts),
                )
            )

    @property
    def _is_paused(self) -> bool:
        return self._paused or self._auto_paused

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="right"):
                with Vertical(id="hr-tile", classes="tile"):
                    yield Label("HEART RATE", classes="tile-title")
                    yield Digits("---", id="hr-val")
                    yield Label("bpm", classes="tile-unit")
                with Vertical(id="cad-tile", classes="tile"):
                    yield Label("CADENCE", classes="tile-title")
                    yield Digits("---", id="cadence-val")
                    yield Label("rpm", classes="tile-unit")
                with Vertical(id="spd-tile", classes="tile"):
                    yield Label("EST SPEED", classes="tile-title")
                    yield Digits("---", id="speed-val")
                    yield Label("km/h", classes="tile-unit")
                with Vertical(id="ftp-tile", classes="tile"):
                    yield Label("FTP", classes="tile-title")
                    yield Digits("---", id="ftp-val")
                    yield Label("W", classes="tile-unit")
                with Vertical(id="power-source-tile", classes="tile"):
                    yield Label("SOURCE POWER", classes="tile-title")
                    yield Label("Trainer", classes="tile-unit")
                    yield Digits("---", id="trainer-power-val")
                    yield Label("Secondary", classes="tile-unit")
                    yield Digits("---", id="power-meter-power-val")
                    yield Label("Bias", classes="tile-unit")
                    yield Label("---", id="power-bias-val")
                with Vertical(id="mode-tile"):
                    yield Label("⚡ ERG  ON", id="mode-label")
            with Vertical(id="left"):
                with Horizontal(id="power-tiles"):
                    with Vertical(id="target-tile", classes="power-tile"):
                        yield Label("TARGET", classes="tile-title")
                        yield Digits("---", id="target-val")
                        yield Label("W", classes="tile-unit")
                    with Vertical(id="actual-tile", classes="power-tile"):
                        yield Label("ACTUAL", classes="tile-title")
                        yield Digits("---", id="actual-val")
                        yield Label("W", classes="tile-unit")
                with Vertical(id="metrics-tile", classes="tile"):
                    yield Label("POWER", classes="tile-title")
                    yield PowerBar(id="power-bar")
                    yield HistoryChart(id="history-chart")
                with Vertical(id="workout-tile"):
                    yield Label("", id="workout-name", classes="workout-label-lg")
                    yield Label("", id="workout-interval", classes="workout-label-lg")
                    yield Digits("", id="workout-time")
                    yield ProgressBar(
                        total=100,
                        show_eta=False,
                        show_percentage=False,
                        id="workout-bar",
                    )
        with Horizontal(id="controls"):
            yield Button("▲  +10 W", id="btn_up")
            yield Button("⏸  Pause", id="btn_pause")
            yield Button("▼  −10 W", id="btn_down")
            yield Button("FTP +", id="btn_ftp_up")
            yield Button("FTP -", id="btn_ftp_down")
            yield Button("⚡ ERG  ON", id="btn_erg")
        yield Footer()

    async def on_mount(self) -> None:
        self._quit_event = asyncio.Event()
        asyncio.create_task(self._ble_loop())
        if self._hr_address:
            asyncio.create_task(self._hr_loop())
        if self._cadence_address:
            asyncio.create_task(self._cadence_loop())
        if self._power_address:
            asyncio.create_task(self._power_loop())
        self.set_interval(0.25, self._refresh)

    def _refresh(self) -> None:
        spd = physics_speed(self._actual)
        diff = self._actual - self._target
        t_hex = _zone_hex(self._target)
        t_zone, _ = _zone(self._target)

        # Power tiles
        td = self.query_one("#target-val", Digits)
        td.update(str(self._target))
        td.styles.color = t_hex

        ad = self.query_one("#actual-val", Digits)
        ad.update(str(self._actual))
        ad.styles.color = (
            "#66bb6a"
            if abs(diff) <= 10
            else "#ffd54f"
            if abs(diff) <= 30
            else "#ef5350"
        )

        # Power bar
        self.query_one("#power-bar", PowerBar).set_watts(self._actual)

        # History chart
        self.query_one("#history-chart", HistoryChart).set_data(list(self._history))

        # HR
        hd = self.query_one("#hr-val", Digits)
        hd.update(str(self._hr) if self._hr > 0 else "---")
        hd.styles.color = _hr_hex(self._hr)

        # Cadence
        self.query_one("#cadence-val", Digits).update(
            str(self._cadence) if self._cadence > 0 else "---"
        )

        # Speed
        self.query_one("#speed-val", Digits).update(f"{spd:.1f}" if spd > 0 else "---")

        # FTP display
        self.query_one("#ftp-val", Digits).update(str(self._ftp))

        # Power source display
        self.query_one("#trainer-power-val", Digits).update(
            str(self._trainer_power) if self._trainer_power > 0 else "---"
        )
        self.query_one("#power-meter-power-val", Digits).update(
            str(self._power_meter_power) if self._power_meter_power > 0 else "---"
        )
        self.query_one("#power-bias-val", Label).update(
            f"{self._power_meter_bias:.3f}"
            if self._power_meter_connected
            else "---"
        )

        # ERG mode tile
        mode_lbl = self.query_one("#mode-label", Label)
        mode_tile = self.query_one("#mode-tile")
        if self._erg_enabled:
            mode_lbl.update("⚡ ERG  ON")
            mode_tile.styles.border = ("round", "#1a5a6e")
            mode_lbl.styles.color = "#42c8f5"
        else:
            mode_lbl.update("⚡ ERG OFF")
            mode_tile.styles.border = ("round", "#6e3a1a")
            mode_lbl.styles.color = "#f5a342"

        # Workout status
        workout_tile = self.query_one("#workout-tile")
        if self._status is not None:
            workout_tile.display = True
            remaining = self._status.interval_dur - self._status.elapsed_in
            iv_suffix = "  ramp" if self._status.ramp else ""
            self.query_one("#workout-name", Label).update(self._status.name)
            self.query_one("#workout-interval", Label).update(
                f"Interval {self._status.interval_idx + 1}/{self._status.total_intervals}{iv_suffix}"
            )
            self.query_one("#workout-time", Digits).update(
                f"Left {_fmt_time(remaining)}  ·  "
                f"{_fmt_time(self._status.total_elapsed)} / {_fmt_time(self._status.total_dur)}"
            )
            self.query_one("#workout-bar", ProgressBar).update(
                progress=self._status.elapsed_in, total=self._status.interval_dur
            )
        else:
            workout_tile.display = False

        # App title bar
        if self._is_paused:
            self.title = "⏸  WAHOO ERG — PAUSED"
            self.sub_title = ""
        elif self._done:
            self.title = "✓  WAHOO ERG — COMPLETE"
            self.sub_title = ""
        else:
            self.title = "⚡  WAHOO ERG"
            self.sub_title = f"Target {self._target} W  ·  {t_zone}"

    # ── Actions ───────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn_up":
                self.action_power_up()
            case "btn_down":
                self.action_power_down()
            case "btn_pause":
                self.action_toggle_pause()
            case "btn_ftp_up":
                self.action_ftp_up()
            case "btn_ftp_down":
                self.action_ftp_down()
            case "btn_erg":
                self.action_toggle_erg()

    def action_power_up(self) -> None:
        if self._workout:
            self._offset = min(MAX_POWER, self._offset + POWER_STEP)
        else:
            self._target = min(MAX_POWER, self._target + POWER_STEP)
            self._send_power()

    def action_power_down(self) -> None:
        if self._workout:
            self._offset = max(-MAX_POWER, self._offset - POWER_STEP)
        else:
            self._target = max(MIN_POWER, self._target - POWER_STEP)
            self._send_power()

    def action_toggle_pause(self) -> None:
        btn = self.query_one("#btn_pause", Button)
        if not self._paused:
            self._paused = True
            self._pause_start = time.monotonic()
            btn.label = "▶  Resume"
        else:
            if self._pause_start is not None:
                self._paused_elapsed += time.monotonic() - self._pause_start
                self._pause_start = None
            self._paused = False
            btn.label = "⏸  Pause"
            if not self._auto_paused:
                self._send_power()

    def action_quit(self) -> None:
        self._save_tcx()
        if hasattr(self, "_quit_event"):
            self._quit_event.set()
        self.exit()

    def action_toggle_erg(self) -> None:
        btn = self.query_one("#btn_erg", Button)
        self._erg_enabled = not self._erg_enabled
        if self._erg_enabled:
            btn.label = "⚡ ERG  ON"
            btn.remove_class("erg_off")
            self._send_power()
        else:
            btn.label = "⚡ ERG OFF"
            btn.add_class("erg_off")
            if self._client and self._has_ftms:
                asyncio.create_task(ftms_reset(self._client))

    def action_ftp_up(self) -> None:
        self._ftp = min(500, self._ftp + 5)
        if self._workout:
            self._workout.ftp = self._ftp
            power, status = workout_target(
                self._workout, self._status.total_elapsed if self._status else 0.0
            )
            self._status = status
            self._target = power
            self._send_power()

    def action_ftp_down(self) -> None:
        self._ftp = max(50, self._ftp - 5)
        if self._workout:
            self._workout.ftp = self._ftp
            power, status = workout_target(
                self._workout, self._status.total_elapsed if self._status else 0.0
            )
            self._status = status
            self._target = power
            self._send_power()

    def _save_tcx(self) -> None:
        if not self._trackpoints or self._session_start is None:
            return
        name = (self._workout.name if self._workout else "Free Ride").replace(" ", "_")
        ts = self._session_start.strftime("%Y%m%dT%H%M%S")
        out = pathlib.Path("recordings") / f"{ts}_{name}.tcx"
        write_tcx(self._trackpoints, name.replace("_", " "), out)
        console.print(f"[green]Activity saved →[/green] {out}")

    def _send_power(self) -> None:
        if (
            self._client
            and self._has_ftms
            and not self._is_paused
            and self._erg_enabled
        ):
            asyncio.create_task(ftms_set_power(self._client, self._target))

    # ── BLE background tasks ──────────────────────────────────────────────────

    async def _ble_loop(self) -> None:
        self._session_start = datetime.datetime.now(datetime.timezone.utc)
        _AUTO_PAUSE_GRACE = 3.0  # seconds of zero power before auto-pausing

        def on_measurement(char: BleakGATTCharacteristic, data: bytearray) -> None:
            watts, crank_data = parse_power(data)
            self._handle_power_measurement(
                watts, crank_data, source="trainer", raw_data=bytes(data)
            )

        async with BleakClient(self._address) as client:
            self._client = client
            svc_uuids = {s.uuid.lower() for s in client.services}
            self._has_ftms = FTMS_SERVICE in svc_uuids
            if self._has_ftms:
                try:
                    await ftms_request_control(client)
                    await ftms_start(client)
                except Exception:
                    self._has_ftms = False

            await client.start_notify(CYCLING_POWER_MEASUREMENT, on_measurement)

            # Subscribe to FTMS Indoor Bike Data for speed and cadence (KICKR FLOW)
            if self._has_ftms:
                char_uuids = {
                    c.uuid.lower() for s in client.services for c in s.characteristics
                }
                if FTMS_INDOOR_BIKE_DATA in char_uuids:

                    def on_indoor_bike(
                        char: BleakGATTCharacteristic, data: bytearray
                    ) -> None:
                        _, cad = parse_indoor_bike_data(data)
                        if cad is not None and 0 < cad < 250 and self._actual > 0:
                            self._cadence = cad
                            self._cadence_seen = True

                    await client.start_notify(FTMS_INDOOR_BIKE_DATA, on_indoor_bike)

            if self._workout:
                await self._workout_loop(client)
            else:
                await self._quit_event.wait()

            await client.stop_notify(CYCLING_POWER_MEASUREMENT)

    async def _hr_loop(self) -> None:
        assert self._hr_address is not None
        try:
            async with BleakClient(self._hr_address) as client:

                def on_hr(char: BleakGATTCharacteristic, data: bytearray) -> None:
                    self._hr = parse_hr(data)

                await client.start_notify(HR_MEASUREMENT, on_hr)
                await self._quit_event.wait()
                await client.stop_notify(HR_MEASUREMENT)
        except Exception:
            pass  # HR is optional; don't crash the app

    async def _power_loop(self) -> None:
        assert self._power_address is not None
        self._power_meter_connected = False
        try:
            console.print(
                f"[dim]Connecting to secondary power source at {self._power_address}...[/dim]"
            )
            async with BleakClient(self._power_address) as client:

                def on_measurement(
                    char: BleakGATTCharacteristic, data: bytearray
                ) -> None:
                    watts, crank_data = parse_power(data)
                    self._handle_power_measurement(
                        watts,
                        crank_data,
                        source="power_meter",
                        raw_data=bytes(data),
                    )

                await client.start_notify(CYCLING_POWER_MEASUREMENT, on_measurement)
                self._power_meter_connected = True
                console.print(f"[green]Secondary power source connected![/green]")
                await self._quit_event.wait()
                await client.stop_notify(CYCLING_POWER_MEASUREMENT)
        except Exception as e:
            console.print(f"[yellow]Secondary power source connection failed: {e}[/yellow]")
        finally:
            self._power_meter_connected = False

    async def _cadence_loop(self) -> None:
        assert self._cadence_address is not None
        try:
            console.print(
                f"[dim]Connecting to cadence sensor at {self._cadence_address}...[/dim]"
            )
            async with BleakClient(self._cadence_address) as client:

                def on_cadence(char: BleakGATTCharacteristic, data: bytearray) -> None:
                    crank_data = parse_csc(data)
                    if crank_data is not None:
                        cumulative_crank_revs, last_crank_event_time = crank_data
                        if (
                            self._csc_last_crank_revs is not None
                            and self._csc_last_crank_event_time is not None
                        ):
                            delta_revs = (
                                cumulative_crank_revs - self._csc_last_crank_revs
                            ) & 0xFFFF
                            delta_time = (
                                last_crank_event_time - self._csc_last_crank_event_time
                            ) & 0xFFFF
                            if delta_time > 0:
                                self._cadence = int(
                                    60.0 * delta_revs / (delta_time / 1024.0)
                                )
                        self._csc_last_crank_revs = cumulative_crank_revs
                        self._csc_last_crank_event_time = last_crank_event_time

                try:
                    await client.start_notify(CSC_MEASUREMENT, on_cadence)
                    console.print(f"[green]Cadence sensor connected![/green]")
                    await self._quit_event.wait()
                    await client.stop_notify(CSC_MEASUREMENT)
                except Exception as notify_err:
                    console.print(
                        f"[yellow]Cannot find CSC_MEASUREMENT characteristic: {notify_err}[/yellow]"
                    )
        except Exception as e:
            console.print(f"[yellow]Cadence sensor connection failed: {e}[/yellow]")

    async def _workout_loop(self, client: BleakClient) -> None:
        assert self._workout is not None
        start = time.monotonic()
        while not self._quit_event.is_set():
            now = time.monotonic()
            elapsed = now - start - self._paused_elapsed
            if self._is_paused and self._pause_start is not None:
                elapsed -= now - self._pause_start

            if elapsed >= self._workout.total_duration:
                _, final_status = workout_target(
                    self._workout, self._workout.total_duration - 0.001
                )
                final_status.done = True
                self._status = final_status
                self._done = True
                await self._quit_event.wait()
                return

            if not self._is_paused:
                power, status = workout_target(self._workout, elapsed)
                self._status = status
                new_target = max(MIN_POWER, min(MAX_POWER, power + self._offset))
                if new_target != self._target:
                    self._target = new_target
                    if self._has_ftms and self._erg_enabled:
                        await ftms_set_power(client, self._target)

            await asyncio.sleep(1.0)


# ── Entry point ───────────────────────────────────────────────────────────────


def _pick_workout_file() -> str:
    """Open a native file dialog in a child process to avoid tainting COM apartment.

    Works both from source (sys.executable = python.exe) and as a frozen
    PyInstaller .exe (sys.executable = wahoo-power.exe).  In the frozen case
    we pass --_filepicker so the subprocess enters dialog-only mode and exits
    immediately instead of running the full app again.
    """
    frozen = getattr(sys, "frozen", False)
    if frozen:
        cmd = [sys.executable, "--_filepicker"]
    else:
        script = (
            "import tkinter as tk, tkinter.filedialog as fd, pathlib;\n"
            "r=tk.Tk(); r.withdraw(); r.wm_attributes('-topmost', True);\n"
            "p=fd.askopenfilename("
            "title='Select a workout file (cancel for free ride)',"
            f"initialdir=r'{pathlib.Path('workouts').resolve()}',"
            "filetypes=[('Workout JSON','*.json'),('All files','*.*')]);\n"
            "print(p or '')"
        )
        cmd = [sys.executable, "-c", script]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def _filepicker_mode() -> None:
    """Dialog-only entry point used by the frozen exe subprocess."""
    import tkinter as tk
    import tkinter.filedialog as fd

    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    path = fd.askopenfilename(
        title="Select a workout file (cancel for free ride)",
        initialdir=str(pathlib.Path("workouts").resolve()),
        filetypes=[("Workout JSON", "*.json"), ("All files", "*.*")],
    )
    print(path or "")
    root.destroy()


def main() -> None:
    # frozen subprocess spawned just to show the file dialog
    if "--_filepicker" in sys.argv:
        _filepicker_mode()
        return

    workout: Workout | None = None
    ftp = FTP_DEFAULT
    args = sys.argv[1:]

    if "--workout" in args:
        idx = args.index("--workout")
        if idx + 1 >= len(args):
            console.print("[red]--workout requires a file path argument[/red]")
            return
        workout = load_workout(args[idx + 1])
    else:
        path = _pick_workout_file()
        if path:
            workout = load_workout(path)

    if "--ftp" in args:
        idx = args.index("--ftp")
        if idx + 1 < len(args):
            ftp = int(args[idx + 1])

    if workout:
        console.print(
            f"[bold]Workout:[/bold] {workout.name}  "
            f"([dim]{len(workout.intervals)} intervals · {_fmt_time(workout.total_duration)}[/dim])"
        )

    address = asyncio.run(find_trainer())
    if address:
        hr_address = asyncio.run(find_hr_monitor())
        cadence_address = asyncio.run(find_cadence_sensor())
        power_address = asyncio.run(find_power_meter(address))
        WahooApp(
            address,
            workout,
            hr_address,
            cadence_address,
            ftp,
            power_address,
        ).run()


if __name__ == "__main__":
    main()
