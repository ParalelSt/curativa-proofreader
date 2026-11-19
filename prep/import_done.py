"""Import proofread recordings packaged by :mod:`export_done`.

Usage: ``python prep/import_done.py [zip ...] [--dry-run]``. With no zip
arguments, every ``data/inbox/*.zip`` is processed.
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import sys
import wave
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"
MANIFEST = DATA / "manifest.csv"
VIDEOS = DATA / "videos.csv"
CORRECTIONS = DATA / "corrections.json"
SEGMENTS = DATA / "segments"
BACKUPS = DATA / "backups"
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CLIP_FIELDS = ["text", "video_id", "category", "start", "end", "duration",
               "speakers", "overlap", "unsure_share", "unsure_words", "keep", "drop_reason"]
MANIFEST_FIELDS = ["file_path", *CLIP_FIELDS]
VIDEO_FIELDS = ["video_id", "url", "title", "category", "duration_s", "clean_audio", "comment"]


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        return list(reader), list(reader.fieldnames or [])


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _atomic_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _validate_archive(archive: zipfile.ZipFile):
    infos = archive.infolist()
    total = 0
    texts = {}
    wav_paths = set()
    for info in infos:
        name = info.filename
        if (not name or name.startswith("/") or "\\" in name or ".." in name.split("/")
                or re.match(r"^[A-Za-z]:", name)):
            raise ValueError(f"unsafe archive entry: {name!r}")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_SIZE:
            raise ValueError("archive exceeds 2 GB uncompressed size limit")
        parts = name.split("/")
        if len(parts) == 2 and parts[1] == "texts.json" and ID_RE.fullmatch(parts[0]):
            texts[parts[0]] = info
        elif (len(parts) == 3 and parts[1] == "clips" and parts[2].endswith(".wav")
              and ID_RE.fullmatch(parts[0]) and ID_RE.fullmatch(parts[2][:-4])):
            wav_paths.add(name)
        else:
            raise ValueError(f"invalid archive entry path: {name!r}")
    if len(texts) != len([i for i in infos if i.filename.endswith("/texts.json")]):
        raise ValueError("duplicate or invalid texts.json entry")
    if not texts:
        raise ValueError("archive contains no texts.json")
    payloads = {}
    for video_id, info in texts.items():
        try:
            payload = json.loads(archive.read(info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON in {info.filename}") from exc
        if not isinstance(payload, dict) or payload.get("format") != 1:
            raise ValueError(f"{info.filename}: format must equal 1")
        if payload.get("video_id") != video_id:
            raise ValueError(f"{info.filename}: video_id does not match folder")
        clips = payload.get("clips")
        if not isinstance(clips, list):
            raise ValueError(f"{info.filename}: clips must be a list")
        listed = set()
        for clip in clips:
            if not isinstance(clip, dict):
                raise ValueError(f"{info.filename}: invalid clip record")
            filename = clip.get("file")
            if not isinstance(filename, str) or not ID_RE.fullmatch(filename[:-4]) or not filename.endswith(".wav"):
                raise ValueError(f"{info.filename}: invalid clip filename")
            archive_path = f"{video_id}/clips/{filename}"
            if archive_path not in wav_paths or archive_path in listed:
                raise ValueError(f"{info.filename}: listed clip is missing or duplicated")
            if clip.get("status") not in ("approved", "excluded"):
                raise ValueError(f"{info.filename}: clip status must be approved or excluded")
            listed.add(archive_path)
        for path in sorted(p for p in listed):
            try:
                with archive.open(path) as stream, wave.open(stream, "rb") as audio:
                    if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                        raise ValueError(f"{path}: WAV must be 16 kHz mono 16-bit")
                    audio.readframes(1)
            except (wave.Error, EOFError, OSError) as exc:
                raise ValueError(f"{path}: invalid WAV file") from exc
        payloads[video_id] = payload
    if wav_paths != {f"{vid}/clips/{clip['file']}" for vid, p in payloads.items() for clip in p["clips"]}:
        raise ValueError("archive contains an unlisted WAV")
    return payloads


def _slug(category: str) -> str:
    # Reuse prep/segment.py's folder naming logic without relying on cwd imports.
    module_path = ROOT / "prep" / "segment.py"
    spec = importlib.util.spec_from_file_location("_import_done_segment", module_path)
    module = importlib.util.module_from_spec(spec)
    # segment.py imports project modules; make prep importable in scratch roots too.
    sys.path.insert(0, str(module_path.parent))
    try:
        spec.loader.exec_module(module)
        return module.slug(category)
    finally:
        sys.path.pop(0)


def _move_zip(zip_path: Path) -> None:
    if not zip_path.resolve().is_relative_to(INBOX.resolve()):
        return
    destination_dir = INBOX / "imported"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / zip_path.name
    stem, suffix = zip_path.stem, zip_path.suffix
    number = 2
    while destination.exists():
        destination = destination_dir / f"{stem}-{number}{suffix}"
        number += 1
    shutil.move(str(zip_path), str(destination))


def import_zip(zip_path, dry_run: bool = False) -> dict:
    zip_path = Path(zip_path)
    report = {"zip": zip_path.name, "imported": [], "skipped": [], "rejected": None}
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            payloads = _validate_archive(archive)
            manifest_rows, manifest_fields = _read_csv(MANIFEST)
            video_rows, video_fields = _read_csv(VIDEOS)
            corrections = _read_json(CORRECTIONS, {})
            existing_ids = {row.get("video_id", "") for row in manifest_rows}
            existing_ids.update(row.get("video_id", "") for row in video_rows)
            pending = []
            for video_id, payload in payloads.items():
                if video_id in existing_ids:
                    report["skipped"].append({"video_id": video_id, "reason": "skipped: already in our data"})
                    continue
                pending.append((video_id, payload))
            if not pending or dry_run:
                return report
            backup_dir = BACKUPS / ("import-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            if not backup_dir.exists():
                backup_dir.mkdir(parents=True, exist_ok=True)
                for source in (MANIFEST, VIDEOS, CORRECTIONS):
                    if source.exists():
                        shutil.copy2(source, backup_dir / source.name)
            # Keep the on-disk schemas and their existing column order.
            mf = manifest_fields or MANIFEST_FIELDS
            vf = video_fields or VIDEO_FIELDS
            new_manifest, new_videos = list(manifest_rows), list(video_rows)
            new_corrections = dict(corrections)
            with zipfile.ZipFile(zip_path, "r") as archive:
                for video_id, payload in pending:
                    category = str(payload.get("category", ""))
                    folder = SEGMENTS / _slug(category) / video_id
                    folder.mkdir(parents=True, exist_ok=True)
                    approved = excluded = 0
                    for clip in payload["clips"]:
                        filename = clip["file"]
                        target = folder / filename
                        with archive.open(f"{video_id}/clips/{filename}") as source, target.open("wb") as out:
                            shutil.copyfileobj(source, out)
                        file_path = target.relative_to(ROOT).as_posix()
                        row = {key: clip.get(key, "") for key in CLIP_FIELDS}
                        row.update(file_path=file_path, text=clip.get("auto_text", ""))
                        new_manifest.append(row)
                        status = clip["status"]
                        approved += status == "approved"
                        excluded += status == "excluded"
                        new_corrections[file_path] = {
                            "auto_text": clip.get("auto_text", ""), "status": status,
                            "text": clip.get("final_text", ""), "updated": payload.get("marked_done", ""),
                            "by": "import",
                        }
                    new_videos.append({key: payload.get(key, "") for key in VIDEO_FIELDS})
                    report["imported"].append({"video_id": video_id, "clips": len(payload["clips"]),
                                               "approved": approved, "excluded": excluded})
            _atomic_csv(MANIFEST, new_manifest, mf)
            _atomic_csv(VIDEOS, new_videos, vf)
            _atomic_json(CORRECTIONS, new_corrections)
            # Keep the review server's adjacent .txt files in sync with imported
            # decisions, using the same helper it calls when saving a review.
            texts_path = ROOT / "prep" / "texts.py"
            texts_spec = importlib.util.spec_from_file_location("_import_done_texts", texts_path)
            texts_module = importlib.util.module_from_spec(texts_spec)
            texts_spec.loader.exec_module(texts_module)
            for video_id, payload in pending:
                for clip in payload["clips"]:
                    file_path = f"data/segments/{_slug(str(payload.get('category', '')))}/{video_id}/{clip['file']}"
                    if clip["status"] == "approved":
                        texts_module.write_text(file_path, clip.get("final_text", ""))
                    else:
                        texts_module.write_text(file_path, None)
        _move_zip(zip_path)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        report["imported"] = []
        report["rejected"] = str(exc)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", nargs="*", help="zip files to import")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.zip] if args.zip else sorted(INBOX.glob("*.zip"))
    for path in paths:
        report = import_zip(path, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
