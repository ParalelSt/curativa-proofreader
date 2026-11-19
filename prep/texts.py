"""Keep a .txt transcript next to every segment .wav, in the same category folders.

  data/segments/<Category>/<video_id>/<video_id>_0001.wav
  data/segments/<Category>/<video_id>/<video_id>_0001.txt   <- current text

The text is your approved text from the review tool, otherwise the ElevenLabs
text. Excluded segments have no .txt. The tool updates a file on every save;
run this to rebuild them all:

  python prep/texts.py
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.csv"
CORRECTIONS = ROOT / "data" / "corrections.json"


def write_text(file_path: str, text: str | None) -> None:
    """Write (or with None, remove) the .txt next to one segment .wav."""
    target = (ROOT / file_path).with_suffix(".txt")
    if text is None:
        target.unlink(missing_ok=True)
    elif target.parent.exists():
        target.write_text(text.strip() + "\n", encoding="utf-8")


def current_text(row: dict, corrections: dict) -> str | None:
    c = corrections.get(row["file_path"])
    if c and c["auto_text"] == row["text"]:
        return None if c["status"] == "excluded" else c["text"]
    return row["text"]


def sync(video_ids: set[str] | None = None) -> int:
    corrections = json.loads(CORRECTIONS.read_text(encoding="utf-8")) if CORRECTIONS.exists() else {}
    count = 0
    with MANIFEST.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if video_ids and row["video_id"] not in video_ids:
                continue
            write_text(row["file_path"], current_text(row, corrections))
            count += 1
    return count


if __name__ == "__main__":
    print(f"{sync()} segment transcripts written")
