#!/usr/bin/env python3
"""
Ingress COMM static cooker.

Phase 1 rebuilds bot-compatible references from raw DataRecord JSON files:
  references/agents.json
  references/portals.json

Expected raw record shape matches IngressCommTelegramBot DataRecord.to_dict():
  {
    "uuid": "...",
    "timestampms": 171...,
    "action": "Crear campo",
    "agent": {"name": "...", "faction": "Enlightened", ...},
    "portals": [{"name": "...", "address": "...", "location": {"lat": 0, "lng": 0}}],
    "MUs": 123
  }
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTION_CREATE_FIELD = "Crear campo"
ACTION_DESTROY_FIELD = "Destruir campo"
FACTION_UNKNOWN = "Unknown"
CHUNK_PREFIX = "cooked_chunk_"
CHUNK_SIZE = 3 * 1024 * 1024
COOKED_META_NAME = "cooked_meta.json"
COOKED_SCHEMA_VERSION = 1

ACTION_MAP = {
    "Capturar": "capture",
    "Colocar resonador": "deploy",
    "Crear enlace": "link",
    "Crear campo": "field",
    "Destruir resonador": "destroy",
    "Destruir enlace": "destroyLink",
    "Destruir campo": "destroyField",
}

FACTION_MAP = {
    "Enlightened": "ENL",
    "enlightened": "ENL",
    "ENLIGHTENED": "ENL",
    "Resistance": "RES",
    "resistance": "RES",
    "RESISTANCE": "RES",
    "Machina": "NEU",
    "Neutral": "NEU",
    "Unknown": "UNK",
}


@dataclass
class AgentAccumulator:
    name: str
    factions: Counter[str] = field(default_factory=Counter)
    mus_gained: int = 0
    mus_substracted: int = 0
    events: int = 0

    def add(self, faction: str | None, action: str | None, mus: int) -> None:
        self.events += 1
        if faction:
            self.factions[faction] += 1
        if action == ACTION_CREATE_FIELD:
            self.mus_gained += mus
        elif action == ACTION_DESTROY_FIELD:
            self.mus_substracted += mus

    def to_bot_dict(self) -> dict[str, Any]:
        faction = most_likely_faction(self.factions)
        return {
            "name": self.name,
            "faction": faction,
            "MUsgained": self.mus_gained,
            "MUssubstracted": self.mus_substracted,
        }


@dataclass
class PortalAccumulator:
    name: str
    address: str
    lat: float
    lng: float
    seen: int = 0

    def add(self) -> None:
        self.seen += 1

    def to_bot_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "location": {
                "lat": self.lat,
                "lng": self.lng,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ingress dashboard references from raw records.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="Cooker root directory.")
    parser.add_argument("--raw-dir", type=Path, default=None, help="Directory containing *records*.json files.")
    parser.add_argument("--references-dir", type=Path, default=None, help="Output directory for agents/portals JSON.")
    parser.add_argument("--meta-path", type=Path, default=None, help="Output path for build metadata JSON.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow writing an empty cooked payload.")
    args = parser.parse_args()

    root = args.root.resolve()
    raw_dir = (args.raw_dir or root / "raw").resolve()
    references_dir = (args.references_dir or root / "references").resolve()
    compiled_dir = (root / "compiled").resolve()
    meta_path = (args.meta_path or compiled_dir / "build_meta.json").resolve()

    records, stats = read_records(raw_dir)
    if not records and not args.allow_empty:
        raise SystemExit(
            "No records found. Pass --raw-dir with the JSON records folder, "
            "or use --allow-empty if you really want to write an empty payload."
        )
    agents, portals = build_references(records)
    cooked_events = build_cooked_events(records)

    references_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    write_json(references_dir / "agents.json", [a.to_bot_dict() for a in sorted(agents.values(), key=lambda x: x.name.lower())])
    write_json(references_dir / "portals.json", [p.to_bot_dict() for p in sorted(portals.values(), key=lambda x: (x.name.lower(), x.address.lower()))])

    cooked_meta = write_cooked_payload(compiled_dir, cooked_events, stats)

    meta = {
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "rawDir": str(raw_dir),
        "filesRead": stats["files_read"],
        "recordsRead": stats["records_read"],
        "recordsUnique": len(records),
        "duplicatesSkipped": stats["duplicates_skipped"],
        "agents": len(agents),
        "portals": len(portals),
        "cookedEvents": len(cooked_events),
        "cookedChunks": cooked_meta["totalChunks"],
        "cookedRawJsonBytes": cooked_meta["rawJsonBytes"],
        "cookedCompressedBase64Bytes": cooked_meta["compressedBase64Bytes"],
        "schema": "bot-references-v1",
    }
    write_json(meta_path, meta)

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def read_records(raw_dir: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    files = sorted(raw_dir.glob("*records*.json"))
    if not files:
        files = sorted(raw_dir.glob("*.json"))

    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    stats = {
        "files_read": 0,
        "records_read": 0,
        "duplicates_skipped": 0,
    }

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError(f"{path} does not contain a JSON list.")
        stats["files_read"] += 1
        stats["records_read"] += len(data)
        for record in data:
            if not isinstance(record, dict):
                continue
            key = record_key(record)
            if key in seen:
                stats["duplicates_skipped"] += 1
                continue
            seen.add(key)
            records.append(record)

    records.sort(key=lambda r: safe_int(r.get("timestampms")))
    return records, stats


def build_references(records: list[dict[str, Any]]) -> tuple[dict[str, AgentAccumulator], dict[str, PortalAccumulator]]:
    agents: dict[str, AgentAccumulator] = {}
    portals: dict[str, PortalAccumulator] = {}

    for record in records:
        action = as_str(record.get("action"))
        mus = safe_int(record.get("MUs"))

        agent_data = record.get("agent") if isinstance(record.get("agent"), dict) else {}
        agent_name = as_str(agent_data.get("name")).strip()
        if agent_name:
            agent = agents.setdefault(agent_name, AgentAccumulator(name=agent_name))
            agent.add(as_str(agent_data.get("faction")).strip() or None, action, mus)

        portal_list = record.get("portals")
        if not isinstance(portal_list, list):
            continue
        for portal_data in portal_list:
            portal = parse_portal(portal_data)
            if not portal:
                continue
            key = portal_key(portal.name, portal.address)
            existing = portals.get(key)
            if existing:
                existing.add()
            else:
                portal.add()
                portals[key] = portal

    return agents, portals


def build_cooked_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cooked: list[dict[str, Any]] = []

    for record in records:
        event = normalize_record(record)
        if not event:
            continue
        if is_in_france(event.get("lat"), event.get("lng")):
            continue
        cooked.append(event)

    cooked.sort(key=lambda e: safe_int(e.get("t")))
    return cooked


def normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    ts = safe_int(record.get("timestampms"))
    if not ts:
        return None

    agent_data = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    agent = as_str(agent_data.get("name")).strip()
    if not agent:
        return None

    action_label = as_str(record.get("action"))
    action_type = ACTION_MAP.get(action_label, "other")
    portals = record.get("portals") if isinstance(record.get("portals"), list) else []
    p0 = portals[0] if portals and isinstance(portals[0], dict) else {}
    p1 = portals[1] if len(portals) > 1 and isinstance(portals[1], dict) else None

    multi_destroy = action_type in {"destroyLink", "destroyField"}
    p0_location = p0.get("location") if isinstance(p0.get("location"), dict) else {}
    p1_location = p1.get("location") if p1 and isinstance(p1.get("location"), dict) else {}
    p0_address = as_str(p0.get("address"))
    pc = extract_postal_code(p0_address)
    mn = extract_municipality(p0_address)
    mu = max(0, safe_int(record.get("MUs") or agent_data.get("MUsgained")))
    kind = (
        "presence"
        if action_type in {"capture", "deploy", "link", "field"}
        else "attack"
        if action_type in {"destroy", "destroyLink", "destroyField"}
        else "unknown"
    )

    event: dict[str, Any] = {
        "u": as_str(record.get("uuid")).strip() or None,
        "t": ts,
        "f": FACTION_MAP.get(as_str(agent_data.get("faction")), "UNK"),
        "a": agent,
        "p": None if multi_destroy else (as_str(p0.get("name")).strip() or None),
        "at": action_type,
        "m": build_message(agent, action_label, p0, p1, mu),
        "k": kind,
        "mu": mu,
        "pc": pc,
        "mn": mn,
    }

    if not multi_destroy and p0_location:
        lat = p0_location.get("lat")
        lng = p0_location.get("lng")
        if lat is not None and lng is not None:
            event["lat"] = float(lat)
            event["lng"] = float(lng)

    if p1:
        p2 = as_str(p1.get("name")).strip()
        if p2:
            event["p2"] = p2
        if p1_location:
            p2lat = p1_location.get("lat")
            p2lng = p1_location.get("lng")
            if p2lat is not None and p2lng is not None:
                event["p2lat"] = float(p2lat)
                event["p2lng"] = float(p2lng)

    return event


def build_message(agent: str, action_label: str, p0: dict[str, Any], p1: dict[str, Any] | None, mu: int) -> str:
    msg = f"{agent} [{action_label}] {as_str(p0.get('name')).strip()}"
    if p1 and as_str(p1.get("name")).strip():
        msg += " -> " + as_str(p1.get("name")).strip()
    if mu > 0:
        msg += f" +{mu} MUs"
    return msg


def write_cooked_payload(compiled_dir: Path, events: list[dict[str, Any]], stats: dict[str, int]) -> dict[str, Any]:
    payload = {
        "schema": COOKED_SCHEMA_VERSION,
        "events": events,
    }
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    gz_bytes = gzip.compress(raw_json.encode("utf-8"))
    b64 = base64.b64encode(gz_bytes).decode("ascii")
    chunks = split_chunks(b64, CHUNK_SIZE)

    for old_chunk in compiled_dir.glob(CHUNK_PREFIX + "*.txt"):
        old_chunk.unlink()
    for idx, chunk in enumerate(chunks):
        (compiled_dir / f"{CHUNK_PREFIX}{idx:03d}.txt").write_text(chunk, encoding="utf-8")

    meta = {
        "schema": COOKED_SCHEMA_VERSION,
        "cookedAt": datetime.now(timezone.utc).isoformat(),
        "processedSources": [],
        "processedSourceFiles": stats["files_read"],
        "pendingSourceFiles": 0,
        "recordsThisRun": stats["records_read"],
        "eventsAddedThisRun": len(events),
        "eventCount": len(events),
        "rawJsonBytes": len(raw_json),
        "compressedBase64Bytes": len(b64),
        "totalChunks": len(chunks),
        "chunkSize": CHUNK_SIZE,
        "generator": "python-static-cooker",
    }
    write_json(compiled_dir / COOKED_META_NAME, meta)
    return meta


def parse_portal(data: Any) -> PortalAccumulator | None:
    if not isinstance(data, dict):
        return None
    name = as_str(data.get("name")).strip()
    address = as_str(data.get("address")).strip()
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    lat = location.get("lat")
    lng = location.get("lng")
    if not name or not address or lat is None or lng is None:
        return None
    try:
        return PortalAccumulator(name=name, address=address, lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None


def record_key(record: dict[str, Any]) -> str:
    uuid = as_str(record.get("uuid")).strip()
    if uuid:
        return "uuid|" + uuid
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    portals = record.get("portals") if isinstance(record.get("portals"), list) else []
    first_portal = portals[0] if portals and isinstance(portals[0], dict) else {}
    return "|".join(
        [
            "fallback",
            as_str(record.get("timestampms")),
            as_str(record.get("action")),
            as_str(agent.get("name")),
            as_str(first_portal.get("name")),
        ]
    )


def portal_key(name: str, address: str) -> str:
    return name + "\0" + address


def most_likely_faction(factions: Counter[str]) -> str:
    if not factions:
        return FACTION_UNKNOWN
    return sorted(factions.items(), key=lambda item: (-item[1], item[0]))[0][0]


def extract_postal_code(address: str) -> str | None:
    match = re.search(r"\b(\d{5})\b", address or "")
    return match.group(1) if match else None


def extract_municipality(address: str) -> str | None:
    match = re.search(r"\b\d{5}\s+([^,]+?)(?:,|$)", address or "")
    if not match:
        return None
    municipality = match.group(1).strip()
    return None if municipality.lower() == "spain" else municipality


def is_in_france(lat: Any, lng: Any) -> bool:
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return False
    return lat_f > 43.35 and -2.0 < lng_f < 8.5


def split_chunks(text: str, size: int) -> list[str]:
    return [text[offset : offset + size] for offset in range(0, len(text), size)]


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def as_str(value: Any) -> str:
    return "" if value is None else str(value)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
