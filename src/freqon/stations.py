"""The station list: found by scanning, editable by the user, shared by every client.

Each station is a frequency; each HD program on it (HD1, HD2, ...) becomes a channel.
Channel numbers follow the "<MHz*10>.<HD number>" pattern, e.g. 97.1 HD2 -> "971.2", so
they sort sensibly in Plex and never collide.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field


@dataclass
class Station:
    frequency: float
    name: str | None = None  # from the broadcast (HD station name)
    custom_name: str | None = None  # user override
    hd: bool = False
    programs: list[dict] = field(default_factory=list)  # [{program, name, type}]
    power_db: float | None = None
    hidden: bool = False

    @property
    def display_name(self) -> str:
        return clean_name(self.custom_name) or clean_name(self.name) or f"{self.frequency:.1f} FM"

    def channels(self) -> list[dict]:
        progs = self.programs if self.hd and self.programs else [{"program": 0}]
        out = []
        for p in sorted(progs, key=lambda p: p["program"]):
            n = p["program"]
            hd_label = f"HD{n + 1}"
            name = self.display_name if n == 0 else f"{self.display_name} {hd_label}"
            out.append({
                "id": channel_id(self.frequency, n),
                "number": f"{round(self.frequency * 10)}.{n + 1}",
                "name": name,
                "frequency": self.frequency,
                "program": n,
                "hd": self.hd,
                "program_name": p.get("name") or hd_label,
                "program_type": p.get("type"),
            })
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["display_name"] = self.display_name
        d["channels"] = self.channels()
        return d


MAX_NAME = 64


def clean_name(text: str | None) -> str | None:
    """Names come from users and from the air; keep them short, single-line and printable."""
    if text is None:
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return " ".join(text.split())[:MAX_NAME]


def channel_id(frequency: float, program: int) -> str:
    return f"{round(frequency * 10)}.{program + 1}"


def parse_channel_id(cid: str) -> tuple[float, int]:
    major, _, minor = cid.partition(".")
    return int(major) / 10, (int(minor) - 1 if minor else 0)


class StationStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._stations: dict[float, Station] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as f:
            data = json.load(f)
        self._stations = {}
        for item in data.get("stations", []):
            item.pop("display_name", None)
            item.pop("channels", None)
            st = Station(**item)
            self._stations[st.frequency] = st

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        data = {"stations": [asdict(s) for s in self.all(include_hidden=True)]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def all(self, include_hidden: bool = False) -> list[Station]:
        with self._lock:
            items = sorted(self._stations.values(), key=lambda s: s.frequency)
        return [s for s in items if include_hidden or not s.hidden]

    def get(self, frequency: float) -> Station | None:
        return self._stations.get(round(frequency, 1))

    def channels(self) -> list[dict]:
        return [c for s in self.all() for c in s.channels()]

    def merge_scan(self, found: list[Station]) -> None:
        """Replace scanned data but keep the user's renames and hidden flags."""
        with self._lock:
            old = self._stations
            new: dict[float, Station] = {}
            for st in found:
                prev = old.get(st.frequency)
                if prev:
                    st.custom_name = prev.custom_name
                    st.hidden = prev.hidden
                new[st.frequency] = st
            # Stations the user customised survive a scan that missed them (e.g. weak tonight).
            for f, prev in old.items():
                if f not in new and (prev.custom_name or prev.hidden):
                    new[f] = prev
            self._stations = new
        self.save()

    def add_found(self, found: list[Station]) -> list[str]:
        """Merge a scan without removing anything: new stations and new HD channels of known
        stations are added; renames, hidden flags and existing details are kept.
        Returns the channel ids that are new."""
        added: list[str] = []
        with self._lock:
            for st in found:
                prev = self._stations.get(st.frequency)
                if prev is None:
                    self._stations[st.frequency] = st
                    added += [c["id"] for c in st.channels()]
                    continue
                had = {p["program"] for p in prev.programs} or {0}  # an FM-only station has HD1/FM
                new = [p for p in st.programs if p["program"] not in had]
                if st.hd and not prev.hd:
                    prev.hd = True
                prev.name = prev.name or st.name
                if new or (st.programs and not prev.programs):
                    known = {p["program"] for p in prev.programs}
                    prev.programs = sorted(prev.programs + [p for p in st.programs if p["program"] not in known],
                                           key=lambda p: p["program"])
                    added += [channel_id(prev.frequency, p["program"]) for p in new]
        if added:
            self.save()
        return added

    def upsert(self, st: Station) -> None:
        with self._lock:
            self._stations[st.frequency] = st
        self.save()

    def update(self, frequency: float, **changes) -> Station:
        with self._lock:
            st = self._stations.get(round(frequency, 1))
            if st is None:
                raise KeyError(frequency)
            for k, v in changes.items():
                if k == "custom_name":
                    if v is not None and not isinstance(v, str):
                        raise ValueError("custom_name must be text or null")
                    v = (clean_name(v) or None) if v is not None else None
                elif k == "hidden":
                    if not isinstance(v, bool):
                        raise ValueError("hidden must be true or false")
                else:
                    raise ValueError(f"cannot change {k}")
                setattr(st, k, v)
        self.save()
        return st

    def delete(self, frequency: float) -> None:
        with self._lock:
            self._stations.pop(round(frequency, 1), None)
        self.save()
