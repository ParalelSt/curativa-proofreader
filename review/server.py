"""Proofreading GUI for the transcribed segments.

Usage:
  .venv/bin/python review/server.py          # opens http://127.0.0.1:8765

Edits are saved to data/corrections.json. Training and evaluation read them
automatically (train/common.py), so an approved segment is used with your text
and an excluded one is left out. Nothing else needs to be run after editing.
"""
import csv
import json
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "prep"))
import add_recording  # noqa: E402
import complete_recording  # noqa: E402
import export_done  # noqa: E402
import import_done  # noqa: E402
import tools  # noqa: E402
from texts import write_text  # noqa: E402

MANIFEST = ROOT / "data" / "manifest.csv"
VIDEOS = ROOT / "data" / "videos.csv"
SPLITS = ROOT / "data" / "splits.json"
CORRECTIONS = ROOT / "data" / "corrections.json"
HINTS = ROOT / "data" / "hints.json"  # model predictions, from train/make_hints.py
GRAMMAR = ROOT / "data" / "grammar.json"  # spelling/grammar fixes: {file: [{find, to, why}]}
SEGMENTS = (ROOT / "data" / "segments").resolve()
PAGE = Path(__file__).with_name("index.html")
PORT = int(os.environ.get("PROOFREAD_PORT", "8765"))
LOCK = threading.Lock()
JOBS: list[dict] = []
JOBS_LOCK = threading.Lock()
DRAFTS = ROOT / "data" / "drafts.json"         # typed but not approved text, saved while typing
STATE = ROOT / "data" / "review_state.json"    # last open segment and "Keep mine" choices
HIDDEN = ROOT / "data" / "hidden_videos.json"   # finished recordings kept out of the list


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file


def read_corrections() -> dict:
    return json.loads(CORRECTIONS.read_text(encoding="utf-8")) if CORRECTIONS.exists() else {}


def write_corrections(data: dict) -> None:
    tmp = CORRECTIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, CORRECTIONS)  # atomic: a crash never leaves a half-written file


def state_of(row: dict, corrections: dict) -> dict:
    c = corrections.get(row["file_path"])
    if not c:
        return {"status": "todo", "text": row["text"]}
    if c["auto_text"] != row["text"]:
        # The segment was re-cut since this edit; its audio no longer matches.
        return {"status": "stale", "text": row["text"], "old_text": c["text"]}
    return {"status": c["status"], "text": c["text"]}


def videos_payload() -> list[dict]:
    rows = read_csv(MANIFEST)
    info = {v["video_id"]: v for v in read_csv(VIDEOS)}
    test = set(json.loads(SPLITS.read_text(encoding="utf-8"))["test"]) if SPLITS.exists() else set()
    corrections = read_corrections()
    packaged = export_done.packaged_ids()
    out = {}
    for row in rows:
        vid = row["video_id"]
        v = out.setdefault(vid, {
            "video_id": vid,
            "title": info.get(vid, {}).get("title", vid),
            "category": row["category"],
            "state": "sent" if vid in packaged and export_done.is_done(vid)
                     else "done" if export_done.is_done(vid)
                     else "not_started",
            "split": "test" if vid in test else "train",
            "total": 0, "done": 0, "minutes": 0.0,
        })
        v["total"] += 1
        v["minutes"] += float(row["duration"]) / 60
        if state_of(row, corrections)["status"] in ("approved", "excluded"):
            v["done"] += 1
    for item in out.values():
        if item["state"] == "not_started" and item["done"] > 0:
            item["state"] = "in_progress"
    return sorted(out.values(), key=lambda v: (v["split"] != "test", v["category"], v["title"]))


def status_payload() -> dict:
    try:
        sheet = add_recording.load_sheet(refresh=True)
    except Exception:
        return {"rows": [], "sheet_ok": False}
    local = {item["video_id"]: item for item in videos_payload()}
    rows = []
    for item in sheet:
        recording = local.get(item["video_id"])
        rows.append({
            "url": item["url"], "title": item["title"], "category": item["category"],
            "sheet_status": item["status"] if item["status"] in ("free", "in_progress", "done") else "in_progress",
            "person": item["person"],
            "local": recording["state"] if recording else "",
            "progress": f"{recording['done']}/{recording['total']}" if recording else "",
        })
    return {"rows": rows, "sheet_ok": True}


