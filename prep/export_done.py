"""Mark fully proofread recordings as done and package them for delivery."""
import csv
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DONE = ROOT / "done"
OUTBOX = ROOT / "outbox"
PACKAGED = ROOT / "data" / "packaged.json"
MANIFEST = ROOT / "data" / "manifest.csv"
VIDEOS = ROOT / "data" / "videos.csv"
CORRECTIONS = ROOT / "data" / "corrections.json"


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _status(row: dict, corrections: dict) -> tuple[str, str]:
    """Mirror review.server.state_of, including its stale-edit check."""
    correction = corrections.get(row["file_path"])
    if not correction:
        return "todo", row["text"]
    if correction["auto_text"] != row["text"]:
        return "stale", row["text"]
    return correction["status"], correction["text"]


def progress(video_id: str) -> dict:
    rows = [row for row in _read_csv(MANIFEST) if row["video_id"] == video_id]
    corrections = _read_json(CORRECTIONS, {})
    decided = sum(_status(row, corrections)[0] in ("approved", "excluded") for row in rows)
    return {"total": len(rows), "decided": decided}


def is_done(video_id: str) -> bool:
    return (DONE / video_id / "texts.json").exists()


def packaged_ids() -> set[str]:
    """Return recording IDs included in a previously created package."""
    return set(_read_json(PACKAGED, []))


def mark_done(video_id: str) -> dict:
    rows = [row for row in _read_csv(MANIFEST) if row["video_id"] == video_id]
    status_progress = progress(video_id)
    if status_progress["total"] == 0 or status_progress["decided"] != status_progress["total"]:
        raise ValueError("Some clips are not decided yet.")

    corrections = _read_json(CORRECTIONS, {})
    video = next((item for item in _read_csv(VIDEOS) if item["video_id"] == video_id), {})
    folder = DONE / video_id
    if folder.exists():
        shutil.rmtree(folder)
    clips_dir = folder / "clips"
    clips_dir.mkdir(parents=True)

    clips = []
    for row in rows:
        status, final_text = _status(row, corrections)
        filename = Path(row["file_path"]).name
        shutil.copy2(ROOT / row["file_path"], clips_dir / filename)
        clips.append({
            **{key: value for key, value in row.items() if key != "file_path"},
            "file": filename,
            "status": status,
            "final_text": final_text,
            "auto_text": row["text"],
        })

    payload = {
        "format": 1,
        "video_id": video_id,
        "title": video.get("title", ""),
        "url": video.get("url", ""),
        "category": video.get("category", ""),
        "duration_s": video.get("duration_s", ""),
        "clean_audio": video.get("clean_audio", ""),
        "comment": video.get("comment", ""),
        "marked_done": datetime.now().isoformat(timespec="seconds"),
        "clips": clips,
    }
    (folder / "texts.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {"video_id": video_id, "clips": len(clips), "folder": folder.relative_to(ROOT).as_posix()}


def _write_packaged(ids: list[str]) -> None:
    PACKAGED.parent.mkdir(parents=True, exist_ok=True)
    temporary = PACKAGED.with_suffix(PACKAGED.suffix + ".tmp")
    temporary.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, PACKAGED)


def reopen(video_id: str) -> None:
    folder = DONE / video_id
    if folder.exists():
        shutil.rmtree(folder)
    packaged = _read_json(PACKAGED, [])
    if video_id in packaged:
        _write_packaged([item for item in packaged if item != video_id])


def package() -> dict:
    packaged = _read_json(PACKAGED, [])
    already = set(packaged)
    new_folders = sorted(
        (folder for folder in DONE.iterdir() if folder.is_dir() and (folder / "texts.json").exists()
         and folder.name not in already),
        key=lambda folder: folder.name,
    ) if DONE.exists() else []
    if not new_folders:
        return {"zip": None, "recordings": 0, "message": "Nothing new to package."}

    OUTBOX.mkdir(parents=True, exist_ok=True)
    stem = "proofread-" + datetime.now().strftime("%Y%m%d-%H%M")
    zip_path = OUTBOX / f"{stem}.zip"
    suffix = 2
    while zip_path.exists():
        zip_path = OUTBOX / f"{stem}-{suffix}.zip"
        suffix += 1
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for folder in new_folders:
            archive.write(folder / "texts.json", f"{folder.name}/texts.json")
            for clip in sorted((folder / "clips").glob("*.wav")):
                archive.write(clip, f"{folder.name}/clips/{clip.name}")

    ids = packaged + [folder.name for folder in new_folders]
    _write_packaged(ids)
    return {
        "zip": zip_path.relative_to(ROOT).as_posix(),
        "recordings": len(new_folders),
        "message": f"Packaged {len(new_folders)} recording(s).",
    }


def state(video_id: str) -> str:
    if video_id in packaged_ids() and is_done(video_id):
        return "sent"
    if is_done(video_id):
        return "done"
    if progress(video_id)["decided"] > 0:
        return "in_progress"
    return "not_started"
