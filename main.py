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
from rich.panel import Panel
from rich.table import Table
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Static

# ── BLE service / characteristic UUIDs ────────────────────────────────────────
CYCLING_POWER_SERVICE            = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT        = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_SPEED_CADENCE_SERVICE    = "00001816-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT                  = "00002a5b-0000-1000-8000-00805f9b34fb"
FTMS_SERVICE                     = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT               = "00002ad9-0000-1000-8000-00805f9b34fb"
HR_SERVICE                = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT            = "00002a37-0000-1000-8000-00805f9b34fb"

# ── ERG settings ──────────────────────────────────────────────────────────────
POWER_STEP = 10
MIN_POWER  = 0
MAX_POWER  = 1000

console = Console()

# ── Power zones ───────────────────────────────────────────────────────────────
_ZONES = [
    (100,  "Z1", "grey62"),
    (160,  "Z2", "steel_blue1"),
    (230,  "Z3", "green3"),
    (300,  "Z4", "yellow3"),
    (375,  "Z5", "dark_orange"),
    (450,  "Z6", "red1"),
    (9999, "Z7", "magenta"),
]
_SPARK     = "▁▂▃▄▅▆▇█"
_BAR_WIDTH = 24

_BIG_DIGITS = {
    "0": ["███", "█ █", "█ █", "█ █", "███"],
    "1": [" ██", "  █", "  █", "  █", "███"],
    "2": ["███", "  █", "███", "█  ", "███"],
    "3": ["███", "  █", "███", "  █", "███"],
    "4": ["█ █", "█ █", "███", "  █", "  █"],
    "5": ["███", "█  ", "███", "  █", "███"],
    "6": ["███", "█  ", "███", "█ █", "███"],
    "7": ["███", "  █", "  █", "  █", "  █"],
    "8": ["███", "█ █", "███", "█ █", "███"],
    "9": ["███", "█ █", "███", "  █", "███"],
    "-": ["   ", "   ", "███", "   ", "   "],
    "+": ["   ", " █ ", "███", " █ ", "   "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def _big_digits(text: str, style: str = "white") -> str:
    rows = ["" for _ in range(5)]
    for ch in text:
        pattern = _BIG_DIGITS.get(ch, _BIG_DIGITS[" "])
        for i, line in enumerate(pattern):
            rows[i] += f"[{style}]{line}[/] "
    return "\n".join(rows).rstrip()


def parse_power(data: bytearray) -> tuple[int, tuple[int, int] | None]:
    if len(data) < 4:
        return 0, None
    flags = int.from_bytes(data[0:2], "little")
    power, = struct.unpack_from("<h", data, 2)
    offset = 4
    cadence_info: tuple[int, int] | None = None

    if flags & 0x01:
        offset += 1
    if flags & 0x02:
        offset += 2
    if flags & 0x04:
        if len(data) >= offset + 6:
            offset += 6
        else:
            return power, None
    if flags & 0x08:
        if len(data) >= offset + 4:
            cumulative_crank_revs = struct.unpack_from("<H", data, offset)[0]
            last_crank_event_time = struct.unpack_from("<H", data, offset + 2)[0]
            cadence_info = (cumulative_crank_revs, last_crank_event_time)

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


def _hr_color(bpm: int) -> str:
    if bpm <= 0:   return "dim white"
    if bpm < 100:  return "grey62"
    if bpm < 130:  return "steel_blue1"
    if bpm < 155:  return "green3"
    if bpm < 170:  return "yellow3"
    return "red1"


def _bar(watts: int, color: str) -> str:
    filled = min(_BAR_WIDTH, int(_BAR_WIDTH * watts / MAX_POWER))
    empty  = _BAR_WIDTH - filled
    return f"[{color}]{'█' * filled}[/{color}][dim white]{'░' * empty}[/dim white]"


def _sparkline(history: list[int]) -> str:
    if not history:
        return "[dim]no data yet…[/dim]"
    peak = max(max(history), 1)
    parts: list[str] = []
    for v in history:
        _, c = _zone(v)
        i = min(7, int(8 * v / peak))
        parts.append(f"[{c}]{_SPARK[i]}[/{c}]")
    return "".join(parts)


def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


# ── Workout plan ──────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Interval:
    duration: int
    power:    int
    ramp:     bool = False


@dataclasses.dataclass
class Workout:
    name:      str
    intervals: list[Interval]

    @property
    def total_duration(self) -> int:
        return sum(iv.duration for iv in self.intervals)


@dataclasses.dataclass
class WorkoutStatus:
    name:            str
    interval_idx:    int
    total_intervals: int
    elapsed_in:      float
    interval_dur:    float
    total_elapsed:   float
    total_dur:       int
    ramp:            bool
    done:            bool = False


def load_workout(path: str) -> Workout:
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    intervals = [
        Interval(
            duration=int(iv["duration"]),
            power=int(iv["power"]),
            ramp=iv.get("type", "steady") == "ramp",
        )
        for iv in raw["intervals"]
    ]
    return Workout(name=raw.get("name", "Workout"), intervals=intervals)


def workout_target(workout: Workout, elapsed: float) -> tuple[int, WorkoutStatus]:
    t          = 0.0
    prev_power = workout.intervals[0].power
    for idx, interval in enumerate(workout.intervals):
        end = t + interval.duration
        if elapsed < end or idx == len(workout.intervals) - 1:
            elapsed_in = min(elapsed - t, float(interval.duration))
            if interval.ramp and idx > 0:
                frac  = elapsed_in / interval.duration
                power = int(prev_power + (interval.power - prev_power) * min(frac, 1.0))
            else:
                power = interval.power
            return power, WorkoutStatus(
                name=workout.name,
                interval_idx=idx,
                total_intervals=len(workout.intervals),
                elapsed_in=elapsed_in,
                interval_dur=float(interval.duration),
                total_elapsed=elapsed,
                total_dur=workout.total_duration,
                ramp=interval.ramp and idx > 0,
            )
        prev_power = interval.power
        t = end
    last = workout.intervals[-1]
    return last.power, WorkoutStatus(
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


# ── Rich panel renderer ───────────────────────────────────────────────────────

def make_panel(
    actual:       int,
    target:       int,
    history:      list[int],
    status:       WorkoutStatus | None = None,
    workout_done: bool = False,
    paused:       bool = False,
    hr:           int  = 0,
    cadence:      int  = 0,
    erg_enabled:  bool = True,
) -> Panel:
    t_zone, t_color = _zone(target)
    a_zone, a_color = _zone(actual)
    diff       = actual - target
    diff_color = "green3" if abs(diff) < 10 else "yellow3" if abs(diff) < 30 else "red1"
    hc         = _hr_color(hr)

    target_panel = Panel(
        f"[dim]{t_zone}[/dim]\n[bold {t_color}]{target} W[/bold {t_color}]",
        title="TARGET",
        border_style=t_color,
        padding=(0, 2),
        expand=True,
    )

    actual_panel = Panel(
        f"[dim]{a_zone}[/dim]\n[bold {a_color}]{actual} W[/bold {a_color}]",
        title="ACTUAL",
        border_style=a_color,
        padding=(0, 2),
        expand=True,
    )

    diff_panel = Panel(
        f"[bold {diff_color}]{diff:+} W[/bold {diff_color}]",
        title="DIFF",
        border_style=diff_color,
        padding=(0, 2),
        expand=True,
    )

    heart_panel = Panel(
        f"[bold {hc}]{hr if hr > 0 else '---'} bpm[/bold {hc}]",
        title="HEART",
        border_style=hc,
        padding=(0, 2),
        expand=True,
    )

    cadence_panel = Panel(
        f"[bold magenta]{cadence if cadence > 0 else '---'} rpm[/bold magenta]",
        title="CADENCE",
        border_style="magenta",
        padding=(0, 2),
        expand=True,
    )

    mode_panel = Panel(
        "[bold cyan]ERG ON[/bold cyan]" if erg_enabled else "[bold yellow]ERG OFF[/bold yellow]",
        title="MODE",
        border_style="cyan" if erg_enabled else "yellow",
        padding=(0, 2),
        expand=True,
    )

    left = Table.grid(padding=(0, 1))
    left.add_row(target_panel)
    left.add_row(actual_panel)
    left.add_row(diff_panel)
    left.add_row(Panel(_bar(actual, a_color), title="POWER BAR", border_style=a_color, padding=(0, 1), expand=True))
    left.add_row(Panel(_sparkline(history), title="HISTORY", border_style="grey50", padding=(0, 1), expand=True))

    right = Table.grid(padding=(0, 1))
    right.add_row(heart_panel)
    right.add_row(cadence_panel)
    right.add_row(mode_panel)

    if status is not None:
        pct       = status.elapsed_in / max(status.interval_dur, 1)
        filled    = int(_BAR_WIDTH * pct)
        prog      = (f"[cyan]{'█' * filled}[/cyan]"
                     f"[dim white]{'░' * (_BAR_WIDTH - filled)}[/dim white]")
        iv_type   = "  [cyan]ramp[/cyan]" if status.ramp else ""
        remaining = status.interval_dur - status.elapsed_in
        total_rem = status.total_dur - status.total_elapsed

        status_panel = Panel(
            "\n".join([
                f"[bold]{status.name}[/bold]",
                f"[dim]Interval[/dim] [bold]{status.interval_idx + 1}[/bold] [dim]/ {status.total_intervals}[/dim]{iv_type}",
                f"[dim]Time left[/dim] [bold]{_fmt_time(remaining)}[/bold]",
                f"[dim]Session[/dim] [bold]{_fmt_time(status.total_elapsed)}[/bold] [dim]/ {_fmt_time(status.total_dur)}[/dim]",
                "",
                prog,
            ]),
            title="WORKOUT",
            border_style="cyan",
            padding=(0, 1),
            expand=True,
        )
        right.add_row(status_panel)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(ratio=3)
    grid.add_column(ratio=2, min_width=24)
    grid.add_row(left, right)

    if paused:
        title  = "[bold yellow]⏸  WAHOO ERG — PAUSED[/bold yellow]"
        border = "yellow"
    elif workout_done:
        title  = "[bold green]✓  WAHOO ERG — COMPLETE[/bold green]"
        border = "green"
    else:
        title  = "[bold cyan]⚡  WAHOO ERG  ⚡[/bold cyan]"
        border = "cyan"

    return Panel(grid, title=title, border_style=border, expand=True, padding=(1, 2))


# ── TCX export ───────────────────────────────────────────────────────────────

_TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
_EXT_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("",    _TCX_NS)
ET.register_namespace("ext", _EXT_NS)
ET.register_namespace("xsi", _XSI_NS)


def _sub(parent: ET.Element, tag: str, ns: str = _TCX_NS,
         text: str | None = None, **attrib: str) -> ET.Element:
    e = ET.SubElement(parent, f"{{{ns}}}{tag}", attrib)
    if text is not None:
        e.text = text
    return e


def write_tcx(
    trackpoints: list[tuple[datetime.datetime, int, int]],  # (ts, watts, hr)
    activity_name: str,
    out_path: pathlib.Path,
) -> None:
    if not trackpoints:
        return

    start      = trackpoints[0][0]
    end        = trackpoints[-1][0]
    duration   = (end - start).total_seconds()
    watts_list = [w for _, w, _ in trackpoints]
    hr_list    = [h for _, _, h in trackpoints if h > 0]
    avg_watts  = int(sum(watts_list) / len(watts_list))
    max_watts  = max(watts_list)
    avg_hr     = int(sum(hr_list) / len(hr_list)) if hr_list else 0
    max_hr     = max(hr_list) if hr_list else 0

    root = ET.Element(
        f"{{{_TCX_NS}}}TrainingCenterDatabase",
        {
            f"{{{_XSI_NS}}}schemaLocation": (
                f"{_TCX_NS} "
                "https://www8.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd"
            )
        },
    )
    acts     = _sub(root, "Activities")
    activity = _sub(acts, "Activity", Sport="Biking")
    _sub(activity, "Id", text=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(activity, "Notes", text=activity_name)

    lap = _sub(activity, "Lap", StartTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _sub(lap, "TotalTimeSeconds", text=f"{duration:.1f}")
    _sub(lap, "DistanceMeters",   text="0")
    _sub(lap, "Calories",         text="0")
    if avg_hr > 0:
        bpm_avg = _sub(lap, "AverageHeartRateBpm")
        _sub(bpm_avg, "Value", text=str(avg_hr))
        bpm_max = _sub(lap, "MaximumHeartRateBpm")
        _sub(bpm_max, "Value", text=str(max_hr))
    _sub(lap, "Intensity",        text="Active")
    _sub(lap, "TriggerMethod",    text="Manual")

    track = _sub(lap, "Track")
    for ts, watts, hr in trackpoints:
        tp = _sub(track, "Trackpoint")
        _sub(tp, "Time", text=ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if hr > 0:
            hr_el = _sub(tp, "HeartRateBpm")
            _sub(hr_el, "Value", text=str(hr))
        ext_tp = _sub(tp, "Extensions")
        tpx    = _sub(ext_tp, "TPX", ns=_EXT_NS)
        _sub(tpx, "Watts", ns=_EXT_NS, text=str(watts))

    lap_ext = _sub(lap, "Extensions")
    lx      = _sub(lap_ext, "LX", ns=_EXT_NS)
    _sub(lx, "AvgWatts", ns=_EXT_NS, text=str(avg_watts))
    _sub(lx, "MaxWatts", ns=_EXT_NS, text=str(max_watts))

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

async def find_trainer() -> str | None:
    console.print("[bold cyan]Scanning for trainer (FTMS / Cycling Power)...[/bold cyan]")
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
        console.print("[yellow]No HR monitor found — continuing without heart rate.[/yellow]")
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


# ── Textual app ───────────────────────────────────────────────────────────────

class PowerDisplay(Static):
    """Static widget that renders the Rich power panel."""


class WahooApp(App):
    CSS = """
    Screen {
        background: #0d0d0d;
        align: center top;
    }
    PowerDisplay {
        width: 68;
        height: auto;
        margin: 2 0 0 0;
        align: center middle;
    }
    #controls {
        width: 68;
        height: auto;
        margin: 2 0 0 0;
        align: center middle;
    }
    Button {
        width: 14;
        height: 3;
        margin: 0 1;
        content-align: center middle;
    }
    #btn_up {
        background: #1a3a1a;
        color: #5af55a;
        border: tall #2e6e2e;
    }
    #btn_up:hover {
        background: #2e6e2e;
    }
    #btn_pause {
        background: #3a2e08;
        color: #f5c842;
        border: tall #6e5a14;
    }
    #btn_pause:hover {
        background: #6e5a14;
    }
    #btn_down {
        background: #3a1a1a;
        color: #f55a5a;
        border: tall #6e2e2e;
    }
    #btn_down:hover {
        background: #6e2e2e;
    }
    #btn_erg {
        background: #0d2a3a;
        color: #42c8f5;
        border: tall #1a5a6e;
    }
    #btn_erg:hover {
        background: #1a5a6e;
    }
    #btn_erg.erg_off {
        background: #2a1a0d;
        color: #f5a342;
        border: tall #6e3a1a;
    }
    #btn_erg.erg_off:hover {
        background: #6e3a1a;
    }
    """

    BINDINGS = [
        Binding("up",    "power_up",     "+10 W"),
        Binding("down",  "power_down",   "−10 W"),
        Binding("e",     "toggle_erg",   "ERG on/off"),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("q",     "quit",         "Quit"),
    ]

    def __init__(self, address: str, workout: Workout | None = None, hr_address: str | None = None, cadence_address: str | None = None) -> None:
        super().__init__()
        self._address         = address
        self._hr_address      = hr_address
        self._cadence_address = cadence_address
        self._workout         = workout
        self._client: BleakClient | None = None
        self._has_ftms        = False
        self._target          = workout.intervals[0].power if workout else 200
        self._actual          = 0
        self._history:  list[int]              = []
        self._offset          = 0
        self._status:   WorkoutStatus | None   = None
        self._done            = False
        self._paused          = False
        self._pause_start:    float | None    = time.monotonic()  # paused from the start
        self._paused_elapsed: float           = 0.0
        self._auto_paused     = True   # stay paused until first pedal stroke
        self._zero_since:     float | None    = None
        self._erg_enabled     = True
        self._hr              = 0
        self._cadence         = 0
        # Crank tracking for power sensor
        self._last_crank_revs: int | None         = None
        self._last_crank_event_time: int | None   = None
        # Crank tracking for dedicated cadence sensor
        self._csc_last_crank_revs: int | None     = None
        self._csc_last_crank_event_time: int | None = None
        self._session_start:  datetime.datetime | None                    = None
        self._trackpoints:    list[tuple[datetime.datetime, int, int]]    = []

    @property
    def _is_paused(self) -> bool:
        return self._paused or self._auto_paused

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PowerDisplay(id="display")
        with Horizontal(id="controls"):
            yield Button("▲  +10 W",   id="btn_up")
            yield Button("⏸  Pause",  id="btn_pause")
            yield Button("▼  −10 W",   id="btn_down")
            yield Button("⚡ ERG  ON", id="btn_erg")
        yield Footer()

    async def on_mount(self) -> None:
        self._quit_event = asyncio.Event()
        asyncio.create_task(self._ble_loop())
        if self._hr_address:
            asyncio.create_task(self._hr_loop())
        if self._cadence_address:
            asyncio.create_task(self._cadence_loop())
        self.set_interval(0.25, self._refresh)

    def _refresh(self) -> None:
        self.query_one("#display", PowerDisplay).update(
            make_panel(
                self._actual,
                self._target,
                self._history,
                self._status,
                self._done,
                self._is_paused,
                self._hr,
                self._cadence,
                self._erg_enabled,
            )
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn_up":    self.action_power_up()
            case "btn_down":  self.action_power_down()
            case "btn_pause": self.action_toggle_pause()
            case "btn_erg":   self.action_toggle_erg()

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
            self._paused      = True
            self._pause_start = time.monotonic()
            btn.label         = "▶  Resume"
        else:
            if self._pause_start is not None:
                self._paused_elapsed += time.monotonic() - self._pause_start
                self._pause_start = None
            self._paused = False
            btn.label    = "⏸  Pause"
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

    def _save_tcx(self) -> None:
        if not self._trackpoints or self._session_start is None:
            return
        name = (self._workout.name if self._workout else "Free Ride").replace(" ", "_")
        ts   = self._session_start.strftime("%Y%m%dT%H%M%S")
        out  = pathlib.Path("recordings") / f"{ts}_{name}.tcx"
        write_tcx(self._trackpoints, name.replace("_", " "), out)
        console.print(f"[green]Activity saved →[/green] {out}")

    def _send_power(self) -> None:
        if self._client and self._has_ftms and not self._is_paused and self._erg_enabled:
            asyncio.create_task(ftms_set_power(self._client, self._target))

    # ── BLE background tasks ──────────────────────────────────────────────────

    async def _ble_loop(self) -> None:
        self._session_start = datetime.datetime.now(datetime.timezone.utc)
        _AUTO_PAUSE_GRACE = 3.0  # seconds of zero power before auto-pausing

        def on_measurement(char: BleakGATTCharacteristic, data: bytearray) -> None:
            watts, crank_data = parse_power(data)
            self._actual = watts
            if not self._is_paused:
                self._history.append(watts)
                if len(self._history) > 40:
                    self._history.pop(0)

            if crank_data is not None:
                cumulative_crank_revs, last_crank_event_time = crank_data
                if self._last_crank_revs is not None and self._last_crank_event_time is not None:
                    delta_revs = (cumulative_crank_revs - self._last_crank_revs) & 0xFFFF
                    delta_time = (last_crank_event_time - self._last_crank_event_time) & 0xFFFF
                    if delta_time > 0:
                        self._cadence = int(60.0 * delta_revs / (delta_time / 1024.0))
                self._last_crank_revs = cumulative_crank_revs
                self._last_crank_event_time = last_crank_event_time

            now = time.monotonic()
            if watts == 0:
                if self._zero_since is None:
                    self._zero_since = now
                elif not self._auto_paused and (now - self._zero_since) >= _AUTO_PAUSE_GRACE:
                    # auto-pause: freeze workout clock
                    self._auto_paused = True
                    if self._pause_start is None:
                        self._pause_start = now
            else:
                if self._auto_paused:
                    # auto-resume
                    if self._pause_start is not None:
                        self._paused_elapsed += now - self._pause_start
                        self._pause_start = None
                    self._auto_paused = False
                    self._zero_since  = None
                    self._send_power()
                else:
                    self._zero_since = None

            if not self._is_paused:
                self._trackpoints.append(
                    (datetime.datetime.now(datetime.timezone.utc), watts, self._hr)
                )

        async with BleakClient(self._address) as client:
            self._client = client
            svc_uuids      = {s.uuid.lower() for s in client.services}
            self._has_ftms = FTMS_SERVICE in svc_uuids
            if self._has_ftms:
                try:
                    await ftms_request_control(client)
                    await ftms_start(client)
                    if self._erg_enabled:
                        await ftms_set_power(client, self._target)
                except Exception:
                    self._has_ftms = False

            await client.start_notify(CYCLING_POWER_MEASUREMENT, on_measurement)

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

    async def _cadence_loop(self) -> None:
        assert self._cadence_address is not None
        try:
            console.print(f"[dim]Connecting to cadence sensor at {self._cadence_address}...[/dim]")
            async with BleakClient(self._cadence_address) as client:
                def on_cadence(char: BleakGATTCharacteristic, data: bytearray) -> None:
                    crank_data = parse_csc(data)
                    if crank_data is not None:
                        cumulative_crank_revs, last_crank_event_time = crank_data
                        if self._csc_last_crank_revs is not None and self._csc_last_crank_event_time is not None:
                            delta_revs = (cumulative_crank_revs - self._csc_last_crank_revs) & 0xFFFF
                            delta_time = (last_crank_event_time - self._csc_last_crank_event_time) & 0xFFFF
                            if delta_time > 0:
                                self._cadence = int(60.0 * delta_revs / (delta_time / 1024.0))
                        self._csc_last_crank_revs = cumulative_crank_revs
                        self._csc_last_crank_event_time = last_crank_event_time

                try:
                    await client.start_notify(CSC_MEASUREMENT, on_cadence)
                    console.print(f"[green]Cadence sensor connected![/green]")
                    await self._quit_event.wait()
                    await client.stop_notify(CSC_MEASUREMENT)
                except Exception as notify_err:
                    console.print(f"[yellow]Cannot find CSC_MEASUREMENT characteristic: {notify_err}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Cadence sensor connection failed: {e}[/yellow]")

    async def _workout_loop(self, client: BleakClient) -> None:
        assert self._workout is not None
        start = time.monotonic()
        while not self._quit_event.is_set():
            now     = time.monotonic()
            elapsed = now - start - self._paused_elapsed
            if self._is_paused and self._pause_start is not None:
                elapsed -= (now - self._pause_start)

            if elapsed >= self._workout.total_duration:
                _, final_status   = workout_target(self._workout, self._workout.total_duration - 0.001)
                final_status.done = True
                self._status      = final_status
                self._done        = True
                await self._quit_event.wait()
                return

            if not self._is_paused:
                power, status = workout_target(self._workout, elapsed)
                self._status  = status
                new_target    = max(MIN_POWER, min(MAX_POWER, power + self._offset))
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
    args = sys.argv[1:]

    if "--workout" in args:
        # keep CLI path for scripting / the .spec launcher
        idx = args.index("--workout")
        if idx + 1 >= len(args):
            console.print("[red]--workout requires a file path argument[/red]")
            return
        workout = load_workout(args[idx + 1])
    else:
        path = _pick_workout_file()
        if path:
            workout = load_workout(path)

    if workout:
        console.print(
            f"[bold]Workout:[/bold] {workout.name}  "
            f"([dim]{len(workout.intervals)} intervals · {_fmt_time(workout.total_duration)}[/dim])"
        )

    address = asyncio.run(find_trainer())
    if address:
        hr_address = asyncio.run(find_hr_monitor())
        cadence_address = asyncio.run(find_cadence_sensor())
        WahooApp(address, workout, hr_address, cadence_address).run()


if __name__ == "__main__":
    main()
