# Analiticgress Cooker Architecture v3

This document is the contract for the scalable version of the Ingress COMM
pipeline. It exists so future changes do not silently change rankings, sessions,
or modal data.

## Core Principle

The browser must not load the full historical dataset.

Python cooks, filters, aggregates, and indexes. The HTML visualizes summaries and
loads raw events only for bounded operational ranges.

Target scale: 12M+ raw records.

## Stable Contracts

### Timezone

All day buckets use Europe/Madrid local dates, not UTC dates.

```json
{
  "timezone": "Europe/Madrid",
  "dayKeyAlgo": "Europe/Madrid local date"
}
```

`day_key(ts)` is the ISO date after converting the timestamp from UTC to
Europe/Madrid.

### Sessions

Sessions are computed globally in Python with:

```text
SESSION_GAP_MS = 14400000
```

Every compact event gets:

```json
{
  "sid": "s_abcdef1234",
  "dk": "2026-05-24"
}
```

If a session crosses days, events may also carry:

```json
{
  "sdk": ["2026-05-24", "2026-05-25"]
}
```

The browser must group by `sid` when available. Recomputing sessions in the
browser is legacy fallback only.

Definitive session IDs:

```py
def session_id(agent_key, session_start_ts):
    raw = f"{agent_key}|{session_start_ts}".encode("utf-8")
    return "s_" + hashlib.sha1(raw).hexdigest()[:10]
```

Open sessions are provisional until `now - lastEventTs > SESSION_GAP_MS`:

```json
{
  "sid": "s_open_4f9a21c8e2",
  "sessionOpen": true
}
```

Metadata:

```json
{
  "sidAlgo": "sha1-10",
  "openSessionSidAlgo": "sha1-10-agent-start-end",
  "sessionGapMs": 14400000
}
```

### Filters

The source of truth for filtering is Python.

Agent blacklist is applied before references, shards, and summaries are written.
The blacklist uses a normalized `agent_key`: NFKD normalization, combining marks
removed, lowercase, then ASCII alphanumeric characters, with `__ada__` and
`__jarvis__` kept as sentinels.

The France geographic exclusion is also applied in Python:

```text
lat > 43.35 and -2.0 < lng < 8.5
```

The client can keep defensive filters, but cooked files must already be clean.

### Heatmap

Agent heatmaps use one format:

```json
{
  "format": "weekday-hour-flat-168",
  "weekStart": "monday",
  "tz": "Europe/Madrid",
  "data": [0, 0, 0]
}
```

Index formula:

```text
index = weekday * 24 + hour
```

Monday is 0.

### Speed

Distance metrics must avoid teleport jumps.

```json
{
  "speedFilter": {
    "maxKmh": 200,
    "minGapMs": 60000
  }
}
```

Segments above this speed or below this gap are excluded from `speedTotalKm`.

### Relationships

Relationship metrics are precomputed in Python as top-N lists.

`topCollab`: same faction, co-presence in a global session or nearby operational
window.

`topRivals`: opposite faction, same portal or zone inside a 30 minute window.

Shape:

```json
{
  "topCollab": [["AgentA", 45], ["AgentB", 31]],
  "topRivals": [["EnemyA", 18]]
}
```

## Summary Agent Shape

`summary_agents` must stay small enough to load on page start. It must not
include a full `days[]` list for every agent.

Use:

```json
{
  "name": "Agent",
  "faction": "ENL",
  "count": 1000,
  "totalMU": 123456,
  "firstTs": 1710000000000,
  "lastTs": 1770000000000,
  "daysActive": 421,
  "actions": {
    "captures": 10,
    "deploys": 20,
    "links": 30,
    "fields": 40,
    "destroys": 50,
    "destroyLinks": 60,
    "destroyFields": 70
  },
  "eventKinds": {
    "presence": 100,
    "attack": 50,
    "unknown": 0
  },
  "topPortals": [["Portal 1", 50], ["Portal 2", 31]],
  "topZones": [["Barcelona", 120], ["Terrassa", 42]],
  "heatmap7x24": {
    "format": "weekday-hour-flat-168",
    "weekStart": "monday",
    "tz": "Europe/Madrid",
    "data": []
  },
  "ratios": {
    "capPerDay": 0.0,
    "muPerDay": 0.0,
    "dstPerDay": 0.0,
    "muPerCap": null,
    "muPerField": null,
    "muPerKm": null,
    "kmPer1000mu": null,
    "evPerSessH": null,
    "fieldsPerSess": null,
    "sessWithFieldPct": null,
    "atkPerKm": null
  }
}
```

Unknown or unavailable metrics are `null`. The client displays a dash, not 0.

`actions` are normalized UI verbs. `eventKinds` are semantic groups.

## Future Compiled Layout

The current capsule remains for `index_42` during transition:

```text
compiled/cooked_meta.json
compiled/cooked_chunk_000.txt
```

The scalable layout for `index_43`:

```text
compiled/meta.json
compiled/events/YYYY/MM/YYYY-MM-DD.json.gz.b64.txt
compiled/summaries/global.json.gz.b64.txt
compiled/summaries/months.json.gz.b64.txt
compiled/summaries/agents.json.gz.b64.txt
compiled/summaries/portals.json.gz.b64.txt
compiled/summaries/rankings.json.gz.b64.txt
compiled/summaries/hall_of_fame.json.gz.b64.txt
compiled/indexes/agents.json.gz.b64.txt
```

Days that have 0 valid events after filters are omitted from `meta.days[]`.

All client fetches use versioned URLs:

```js
`${path}?v=${meta.builtAt}`
```

## UI Data Sources

| View | Source |
| --- | --- |
| Global KPIs | `summaries/global` |
| Historical rankings | `summaries/rankings` |
| Hall of Fame | `summaries/hall_of_fame` |
| Agent modal basics | `summaries/agents` |
| Agent modal map/log/sessions | Events for active range, default max 90 days |
| Session modal | Events for the session `dayKeys` |
| Main map | Events for active range, max 90 days |
| Log | Events for active range, max 90 days |
| Agent heatmap | `summaries/agents` |
| Full history | Summaries only, never raw events |

The raw "TODO" button must not exist in the scalable UI. Full historical views
come from summaries.

## Client Loading Rules

`index_43` starts from summaries plus a bounded event range, normally the latest
30 days.

The client keeps:

```js
dayCache = {}
loadedDays = new Set()
currentLoadToken = 0
```

Every filter change increments `currentLoadToken`. Finished loads whose token is
stale are discarded.

Each shard fetch retries twice. If any requested day still fails, render only
loaded data and show a visible warning with missing day keys.

The app polls `meta.json?v=Date.now()` every 5-10 minutes. If `builtAt` changes,
show "New data available, reload" without auto-reloading.

## GitHub Actions

Workflow concurrency must prevent overlapping cooks:

```yaml
concurrency:
  group: ingress-cook
  cancel-in-progress: false
```

Raw Drive JSON files are treated as immutable daily logs.

Future incremental sync/cook uses:

```text
cooker/cache/drive_manifest.json
```

Manifest entries should include `fileId`, `name`, `modifiedTime`, `size`, and
`md5Checksum` when Drive provides it.

## Transition Rule

`index_42` uses the new cleaned cooked data. No double pipeline.

If `index_42` shows fewer events because Python filtered ignored agents or
France-bound coordinates, that is expected behavior.