def grammar_for(entry, file: str, corrections: dict, auto_text: str) -> list[dict]:
    if not entry:
        return []
    if isinstance(entry, list):  # older hand-written format
        return entry
    c = corrections.get(file)
    current = c["text"] if c and c["auto_text"] == auto_text else auto_text
    return entry["fixes"] if entry.get("checked") == current else []


def segments_payload(video_id: str) -> list[dict]:
    corrections = read_corrections()
    hints = json.loads(HINTS.read_text(encoding="utf-8")) if HINTS.exists() else {}
    grammar = json.loads(GRAMMAR.read_text(encoding="utf-8")) if GRAMMAR.exists() else {}
    drafts = read_json(DRAFTS)
    out = []
    for row in read_csv(MANIFEST):
        if row["video_id"] != video_id:
            continue
        start = float(row["start"])
        unsure = []
        for item in filter(None, row.get("unsure_words", "").split("; ")):
            word, _, at = item.rpartition(" @")
            unsure.append({"word": word, "offset": max(0.0, float(at.rstrip("s")) - start)})
        out.append({
            "file": row["file_path"],
            "auto_text": row["text"],
            "start": start,
            "duration": float(row["duration"]),
            "speakers": int(row["speakers"]),
            "overlap": row["overlap"] == "1",
            "auto_keep": row["keep"] == "1",
            "drop_reason": row["drop_reason"],
            "unsure": unsure,
            "hint": hints.get(row["file_path"]),
            # A draft only counts while the segment still has the text it was typed against.
            "draft": (drafts.get(row["file_path"]) or {}).get("text")
                     if (drafts.get(row["file_path"]) or {}).get("auto_text") == row["text"] else None,
            # grammar_check.py stores {checked, model, fixes}; only show fixes made for this exact text.
            "grammar": grammar_for(grammar.get(row["file_path"]), row["file_path"], corrections, row["text"]),
            **state_of(row, corrections),
        })
    return out


def missing_payload() -> dict:
    """Recordings (hidden ones too) with audio that was never downloaded. Reads metadata only: no audio, no paid call."""
    try:
        return {"rows": complete_recording.check()}
    except Exception:
        return {"rows": [], "error": "Could not check for missing parts."}


def queue_complete(video_id: str) -> tuple[dict, int]:
    """Queue a job that adds the missing parts of one recording (transcribing them is a paid ElevenLabs call)."""
    if not tools.has_key():
        return {"error": "Paste the ElevenLabs key first."}, 400
    row = next((item for item in read_csv(VIDEOS) if item["video_id"] == video_id), None)
    if row is None or re.search(r"-p\d+$", video_id):
        return {"error": "Choose a recording from the list."}, 400
    with JOBS_LOCK:
        queued = 0
        if not any(job["video_id"] == video_id and job["step"] not in ("ready", "failed") for job in JOBS):
            JOBS.append({"id": max((job["id"] for job in JOBS), default=0) + 1, "kind": "complete",
                         "url": row["url"], "video_id": video_id, "title": f"{row['title']} (missing parts)",
                         "category": row["category"], "step": "queued", "error": "", "started": None,
                         "finished": None, "clips": None})
            queued = 1
        return {"queued": queued, "jobs": jobs_payload_locked()}, 200


