"""Add a YouTube recording from the proofreading sheet to the dataset."""
import csv
import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

try:
    from prep import download, segment, tools, transcribe
except ImportError:
    import download
    import segment
    import tools
    import transcribe

ROOT = Path(__file__).resolve().parent.parent
SHEET_URL = "https://docs.google.com/spreadsheets/d/1nNmUwbvTH7la1dxs2x7g6mVO60AspPJi_lYNMaf_lmc/export?format=csv"
SHEET_CACHE = ROOT / "data" / "sheet.csv"
DEFAULT_START = 60
DEFAULT_MINUTES = 22


def video_id_of(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
        else:
            return None
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def _parse_sheet(content: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(content)))
    header_index = next(i for i, row in enumerate(rows) if "Link" in row)
    header = rows[header_index]
    status_column = next((name for name in header if name.startswith("Status Proof")), None)
    person_column = next((name for name in header if name.startswith("Osoba")), None)
    items = []
    for row in rows[header_index + 1:]:
        record = dict(zip(header, row))
        url = record.get("Link", "").strip()
        video_id = video_id_of(url)
        if not url or video_id is None:
            continue
        raw_status = record.get(status_column, "").strip() if status_column else ""
        normalized = raw_status.casefold()
        status = {"": "free", "ne": "free", "u tijeku": "in_progress", "da": "done"}.get(normalized, "in_progress")
        items.append({
            "video_id": video_id,
            "url": url,
            "title": record.get("Naslov", "").strip(),
            "category": record.get("Kategorija", "").strip(),
            "duration": record.get("Trajanje", "").strip(),
            "clean_audio": record.get("Čist zvuk", "").strip(),
            "comment": record.get("Komentari", "").strip(),
            "status": status,
            "person": record.get(person_column, "").strip() if person_column else "",
        })
    return items


def load_sheet(refresh: bool = True) -> list[dict]:
    if refresh:
        try:
            response = requests.get(SHEET_URL, timeout=20)
            response.raise_for_status()
            content = response.content
            SHEET_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SHEET_CACHE.write_bytes(content)
        except requests.RequestException:
            pass
    return _parse_sheet(SHEET_CACHE.read_text(encoding="utf-8-sig"))


def check(urls: list[str], refresh: bool = True) -> list[dict]:
    sheet = load_sheet(refresh)
    by_id = {row["video_id"]: row for row in sheet}
    present = download.load_index()
    seen = set()
    results = []
    for raw_url in urls:
        url = raw_url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        video_id = video_id_of(url)
        item = by_id.get(video_id, {}) if video_id else {}
        title, category, duration, person = (item.get(k, "") for k in ("title", "category", "duration", "person"))
        if video_id is None:
            state, message = "invalid", "Not a YouTube link."
        elif video_id in present:
            state, message = "here", "Already in this tool."
        elif video_id not in by_id:
            state, message = "unknown", "This link is not in the sheet."
        elif item["status"] == "done":
            state, message = "done", "Already proofread" + (f" by {person}" if person else "")
        elif item["status"] == "in_progress":
            state, message = "taken", "In progress" + (f" by {person}" if person else "")
        else:
            state, message = "ready", "Ready to add."
        results.append({"url": url, "video_id": video_id, "title": title, "category": category,
                        "duration": duration, "state": state, "person": person, "message": message})
    return results


def _is_connection_error(error: BaseException) -> bool:
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    return error.__class__.__module__.startswith("yt_dlp") and any(
        word in str(error).casefold() for word in ("network", "connection", "unable to download", "http error", "timed out")
    )


def add(url: str, on_step=None, start: float = DEFAULT_START,
        minutes: float | None = DEFAULT_MINUTES) -> dict:
    video_id = video_id_of(url)
    if video_id is None:
        raise ValueError("Not a YouTube link.")
    rows = load_sheet()
    row = next((item for item in rows if item["video_id"] == video_id), None)
    item = {"url": url, "category": row["category"] if row else "",
            "clean_audio": row["clean_audio"] if row else "",
            "comment": row["comment"] if row else ""}
    wav = download.RAW / f"{video_id}.wav"
    transcript_path = transcribe.TRANSCRIPTS / f"{video_id}.json"
    try:
        if not wav.exists():
            if on_step:
                on_step("downloading")
            download.download(item, download.load_index(), start, minutes)
        if not transcript_path.exists():
            if on_step:
                on_step("transcribing")
            key = transcribe.read_key()
            if not key:
                raise RuntimeError("The ElevenLabs key is missing. Paste it in the tool first.")
            try:
                result = transcribe.transcribe(wav, key)
            except Exception as error:
                if "401" in str(error) and "http" in str(error).casefold():
                    raise RuntimeError("The ElevenLabs key was not accepted.") from None
                raise
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        segment_root = getattr(segment, "SEGMENTS", ROOT / "data" / "segments")
        existing_clips = list(segment_root.glob(f"*/{video_id}/*.wav"))
        if existing_clips:
            clips = len(existing_clips)
        else:
            if on_step:
                on_step("cutting")
            clips = segment.run({video_id})
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) in {
            "The ElevenLabs key is missing. Paste it in the tool first.",
            "The ElevenLabs key was not accepted.",
        }:
            raise
        if _is_connection_error(error):
            raise RuntimeError("No internet connection, or the video could not be reached.") from None
        raise
    if on_step:
        on_step("ready")
    return {"video_id": video_id, "clips": clips}
