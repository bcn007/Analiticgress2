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
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ACTION_CREATE_FIELD = "Crear campo"
ACTION_DESTROY_FIELD = "Destruir campo"
FACTION_UNKNOWN = "Unknown"
TIMEZONE_NAME = "Europe/Madrid"
DASHBOARD_TZ = ZoneInfo(TIMEZONE_NAME)
SESSION_GAP_MS = 4 * 60 * 60 * 1000
SESSION_ID_ALGO = "sha1-10"
OPEN_SESSION_ID_ALGO = "sha1-10-agent-start-end"
HEATMAP_FORMAT = "weekday-hour-flat-168"
HEATMAP_WEEK_START = "monday"
SPEED_FILTER = {
    "maxKmh": 200,
    "minGapMs": 60 * 1000,
}
RIVAL_WINDOW_MS = 30 * 60 * 1000
IGNORED_AGENT_KEYS = {
    "agent",
    "your",
    "enlightened",
    "resistance",
    "machina",
    "__ada__",
    "__jarvis__",
    "niasection14",
    "unknown",
    "a",
    "an",
    "the",
}
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

    build_started = datetime.now(timezone.utc)
    now_ms = int(build_started.timestamp() * 1000)

    records, stats = read_records(raw_dir)
    if not records and not args.allow_empty:
        raise SystemExit(
            "No records found. Pass --raw-dir with the JSON records folder, "
            "or use --allow-empty if you really want to write an empty payload."
        )
    agents, portals = build_references(records)
    cooked_events = build_cooked_events(records, now_ms)

    references_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    write_json(references_dir / "agents.json", [a.to_bot_dict() for a in sorted(agents.values(), key=lambda x: x.name.lower())])
    write_json(references_dir / "portals.json", [p.to_bot_dict() for p in sorted(portals.values(), key=lambda x: (x.name.lower(), x.address.lower()))])

    cooked_meta = write_cooked_payload(compiled_dir, cooked_events, stats, build_started)
    scalable_meta = write_scalable_payload(compiled_dir, cooked_events, stats, build_started)

    meta = {
        "builtAt": build_started.isoformat(),
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
        "scalableDays": scalable_meta["dayCount"],
        "scalableSummaries": scalable_meta["summaries"],
        "schema": "bot-references-v1",
        "timezone": TIMEZONE_NAME,
        "dayKeyAlgo": "Europe/Madrid local date",
        "sessionGapMs": SESSION_GAP_MS,
        "sidAlgo": SESSION_ID_ALGO,
        "openSessionSidAlgo": OPEN_SESSION_ID_ALGO,
        "speedFilter": SPEED_FILTER,
        "filters": cooked_filters_contract(),
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
        if agent_name and not is_ignored_agent(agent_name):
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


def build_cooked_events(records: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    cooked: list[dict[str, Any]] = []

    for record in records:
        event = normalize_record(record)
        if not event:
            continue
        if is_in_france(event.get("lat"), event.get("lng")):
            continue
        event["dk"] = day_key(event["t"])
        cooked.append(event)

    cooked.sort(key=lambda e: safe_int(e.get("t")))
    assign_session_ids(cooked, now_ms)
    return cooked


def assign_session_ids(events: list[dict[str, Any]], now_ms: int) -> None:
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        agent = as_str(event.get("a")).strip()
        if agent:
            by_agent[agent_key(agent)].append(event)

    for akey, agent_events in by_agent.items():
        agent_events.sort(key=lambda e: safe_int(e.get("t")))
        session: list[dict[str, Any]] = []
        for event in agent_events:
            if session and safe_int(event.get("t")) - safe_int(session[-1].get("t")) > SESSION_GAP_MS:
                finalize_session_id(akey, session, now_ms)
                session = []
            session.append(event)
        if session:
            finalize_session_id(akey, session, now_ms)


def finalize_session_id(akey: str, session: list[dict[str, Any]], now_ms: int) -> None:
    start_ts = safe_int(session[0].get("t"))
    end_ts = safe_int(session[-1].get("t"))
    is_open = now_ms - end_ts <= SESSION_GAP_MS
    sid = open_session_id(akey, start_ts, end_ts) if is_open else session_id(akey, start_ts)
    day_keys = sorted({day_key(safe_int(event.get("t"))) for event in session})

    for event in session:
        event["sid"] = sid
        if is_open:
            event["sessionOpen"] = True
        if len(day_keys) > 1:
            event["sdk"] = day_keys


def session_id(akey: str, session_start_ts: int) -> str:
    raw = f"{akey}|{session_start_ts}".encode("utf-8")
    return "s_" + hashlib.sha1(raw).hexdigest()[:10]


def open_session_id(akey: str, session_start_ts: int, session_end_ts: int) -> str:
    raw = f"{akey}|{session_start_ts}|{session_end_ts}".encode("utf-8")
    return "s_open_" + hashlib.sha1(raw).hexdigest()[:10]


def day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(DASHBOARD_TZ).date().isoformat()


def normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    ts = safe_int(record.get("timestampms"))
    if not ts:
        return None

    agent_data = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    agent = as_str(agent_data.get("name")).strip()
    if not agent or is_ignored_agent(agent):
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


def write_cooked_payload(
    compiled_dir: Path,
    events: list[dict[str, Any]],
    stats: dict[str, int],
    built_at: datetime,
) -> dict[str, Any]:
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
        "cookedAt": built_at.isoformat(),
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
        "timezone": TIMEZONE_NAME,
        "dayKeyAlgo": "Europe/Madrid local date",
        "sessionGapMs": SESSION_GAP_MS,
        "sidAlgo": SESSION_ID_ALGO,
        "openSessionSidAlgo": OPEN_SESSION_ID_ALGO,
        "heatmap": {
            "format": HEATMAP_FORMAT,
            "weekStart": HEATMAP_WEEK_START,
            "tz": TIMEZONE_NAME,
        },
        "speedFilter": SPEED_FILTER,
        "relationships": {
            "topCollab": "same faction, co-presence in global session or nearby operational window",
            "topRivals": f"opposite faction, same portal or zone inside {RIVAL_WINDOW_MS // 60000} min window",
        },
        "filters": cooked_filters_contract(),
    }
    write_json(compiled_dir / COOKED_META_NAME, meta)
    return meta


def write_scalable_payload(
    compiled_dir: Path,
    events: list[dict[str, Any]],
    stats: dict[str, int],
    built_at: datetime,
) -> dict[str, Any]:
    events_dir = compiled_dir / "events"
    summaries_dir = compiled_dir / "summaries"
    indexes_dir = compiled_dir / "indexes"
    reset_tree(events_dir)
    reset_tree(summaries_dir)
    reset_tree(indexes_dir)

    days: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_day[as_str(event.get("dk"))].append(event)

    for dkey in sorted(k for k in by_day if k):
        day_events = by_day[dkey]
        rel_path = f"events/{dkey[0:4]}/{dkey[5:7]}/{dkey}.json.gz.b64.txt"
        output_path = compiled_dir / rel_path
        file_meta = write_gzip_b64_json(output_path, {"schema": COOKED_SCHEMA_VERSION, "day": dkey, "events": day_events})
        days.append(
            {
                "day": dkey,
                "path": rel_path,
                "eventCount": len(day_events),
                "firstTs": safe_int(day_events[0].get("t")),
                "lastTs": safe_int(day_events[-1].get("t")),
                "rawJsonBytes": file_meta["rawJsonBytes"],
                "compressedBase64Bytes": file_meta["compressedBase64Bytes"],
            }
        )

    summaries = build_summaries(events)
    summary_files = {
        "global": "summaries/global.json.gz.b64.txt",
        "months": "summaries/months.json.gz.b64.txt",
        "agents": "summaries/agents.json.gz.b64.txt",
        "portals": "summaries/portals.json.gz.b64.txt",
        "rankings": "summaries/rankings.json.gz.b64.txt",
        "hallOfFame": "summaries/hall_of_fame.json.gz.b64.txt",
        "agentIndex": "indexes/agents.json.gz.b64.txt",
    }
    summary_meta: dict[str, Any] = {}
    for key, rel_path in summary_files.items():
        summary_meta[key] = {"path": rel_path, **write_gzip_b64_json(compiled_dir / rel_path, summaries[key])}

    meta = {
        "schema": 2,
        "builtAt": built_at.isoformat(),
        "generator": "python-static-cooker",
        "timezone": TIMEZONE_NAME,
        "dayKeyAlgo": "Europe/Madrid local date",
        "sessionGapMs": SESSION_GAP_MS,
        "sidAlgo": SESSION_ID_ALGO,
        "openSessionSidAlgo": OPEN_SESSION_ID_ALGO,
        "eventCount": len(events),
        "sourceFiles": stats["files_read"],
        "recordsRead": stats["records_read"],
        "firstTs": safe_int(events[0].get("t")) if events else None,
        "lastTs": safe_int(events[-1].get("t")) if events else None,
        "dayCount": len(days),
        "days": days,
        "summaries": summary_meta,
        "heatmap": {
            "format": HEATMAP_FORMAT,
            "weekStart": HEATMAP_WEEK_START,
            "tz": TIMEZONE_NAME,
        },
        "speedFilter": SPEED_FILTER,
        "relationships": {
            "topCollab": "same faction, co-presence in global session",
            "topRivals": f"opposite faction, same portal inside {RIVAL_WINDOW_MS // 60000} min window",
        },
        "filters": cooked_filters_contract(),
        "clientRules": {
            "defaultRangeDays": 30,
            "maxRawRangeDays": 90,
            "versionQuery": "builtAt",
        },
    }
    write_json(compiled_dir / "meta.json", meta)
    return meta


def build_summaries(events: list[dict[str, Any]]) -> dict[str, Any]:
    global_summary = build_global_summary(events)
    months_summary = build_months_summary(events)
    agent_summary, agent_index = build_agent_summary_and_index(events)
    portal_summary = build_portal_summary(events)
    rankings = build_rankings(agent_summary)
    hall_of_fame = build_hall_of_fame(events, agent_summary, agent_index)
    return {
        "global": global_summary,
        "months": months_summary,
        "agents": agent_summary,
        "portals": portal_summary,
        "rankings": rankings,
        "hallOfFame": hall_of_fame,
        "agentIndex": agent_index,
    }


def build_global_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    factions = Counter(as_str(event.get("f")) for event in events)
    actions = Counter(as_str(event.get("at")) for event in events)
    kinds = Counter(as_str(event.get("k")) for event in events)
    days = {as_str(event.get("dk")) for event in events if event.get("dk")}
    agents = {as_str(event.get("a")) for event in events if event.get("a")}
    portals = {as_str(event.get("p")) for event in events if event.get("p")}
    return {
        "schema": 1,
        "eventCount": len(events),
        "firstTs": safe_int(events[0].get("t")) if events else None,
        "lastTs": safe_int(events[-1].get("t")) if events else None,
        "days": len(days),
        "agents": len(agents),
        "portals": len(portals),
        "totalMU": sum(safe_int(event.get("mu")) for event in events),
        "factions": dict(sorted(factions.items())),
        "actions": dict(sorted(actions.items())),
        "eventKinds": dict(sorted(kinds.items())),
    }


def build_months_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    months: dict[str, dict[str, Any]] = {}
    for event in events:
        dkey = as_str(event.get("dk"))
        if not dkey:
            continue
        mkey = dkey[:7]
        month = months.setdefault(
            mkey,
            {
                "month": mkey,
                "eventCount": 0,
                "totalMU": 0,
                "days": set(),
                "factions": Counter(),
                "actions": Counter(),
            },
        )
        month["eventCount"] += 1
        month["totalMU"] += safe_int(event.get("mu"))
        month["days"].add(dkey)
        month["factions"][as_str(event.get("f"))] += 1
        month["actions"][as_str(event.get("at"))] += 1

    result = []
    for month in sorted(months.values(), key=lambda item: item["month"]):
        result.append(
            {
                "month": month["month"],
                "eventCount": month["eventCount"],
                "totalMU": month["totalMU"],
                "days": len(month["days"]),
                "factions": dict(sorted(month["factions"].items())),
                "actions": dict(sorted(month["actions"].items())),
            }
        )
    return result


def build_agent_summary_and_index(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        agent = as_str(event.get("a")).strip()
        sid = as_str(event.get("sid")).strip()
        if agent:
            by_agent[agent].append(event)
        if sid:
            by_sid[sid].append(event)

    collabs: dict[str, Counter[str]] = defaultdict(Counter)
    for session_events in by_sid.values():
        agents_in_session: dict[str, str] = {}
        for event in session_events:
            agent = as_str(event.get("a")).strip()
            if agent:
                agents_in_session[agent] = as_str(event.get("f"))
        names = sorted(agents_in_session)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if agents_in_session[a] and agents_in_session[a] == agents_in_session[b]:
                    collabs[a][b] += 1
                    collabs[b][a] += 1

    rivals = build_rival_counters(events)
    summaries = []
    index_agents = []
    for agent, agent_events in sorted(by_agent.items(), key=lambda item: item[0].lower()):
        agent_events.sort(key=lambda event: safe_int(event.get("t")))
        actions = Counter(as_str(event.get("at")) for event in agent_events)
        kinds = Counter(as_str(event.get("k")) for event in agent_events)
        portals = Counter(as_str(event.get("p")) for event in agent_events if event.get("p"))
        zones = Counter(as_str(event.get("mn")) for event in agent_events if event.get("mn"))
        days = {as_str(event.get("dk")) for event in agent_events if event.get("dk")}
        sessions = build_session_index(agent_events)
        total_mu = sum(safe_int(event.get("mu")) for event in agent_events)
        speed_total = compute_speed_total_km(agent_events)
        session_ms = sum(max(0, safe_int(s["endTs"]) - safe_int(s["startTs"])) for s in sessions)
        ratios = build_agent_ratios(agent_events, actions, len(days), total_mu, speed_total, sessions, session_ms)
        summary = {
            "name": agent,
            "faction": most_common(as_str(event.get("f")) for event in agent_events),
            "count": len(agent_events),
            "totalMU": total_mu,
            "firstTs": safe_int(agent_events[0].get("t")),
            "lastTs": safe_int(agent_events[-1].get("t")),
            "daysActive": len(days),
            "sessionsCount": len(sessions),
            "totalSessionMs": session_ms,
            "speedTotalKm": round(speed_total, 3),
            "actions": normalize_action_counts(actions),
            "eventKinds": normalize_kind_counts(kinds),
            "topPortals": top_pairs(portals, 3),
            "topZones": top_pairs(zones, 3),
            "topCollab": top_pairs(collabs.get(agent, Counter()), 10),
            "topRivals": top_pairs(rivals.get(agent, Counter()), 10),
            "heatmap7x24": build_heatmap(agent_events),
            "ratios": ratios,
        }
        summaries.append(summary)
        index_agents.append(
            {
                "name": agent,
                "faction": summary["faction"],
                "daysActive": len(days),
                "days": sorted(days),
                "sessions": sessions,
            }
        )

    return summaries, {"schema": 1, "agents": index_agents}


def build_rival_counters(events: list[dict[str, Any]]) -> dict[str, Counter[str]]:
    rivals: dict[str, Counter[str]] = defaultdict(Counter)
    by_portal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        portal = as_str(event.get("p")).strip()
        if portal:
            by_portal[portal].append(event)

    for portal_events in by_portal.values():
        portal_events.sort(key=lambda event: safe_int(event.get("t")))
        for i, event in enumerate(portal_events):
            t0 = safe_int(event.get("t"))
            a = as_str(event.get("a")).strip()
            fa = as_str(event.get("f"))
            if not a or not fa:
                continue
            j = i + 1
            while j < len(portal_events):
                other = portal_events[j]
                if safe_int(other.get("t")) - t0 > RIVAL_WINDOW_MS:
                    break
                b = as_str(other.get("a")).strip()
                fb = as_str(other.get("f"))
                if b and b != a and fb and fb != fa:
                    rivals[a][b] += 1
                    rivals[b][a] += 1
                j += 1
    return rivals


def build_session_index(agent_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in agent_events:
        sid = as_str(event.get("sid")).strip()
        if sid:
            grouped[sid].append(event)
    sessions = []
    for sid, session_events in grouped.items():
        session_events.sort(key=lambda event: safe_int(event.get("t")))
        day_keys = sorted({as_str(event.get("dk")) for event in session_events if event.get("dk")})
        sessions.append(
            {
                "sid": sid,
                "startTs": safe_int(session_events[0].get("t")),
                "endTs": safe_int(session_events[-1].get("t")),
                "dayKeys": day_keys,
                "eventCount": len(session_events),
                "open": any(bool(event.get("sessionOpen")) for event in session_events),
            }
        )
    sessions.sort(key=lambda item: safe_int(item["startTs"]))
    return sessions


def build_agent_ratios(
    agent_events: list[dict[str, Any]],
    actions: Counter[str],
    days_active: int,
    total_mu: int,
    speed_total_km: float,
    sessions: list[dict[str, Any]],
    session_ms: int,
) -> dict[str, Any]:
    days = max(1, days_active)
    session_hours = session_ms / 3600000 if session_ms else 0
    fields = actions.get("field", 0)
    captures = actions.get("capture", 0)
    attacks = actions.get("destroy", 0) + actions.get("destroyLink", 0) + actions.get("destroyField", 0)
    sessions_count = len(sessions)
    field_sessions = sum(1 for session in sessions if session_has_action(agent_events, session["sid"], "field"))
    return {
        "capPerDay": captures / days,
        "muPerDay": total_mu / days,
        "dstPerDay": attacks / days,
        "muPerCap": safe_ratio(total_mu, captures),
        "muPerField": safe_ratio(total_mu, fields),
        "muPerKm": safe_ratio(total_mu, speed_total_km),
        "kmPer1000mu": speed_total_km / (total_mu / 1000) if total_mu else None,
        "evPerSessH": len(agent_events) / session_hours if session_hours else None,
        "fieldsPerSess": fields / sessions_count if sessions_count else None,
        "sessWithFieldPct": field_sessions / sessions_count if sessions_count else None,
        "atkPerKm": safe_ratio(attacks, speed_total_km),
    }


def session_has_action(events: list[dict[str, Any]], sid: str, action: str) -> bool:
    return any(event.get("sid") == sid and event.get("at") == action for event in events)


def build_heatmap(events: list[dict[str, Any]]) -> dict[str, Any]:
    data = [0] * 168
    for event in events:
        ts = safe_int(event.get("t"))
        if not ts:
            continue
        local = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(DASHBOARD_TZ)
        data[local.weekday() * 24 + local.hour] += 1
    return {
        "format": HEATMAP_FORMAT,
        "weekStart": HEATMAP_WEEK_START,
        "tz": TIMEZONE_NAME,
        "data": data,
    }


def compute_speed_total_km(events: list[dict[str, Any]]) -> float:
    total = 0.0
    prior = None
    for event in sorted(events, key=lambda item: safe_int(item.get("t"))):
        if "lat" not in event or "lng" not in event:
            continue
        if prior:
            dt_ms = safe_int(event.get("t")) - safe_int(prior.get("t"))
            if dt_ms >= SPEED_FILTER["minGapMs"]:
                dist = haversine_km(float(prior["lat"]), float(prior["lng"]), float(event["lat"]), float(event["lng"]))
                kmh = dist / (dt_ms / 3600000)
                if kmh <= SPEED_FILTER["maxKmh"]:
                    total += dist
        prior = event
    return total


def build_portal_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    portals: dict[str, dict[str, Any]] = {}
    for event in events:
        portal = as_str(event.get("p")).strip()
        if not portal:
            continue
        row = portals.setdefault(
            portal,
            {
                "name": portal,
                "count": 0,
                "totalMU": 0,
                "firstTs": safe_int(event.get("t")),
                "lastTs": safe_int(event.get("t")),
                "lat": event.get("lat"),
                "lng": event.get("lng"),
                "actions": Counter(),
                "agents": Counter(),
            },
        )
        row["count"] += 1
        row["totalMU"] += safe_int(event.get("mu"))
        row["firstTs"] = min(row["firstTs"], safe_int(event.get("t")))
        row["lastTs"] = max(row["lastTs"], safe_int(event.get("t")))
        row["actions"][as_str(event.get("at"))] += 1
        row["agents"][as_str(event.get("a"))] += 1
        if row.get("lat") is None and event.get("lat") is not None:
            row["lat"] = event.get("lat")
            row["lng"] = event.get("lng")

    result = []
    for row in portals.values():
        result.append(
            {
                "name": row["name"],
                "count": row["count"],
                "totalMU": row["totalMU"],
                "firstTs": row["firstTs"],
                "lastTs": row["lastTs"],
                "lat": row.get("lat"),
                "lng": row.get("lng"),
                "actions": dict(sorted(row["actions"].items())),
                "topAgents": top_pairs(row["agents"], 5),
            }
        )
    result.sort(key=lambda item: (-item["count"], item["name"].lower()))
    return result


def build_rankings(agent_summary: list[dict[str, Any]]) -> dict[str, Any]:
    def ranked(metric: str, limit: int = 25) -> list[dict[str, Any]]:
        rows = []
        for agent in agent_summary:
            value = agent.get(metric)
            if value is None and isinstance(agent.get("ratios"), dict):
                value = agent["ratios"].get(metric)
            if value is None:
                continue
            rows.append({"name": agent["name"], "faction": agent["faction"], "value": value, "count": agent["count"]})
        return sorted(rows, key=lambda row: (-row["value"], row["name"].lower()))[:limit]

    return {
        "schema": 1,
        "eventCount": ranked("count"),
        "totalMU": ranked("totalMU"),
        "daysActive": ranked("daysActive"),
        "speedTotalKm": ranked("speedTotalKm"),
        "muPerKm": ranked("muPerKm"),
        "evPerSessH": ranked("evPerSessH"),
        "fieldsPerSess": ranked("fieldsPerSess"),
    }


def build_hall_of_fame(
    events: list[dict[str, Any]],
    agent_summary: list[dict[str, Any]],
    agent_index: dict[str, Any],
) -> dict[str, Any]:
    best_field = max((event for event in events if safe_int(event.get("mu")) > 0), key=lambda e: safe_int(e.get("mu")), default=None)
    longest_sessions = []
    for agent in agent_index["agents"]:
        for session in agent["sessions"]:
            longest_sessions.append(
                {
                    "agent": agent["name"],
                    "faction": agent["faction"],
                    "sid": session["sid"],
                    "durationMs": max(0, safe_int(session["endTs"]) - safe_int(session["startTs"])),
                    "eventCount": session["eventCount"],
                    "dayKeys": session["dayKeys"],
                }
            )
    return {
        "schema": 1,
        "bestFieldMU": {
            "agent": best_field.get("a"),
            "portal": best_field.get("p"),
            "mu": safe_int(best_field.get("mu")),
            "ts": safe_int(best_field.get("t")),
        }
        if best_field
        else None,
        "mostEventsAgents": sorted(
            [{"name": agent["name"], "faction": agent["faction"], "value": agent["count"]} for agent in agent_summary],
            key=lambda row: (-row["value"], row["name"].lower()),
        )[:25],
        "longestSessions": sorted(longest_sessions, key=lambda row: (-row["durationMs"], row["agent"].lower()))[:25],
        "mostIntenseSessions": sorted(longest_sessions, key=lambda row: (-row["eventCount"], row["agent"].lower()))[:25],
    }


def write_gzip_b64_json(path: Path, data: Any) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    gz_bytes = gzip.compress(raw_json.encode("utf-8"))
    b64 = base64.b64encode(gz_bytes).decode("ascii")
    path.write_text(b64, encoding="utf-8")
    return {
        "rawJsonBytes": len(raw_json),
        "compressedBase64Bytes": len(b64),
    }


def reset_tree(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.mkdir(parents=True, exist_ok=True)


def normalize_action_counts(actions: Counter[str]) -> dict[str, int]:
    return {
        "captures": actions.get("capture", 0),
        "deploys": actions.get("deploy", 0),
        "links": actions.get("link", 0),
        "fields": actions.get("field", 0),
        "destroys": actions.get("destroy", 0),
        "destroyLinks": actions.get("destroyLink", 0),
        "destroyFields": actions.get("destroyField", 0),
        "other": actions.get("other", 0),
    }


def normalize_kind_counts(kinds: Counter[str]) -> dict[str, int]:
    return {
        "presence": kinds.get("presence", 0),
        "attack": kinds.get("attack", 0),
        "unknown": kinds.get("unknown", 0),
    }


def top_pairs(counter: Counter[str], limit: int) -> list[list[Any]]:
    return [[key, value] for key, value in counter.most_common(limit) if key]


def most_common(values: Any) -> str:
    counter = Counter(value for value in values if value)
    return counter.most_common(1)[0][0] if counter else "UNK"


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    radius = 6371.0088
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def cooked_filters_contract() -> dict[str, Any]:
    return {
        "agentBlacklist": sorted(IGNORED_AGENT_KEYS),
        "agentKey": "NFKD, remove marks, lowercase, ASCII alnum except __name__ sentinels",
        "geo": {
            "franceBoundingBoxExcluded": {
                "latGt": 43.35,
                "lngGt": -2.0,
                "lngLt": 8.5,
            }
        },
    }


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


def is_ignored_agent(name: str) -> bool:
    return agent_key(name) in IGNORED_AGENT_KEYS


def agent_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name or "")
    no_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    lowered = no_marks.lower().strip()
    if lowered.startswith("__") and lowered.endswith("__"):
        return lowered
    return "".join(ch for ch in lowered if ch.isascii() and ch.isalnum())


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
