#!/usr/bin/env python3
"""
Download raw Ingress records JSON files from a Google Drive folder.

Designed for GitHub Actions with a service account:
  GOOGLE_SERVICE_ACCOUNT_JSON      full service-account JSON, or base64-encoded JSON
  GOOGLE_DRIVE_SOURCE_FOLDER_ID    Drive folder containing *records*.json files

The Drive folder must be shared with the service-account email.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DEFAULT_PATTERN = r".*records.*\.json$"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync raw records JSON files from Google Drive.")
    parser.add_argument("--folder-id", default=os.getenv("GOOGLE_DRIVE_SOURCE_FOLDER_ID"), help="Google Drive source folder ID.")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "raw", help="Output directory.")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help="Case-insensitive filename regex.")
    parser.add_argument("--clean", action="store_true", help="Delete existing matching JSON files in out-dir before sync.")
    parser.add_argument("--service-account-file", type=Path, default=None, help="Path to service-account JSON file.")
    args = parser.parse_args()

    if not args.folder_id:
        raise SystemExit("Missing --folder-id or GOOGLE_DRIVE_SOURCE_FOLDER_ID.")

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(args.pattern, re.IGNORECASE)

    if args.clean:
        for path in out_dir.glob("*.json"):
            path.unlink()

    service = build_drive_service(args.service_account_file)
    files = list_drive_files(service, args.folder_id, pattern)
    downloaded = []

    for file_info in files:
        target = out_dir / safe_filename(file_info["name"])
        download_file(service, file_info["id"], target)
        downloaded.append(
            {
                "id": file_info["id"],
                "name": file_info["name"],
                "modifiedTime": file_info.get("modifiedTime"),
                "size": int(file_info.get("size") or 0),
                "path": str(target),
            }
        )

    meta = {
        "syncedAt": datetime.now(timezone.utc).isoformat(),
        "folderId": args.folder_id,
        "outDir": str(out_dir),
        "files": downloaded,
        "fileCount": len(downloaded),
    }
    (out_dir / "drive_sync_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outDir": str(out_dir), "fileCount": len(downloaded)}, ensure_ascii=False, indent=2))
    return 0


def build_drive_service(service_account_file: Path | None):
    if service_account_file:
        info = json.loads(service_account_file.read_text(encoding="utf-8"))
    else:
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise SystemExit("Missing --service-account-file or GOOGLE_SERVICE_ACCOUNT_JSON.")
        info = parse_service_account_json(raw)

    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def parse_service_account_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    decoded = base64.b64decode(raw).decode("utf-8")
    return json.loads(decoded)


def list_drive_files(service, folder_id: str, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                orderBy="name",
                pageSize=1000,
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        for item in response.get("files", []):
            name = item.get("name", "")
            mime_type = item.get("mimeType", "")
            if mime_type.startswith("application/vnd.google-apps."):
                continue
            if pattern.match(name):
                files.append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def download_file(service, file_id: str, target: Path) -> None:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    target.write_bytes(buffer.getvalue())


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)


if __name__ == "__main__":
    raise SystemExit(main())