def run_job(job: dict, on_step) -> tuple[str, int]:
    if job.get("kind") == "complete":
        result = complete_recording.complete(job["video_id"], on_step=on_step)
        return result["video_id"], sum(part.get("clips", 0) for part in result["parts"])
    result = add_recording.add(job["url"], on_step=on_step)
    return result["video_id"], result["clips"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/videos":
            # Recordings listed in data/hidden_videos.json stay in the data (and in training) but are not shown.
            hidden = set(read_json(HIDDEN)) if HIDDEN.exists() else set()
            self.send_json([v for v in videos_payload() if v["video_id"] not in hidden])
        elif url.path == "/api/missing":
            self.send_json(missing_payload())
        elif url.path == "/api/status":
            self.send_json(status_payload())
        elif url.path == "/api/key":
            self.send_json({"set": tools.has_key()})
        elif url.path == "/api/jobs":
            self.send_json({"jobs": jobs_payload()})
        elif url.path == "/api/state":
            self.send_json(read_json(STATE))
        elif url.path == "/api/inbox":
            inbox = ROOT / "data" / "inbox"
            self.send_json({"zips": sorted(p.name for p in inbox.glob("*.zip")) if inbox.exists() else []})
        elif url.path == "/api/segments":
            self.send_json(segments_payload(parse_qs(url.query).get("video", [""])[0]))
        elif url.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif url.path.startswith("/audio/"):
            self.send_audio(unquote(url.path[len("/audio/"):]))
        else:
            self.send_error(404)

    def send_audio(self, relative: str):
        path = (ROOT / relative).resolve()
        if SEGMENTS not in path.parents or path.suffix != ".wav" or not path.exists():
            return self.send_error(404)
        data = path.read_bytes()
        start, end = 0, len(data) - 1
        match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match:  # browsers need range requests to seek inside the audio
            start = int(match.group(1) or 0)
            end = int(match.group(2) or end)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(data[start:end + 1])

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/import-upload":
            query = parse_qs(urlparse(self.path).query)
            raw_name = query.get("name", [""])[0]
            if not raw_name.endswith(".zip"):
                return self.send_json({"error": "Choose a .zip file."}, 400)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > 2 * 1024 * 1024 * 1024:
                return self.send_json({"error": "The file is too large."}, 400)
            data = self.rfile.read(length)
            name = os.path.basename(raw_name.replace("\\", "/"))
            name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            inbox = ROOT / "data" / "inbox"
            with LOCK:
                inbox.mkdir(parents=True, exist_ok=True)
                destination = inbox / name
                stem, suffix = destination.stem, destination.suffix
                number = 2
                while destination.exists():
                    destination = inbox / f"{stem}-{number}{suffix}"
                    number += 1
                destination.write_bytes(data)
            return self.send_json({"saved": destination.name})
        if path == "/api/import":
            inbox = ROOT / "data" / "inbox"
            with LOCK:
                reports = [import_done.import_zip(zip_path) for zip_path in sorted(inbox.glob("*.zip"))] if inbox.exists() else []
            return self.send_json({"reports": reports})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if path == "/api/done":
            try:
                return self.send_json(export_done.mark_done(body["video"]))
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
        if path == "/api/reopen":
            export_done.reopen(body["video"])
            return self.send_json({"ok": True})
        if path == "/api/package":
            return self.send_json(export_done.package())
        if path == "/api/key":
            try:
                tools.save_key(body.get("key", ""))
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            return self.send_json({"set": True})
        if path == "/api/check":
            urls = body.get("urls")
            if urls is None:
                urls = body.get("text", "").splitlines()
            results = add_recording.check(urls)
            with JOBS_LOCK:
                pending = {job["video_id"] or job["url"] for job in JOBS
                           if job["step"] not in ("ready", "failed")}
                for result in results:
                    if result.get("video_id") in pending or result.get("url") in pending:
                        result["state"] = "here"
                        result["message"] = "Already being added."
            return self.send_json({"results": results})
        if path == "/api/add":
            if not tools.has_key():
                return self.send_json({"error": "Paste the ElevenLabs key first."}, 400)
            results = add_recording.check(body.get("urls", []))
            with JOBS_LOCK:
                queued = 0
                for result in results:
                    if result.get("state") != "ready":
                        continue
                    if any(job["video_id"] == result.get("video_id") and job["step"] not in ("ready", "failed") for job in JOBS):
                        continue
                    JOBS.append({"id": max((job["id"] for job in JOBS), default=0) + 1,
                                 "url": result["url"], "video_id": result.get("video_id"),
                                 "title": result.get("title", ""), "category": result.get("category", ""),
                                 "step": "queued", "error": "", "started": None, "finished": None,
                                 "clips": None})
                    queued += 1
                jobs = jobs_payload_locked()
            return self.send_json({"queued": queued, "jobs": jobs})
        if path == "/api/complete":
            payload, code = queue_complete(body.get("video", ""))
            return self.send_json(payload, code)
        if path == "/api/retry":
            with JOBS_LOCK:
                job = next((job for job in JOBS if job["id"] == body.get("id")), None)
                if job and job["step"] == "failed":
                    job.update(step="queued", error="", started=None, finished=None, clips=None)
                jobs = jobs_payload_locked()
            return self.send_json({"jobs": jobs})
        if path == "/api/draft":
            with LOCK:
                drafts = read_json(DRAFTS)
                if body.get("text") is None:
                    drafts.pop(body["file"], None)
                else:
                    drafts[body["file"]] = {"text": body["text"], "auto_text": body["auto_text"],
                                            "updated": datetime.now().isoformat(timespec="seconds")}
                write_json(DRAFTS, drafts)
            return self.send_json({"ok": True})
        if path == "/api/state":
            with LOCK:
                state = read_json(STATE)
                if "last" in body:
                    state["last"] = body["last"]
                if "hints" in body:
                    state["hints"] = bool(body["hints"])
                if "dismiss" in body:
                    d = body["dismiss"]
                    keys = state.setdefault("dismissed", {}).setdefault(d["file"], [])
                    if d["key"] not in keys:
                        keys.append(d["key"])
                write_json(STATE, state)
            return self.send_json({"ok": True})
        if path != "/api/save":
            return self.send_error(404)
        status = body["status"]
        if status not in ("approved", "excluded", "todo"):
            return self.send_json({"error": "bad status"}, 400)
        with LOCK:
            corrections = read_corrections()
            if status == "todo":
                corrections.pop(body["file"], None)
            else:
                corrections[body["file"]] = {
                    "text": body["text"].strip(),
                    "auto_text": body["auto_text"],
                    "status": status,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                }
            write_corrections(corrections)
            # Keep the .txt next to the segment's .wav in step with the decision.
            write_text(body["file"], {"approved": body["text"], "excluded": None, "todo": body["auto_text"]}[status])
            drafts = read_json(DRAFTS)  # an approved or excluded segment has no draft left
            if drafts.pop(body["file"], None) is not None:
                write_json(DRAFTS, drafts)
        self.send_json({"ok": True})


def jobs_payload_locked() -> list[dict]:
    now = datetime.now().timestamp()
    return [{**job, "seconds": max(0, int((job["finished"] or now) - job["started"])) if job["started"] is not None else 0}
            for job in sorted(JOBS, key=lambda item: item["id"], reverse=True)]


def jobs_payload() -> list[dict]:
    with JOBS_LOCK:
        return jobs_payload_locked()


def job_worker() -> None:
    while True:
        with JOBS_LOCK:
            job = next((item for item in JOBS if item["step"] == "queued"), None)
            if job is not None:
                job["started"] = datetime.now().timestamp()
                job["step"] = "downloading"
        if job is None:
            threading.Event().wait(0.25)
            continue
        def on_step(step):
            with JOBS_LOCK:
                job["step"] = step
        try:
            video_id, clips = run_job(job, on_step)
            with JOBS_LOCK:
                job["video_id"] = video_id
                job["clips"] = clips
                job["step"] = "ready"
                job["finished"] = datetime.now().timestamp()
        except Exception as error:
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else "Something went wrong. Details are in data/tool.log."
            with JOBS_LOCK:
                job["step"] = "failed"
                job["error"] = message
                job["finished"] = datetime.now().timestamp()
            log_path = ROOT / "data" / "tool.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n")
                import traceback
                traceback.print_exc(file=log)


def main() -> None:
    threading.Thread(target=job_worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Proofreading at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv and os.environ.get("PROOFREAD_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
