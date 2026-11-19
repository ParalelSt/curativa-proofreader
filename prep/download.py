"""Step 1: download YouTube audio and convert it to 16 kHz mono WAV.

Usage:
  python prep/download.py <url> [<url> ...] --category Neurologija
  python prep/download.py --csv data/sheet.csv --ids id1,id2 --start 60 --max-minutes 22
  python prep/download.py --csv sheet.csv [--limit 3] [--ids id1,id2]

Output:
  data/raw/<video_id>.wav   16 kHz, mono, 16-bit PCM
  data/videos.csv           one row per video (id, url, title, category, ...)
Videos that already have a WAV are skipped, so it is safe to rerun.
"""
import argparse
import csv
import subprocess
import sys
from pathlib import Path

from yt_dlp import YoutubeDL

try:
    from prep import tools
except ImportError:
    import tools

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SOURCE = RAW / "source"
VIDEOS_CSV = ROOT / "data" / "videos.csv"
FIELDS = ["video_id", "url", "title", "category", "duration_s", "clean_audio", "comment"]


def read_sheet(path: Path) -> list[dict]:
    """Read the Google Sheet exported as CSV. The header row is found by its 'Link' column."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header_index = next(i for i, row in enumerate(rows) if "Link" in row)
    header = rows[header_index]
    items = []
    for row in rows[header_index + 1:]:
        record = dict(zip(header, row))
        url = record.get("Link", "").strip()
        if not url:
            continue
        items.append({
            "url": url,
            "category": record.get("Kategorija", "").strip(),
            "clean_audio": record.get("Čist zvuk", "").strip(),
            "comment": record.get("Komentari", "").strip(),
        })
    return items


def load_index() -> dict[str, dict]:
    if not VIDEOS_CSV.exists():
        return {}
    with VIDEOS_CSV.open(newline="", encoding="utf-8") as f:
        return {row["video_id"]: row for row in csv.DictReader(f)}


def save_index(index: dict[str, dict]) -> None:
    VIDEOS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with VIDEOS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(index.values())


def to_wav(source: Path, target: Path, start: float = 0, minutes: float | None = None) -> None:
    clip = (["-ss", str(start)] if start else []) + (["-t", str(minutes * 60)] if minutes else [])
    subprocess.run(
        [tools.find("ffmpeg"), "-y", "-loglevel", "error", *clip, "-i", str(source),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)],
        check=True,
    )


def download(item: dict, index: dict[str, dict], start: float = 0, minutes: float | None = None) -> None:
    SOURCE.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(SOURCE / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(item["url"], download=False)
        video_id = info["id"]
        wav = RAW / f"{video_id}.wav"
        if wav.exists():
            print(f"skip {video_id} (already converted)")
        else:
            print(f"download {video_id}: {info.get('title')}")
            info = ydl.extract_info(item["url"], download=True)
            source = Path(ydl.prepare_filename(info))
            to_wav(source, wav, start, minutes)
            source.unlink()
    index[video_id] = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": info.get("title", ""),
        "category": item.get("category", ""),
        "duration_s": min(info.get("duration") or 0, minutes * 60) if minutes else info.get("duration", ""),
        "clean_audio": item.get("clean_audio", ""),
        "comment": item.get("comment", ""),
    }
    save_index(index)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="YouTube links")
    parser.add_argument("--category", default="", help="category for links given on the command line")
    parser.add_argument("--csv", type=Path, help="Google Sheet exported as CSV")
    parser.add_argument("--ids", default="", help="only these video ids from the CSV, comma separated")
    parser.add_argument("--limit", type=int, help="only the first N videos")
    parser.add_argument("--max-minutes", type=float, help="keep at most this many minutes of each video")
    parser.add_argument("--start", type=float, default=0, help="skip this many seconds at the start (intros)")
    args = parser.parse_args()

    items = [{"url": url, "category": args.category} for url in args.urls]
    if args.csv:
        items += read_sheet(args.csv)
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [item for item in items if any(i in item["url"] for i in wanted)]
    # The sheet lists some videos twice; keep the first row per link.
    seen, unique = set(), []
    for item in items:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    items = unique[: args.limit] if args.limit else unique
    if not items:
        parser.error("give at least one URL or --csv")

    index = load_index()
    failed = []
    for item in items:
        try:
            download(item, index, args.start, args.max_minutes)
        except Exception as error:  # report and continue with the next video
            print(f"FAILED {item['url']}: {error}", file=sys.stderr)
            failed.append(item["url"])
    print(f"done: {len(items) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
