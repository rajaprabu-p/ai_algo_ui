"""Shared configuration for v28 and v30, editable from the UI."""

from __future__ import annotations

import json
import os
from typing import Any

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editable_config.json")
DEFAULTS: dict[str, Any] = {
    "TRACKING_START": "09:15",
    "NO_NEW_ENTRY_AFTER": "12:00",
    "FORCE_EXIT": "14:50",
    "MTF_MODE": "AUTO",
    "MTF_INTERVAL": "5min",
    "MTF_STRICTNESS": 1,
    "FIXED_SL_POINTS": 25,
    "MAX_TRADES": 1,
    "ENABLE_MFE_EXIT": True,
}


def load_editable_config() -> dict[str, Any]:
    values = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
            saved = json.load(config_file)
        if isinstance(saved, dict):
            for key in DEFAULTS:
                if key not in saved:
                    continue
                value = saved[key]
                if key == "ENABLE_MFE_EXIT":
                    value = bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
                elif key in {"FIXED_SL_POINTS", "MAX_TRADES", "MTF_STRICTNESS"}:
                    value = int(value)
                else:
                    value = str(value).strip()
                if value != "":
                    values[key] = value
    except (OSError, TypeError, ValueError):
        pass
    return values


def save_editable_config(updates: dict[str, Any]) -> dict[str, Any]:
    values = load_editable_config()
    for key in DEFAULTS:
        if key in updates:
            value = updates[key]
            if key == "ENABLE_MFE_EXIT":
                value = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
            elif key in {"FIXED_SL_POINTS", "MAX_TRADES", "MTF_STRICTNESS"}:
                value = int(value)
            else:
                value = str(value).strip()
            if value == "" or value is None:
                raise ValueError(f"{key} cannot be empty")
            values[key] = value
    if values["MTF_MODE"] not in {"AUTO", "MANUAL"}:
        raise ValueError("MTF_MODE must be AUTO or MANUAL")
    if values["MTF_INTERVAL"] not in {"1min", "5min"}:
        raise ValueError("MTF_INTERVAL must be 1min or 5min")
    for key in ("TRACKING_START", "NO_NEW_ENTRY_AFTER", "FORCE_EXIT"):
        value = values[key]
        hours, minutes = value.split(":", 1)
        if not (hours.isdigit() and minutes.isdigit() and 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError(f"{key} must use HH:MM format")
    if values["FIXED_SL_POINTS"] <= 0:
        raise ValueError("FIXED_SL_POINTS must be positive")
    if values["MAX_TRADES"] <= 0:
        raise ValueError("MAX_TRADES must be positive")
    with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
        json.dump(values, config_file, indent=2)
        config_file.write("\n")
    return values
