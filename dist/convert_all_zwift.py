#!/usr/bin/env python3
"""
Batch convert all Zwift .zwo files in a directory tree to TrainerApp JSON format.

Usage:
  python convert_all_zwift.py [root_folder]
  
If no root folder is specified, uses ./zwift_workouts_all_collections_ordered_Mar21

Each .zwo file is converted to a .json file in the same directory.
"""

import sys
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


def parse_zwo_file(zwo_path: str) -> dict[str, Any]:
    """Parse a Zwift .zwo file and extract workout data.
    
    Supports both old and new Zwift ZWO formats:
    
    Old format:
    <workout>
      <name>...</name>
      <IntervalsWorkout>
        <Warmup power="0.5" cadence="90" duration="300" />
        ...
      </IntervalsWorkout>
    </workout>
    
    New format:
    <workout_file>
      <name>...</name>
      <workout>
        <SteadyState Duration="300" Power="0.5" />
        <IntervalsT Repeat="5" OnDuration="40" OffDuration="20" OnPower="1.09" OffPower="0.55" />
        ...
      </workout>
    </workout_file>
    """
    tree = ET.parse(zwo_path)
    root = tree.getroot()
    
    # Determine format: old (root=workout) or new (root=workout_file)
    if root.tag == "workout_file":
        # New format
        name = root.findtext("name", "Unnamed Workout")
        description = root.findtext("description", "")
        author = root.findtext("author", "")
        
        # Parse intervals from <workout> child
        intervals = []
        workout_elem = root.find("workout")
        if workout_elem is not None:
            for child in workout_elem:
                interval = _parse_interval_element(child)
                if interval:
                    intervals.append(interval)
    else:
        # Old format
        name = root.findtext("name", "Unnamed Workout")
        description = root.findtext("description", "")
        author = root.findtext("author", "")
        
        # Parse intervals
        intervals = []
        intervals_elem = root.find("IntervalsWorkout")
        
        if intervals_elem is not None:
            for child in intervals_elem:
                interval = _parse_interval_element(child)
                if interval:
                    intervals.append(interval)
    
    return {
        "name": name,
        "description": description,
        "author": author,
        "intervals": intervals,
    }


def _parse_interval_element(elem: ET.Element) -> dict[str, Any] | None:
    """Parse a single interval XML element into TrainerApp format."""
    tag = elem.tag
    
    # Handle new format: IntervalsT (repeating on/off intervals)
    if tag == "IntervalsT":
        repeat = int(float(elem.get("Repeat", 1)))
        on_duration = int(float(elem.get("OnDuration", 0)))
        off_duration = int(float(elem.get("OffDuration", 0)))
        on_power = float(elem.get("OnPower", 1.0))
        off_power = float(elem.get("OffPower", 0.5))
        
        intervals = []
        for _ in range(repeat):
            if on_duration > 0:
                intervals.append({
                    "duration": on_duration,
                    "power": on_power,
                    "note": f"interval {on_duration}sec @ {on_power:.2f}",
                })
            if off_duration > 0:
                intervals.append({
                    "duration": off_duration,
                    "power": off_power,
                    "note": f"recovery {off_duration}sec @ {off_power:.2f}",
                })
        return intervals  # Return list for flattening
    
    # Handle both old (lowercase) and new (uppercase) attribute names
    duration = int(float(elem.get("Duration") or elem.get("duration") or 0))
    if duration == 0:
        return None
    
    power = elem.get("Power") or elem.get("power")
    if power:
        power = float(power)
    
    # Handle different interval types
    if tag in ("SteadyState", "Warmup", "Cooldown", "TextOverlay"):
        if power is None:
            power = 1.0
        return {
            "duration": duration,
            "power": power,
            "note": elem.get("name", f"{tag} interval"),
        }
    
    elif tag == "Ramp":
        power_start_attr = elem.get("power_start") or elem.get("PowerStart")
        power_end_attr = elem.get("power_end") or elem.get("PowerEnd") or power
        
        power_start = float(power_start_attr) if power_start_attr else 0.5
        power_end = float(power_end_attr) if power_end_attr else 1.0
        
        return {
            "duration": duration,
            "power": power_end,
            "power_start": power_start,
            "type": "ramp",
            "note": elem.get("name", "Ramp interval"),
        }
    
    elif tag == "FreeRide":
        if power is None:
            power = 0.5
        return {
            "duration": duration,
            "power": power,
            "note": elem.get("name", "Free ride"),
        }
    
    else:
        # Try to extract power anyway
        if power is not None:
            return {
                "duration": duration,
                "power": power,
                "note": elem.get("name", f"{tag} interval"),
            }
    
    return None


def convert_zwift_to_trainerapp(zwo_data: dict[str, Any]) -> dict[str, Any]:
    """Convert parsed Zwift data to TrainerApp JSON format."""
    
    # Flatten intervals (handle both single dicts and lists from IntervalsT expansion)
    flat_intervals = []
    for interval in zwo_data["intervals"]:
        if interval is None:
            continue
        if isinstance(interval, list):
            flat_intervals.extend(interval)
        else:
            flat_intervals.append(interval)
    
    output = {
        "name": zwo_data["name"],
        "note": zwo_data.get("description", ""),
        "intervals": flat_intervals,
    }
    
    # Add author info to note if available
    if zwo_data.get("author"):
        output["note"] = f"{output['note']} (by {zwo_data['author']})".strip()
    
    return output


def convert_all_workouts(root_folder: str = "zwift_workouts_all_collections_ordered_Mar21") -> None:
    """Recursively convert all .zwo files to .json in the directory tree."""
    
    root_path = Path(root_folder)
    if not root_path.exists():
        print(f"❌ Error: Folder not found: {root_path}")
        sys.exit(1)
    
    print(f"📁 Scanning: {root_path.absolute()}\n")
    
    zwo_files = list(root_path.rglob("*.zwo"))
    
    if not zwo_files:
        print("⚠️  No .zwo files found.")
        return
    
    print(f"Found {len(zwo_files)} workout(s) to convert.\n")
    
    successful = 0
    failed = 0
    
    for idx, zwo_path in enumerate(sorted(zwo_files), 1):
        json_path = zwo_path.with_suffix(".json")
        relative_path = zwo_path.relative_to(root_path)
        
        try:
            print(f"[{idx}/{len(zwo_files)}] Converting: {relative_path}")
            
            # Parse and convert
            zwo_data = parse_zwo_file(str(zwo_path))
            converted = convert_zwift_to_trainerapp(zwo_data)
            
            # Save JSON
            with open(json_path, "w") as f:
                json.dump(converted, f, indent=2)
            
            # Show summary
            interval_count = len(converted["intervals"])
            print(f"          ✅ {converted['name']} ({interval_count} intervals)")
            print(f"          → {json_path.name}\n")
            
            successful += 1
            
        except Exception as e:
            print(f"          ❌ Error: {e}\n")
            failed += 1
    
    # Summary
    print("=" * 70)
    print(f"✅ Converted: {successful}")
    if failed > 0:
        print(f"❌ Failed:    {failed}")
    print(f"📊 Total:     {len(zwo_files)}")
    print("=" * 70)


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "zwift_workouts_all_collections_ordered_Mar21"
    convert_all_workouts(root)
