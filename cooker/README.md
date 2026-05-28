# Ingress Static Cooker

This folder is the Python replacement for the heavy Apps Script cooking step.

The long-term scalable data contract lives in `ARCHITECTURE_V3.md`. Any change
to sessions, day buckets, summaries, shards, filters, or rankings should be
checked against that document first.

## Layout

```text
cooker/
  cook.py
  raw/
    2026-05-21-records.json
  references/
    agents.json
    portals.json
  compiled/
    build_meta.json
```

## Local Build

Run:

```bash
python cook.py --raw-dir "C:\Users\efernandez\Desktop\ING\logs\JSONs"
```

The script reads `raw/*records*.json`, deduplicates records by `uuid`, then rebuilds bot-compatible:

- `references/agents.json`
- `references/portals.json`

It also writes a web-ready payload compatible with the current dashboard loader:

- `compiled/cooked_meta.json`
- `compiled/cooked_chunk_000.txt`
- `compiled/cooked_chunk_001.txt`
- ...
- `compiled/build_meta.json`

And, in parallel, it writes the scalable v3 layout for the future summary-first
dashboard:

- `compiled/meta.json`
- `compiled/events/YYYY/MM/YYYY-MM-DD.json.gz.b64.txt`
- `compiled/summaries/global.json.gz.b64.txt`
- `compiled/summaries/months.json.gz.b64.txt`
- `compiled/summaries/agents.json.gz.b64.txt`
- `compiled/summaries/portals.json.gz.b64.txt`
- `compiled/summaries/rankings.json.gz.b64.txt`
- `compiled/summaries/hall_of_fame.json.gz.b64.txt`
- `compiled/indexes/agents.json.gz.b64.txt`

`index_42.html` still uses the legacy `cooked_*` files. `index_43.html` should
use the scalable layout.

## GitHub Build

In GitHub, the daily workflow is:

```text
Google Drive JSON folder
  -> cooker/sync_drive.py
  -> cooker/raw/        (temporary, ignored by git)
  -> cooker/cook.py
  -> cooker/references/
  -> cooker/compiled/
  -> commit to GitHub
```

Required repository secrets:

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_DRIVE_SOURCE_FOLDER_ID
```

`GOOGLE_SERVICE_ACCOUNT_JSON` can be either the full service-account JSON or a base64-encoded version of it.

The Google Drive folder containing the `*-records.json` files must be shared with the service-account email.

Manual GitHub Actions run:

```text
Actions -> Cook Ingress data -> Run workflow
```
