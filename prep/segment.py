"""Step 3: cut each transcribed video into Whisper training segments.

Usage:
  python prep/segment.py            # all videos that have a transcript
  python prep/segment.py --ids id1  # only these videos (manifest still lists all)

Free to rerun: it only reads data/raw and data/transcripts.

Output:
  data/segments/<category>/<video_id>/<video_id>_0001.wav
  data/manifest.csv   one row per segment; train only on rows with keep=1

Rules:
  - segments are 5-30 s, cut at the best pause (longer pause, sentence end
    or speaker change wins); a pause over 1.5 s always ends a segment
  - audio events ([pozivna špica], [smijeh], music ...) are never inside a
    segment; the audio around them is cut out
  - text stays verbatim (fillers like eh/hm kept); --clean removes fillers;
    number words always become digits ("trideset" -> "30"); everything else stays verbatim
  - low-confidence segments get keep=0 with a reason
  - segments where speakers talk over each other are kept but flagged
"""
import argparse
import csv
import json
import math
import re
import shutil
import unicodedata
import wave
from pathlib import Path

from numbers_hr import words_to_digits
from texts import sync as sync_texts

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
TRANSCRIPTS = ROOT / "data" / "transcripts"
SEGMENTS = ROOT / "data" / "segments"
VIDEOS_CSV = ROOT / "data" / "videos.csv"
MANIFEST = ROOT / "data" / "manifest.csv"

MIN_SECONDS = 5.0
MAX_SECONDS = 29.0          # Whisper sees 30 s; leave room for padding
FORCE_CUT_PAUSE = 1.5       # a pause this long always ends a segment
MAX_PAD = 0.25              # silence kept before/after the first/last word
SHORTEST_KEPT = 1.0         # shorter pieces (between events) are dropped
LOW_WORD_PROB = 0.3         # a word below this probability counts as unsure
MAX_UNSURE_SHARE = 0.10     # more unsure words than this drops the segment

FILLER = re.compile(r"^(e+h*m*|h+m+|m+h*m+|a+h+|u+h+|e+m+)[,.!?]*$", re.IGNORECASE)
FIELDS = ["file_path", "text", "video_id", "category", "start", "end", "duration",
          "speakers", "overlap", "unsure_share", "unsure_words", "keep", "drop_reason"]


def rows_to_keep(existing, wanted, transcript_ids):
    """Return existing manifest rows for videos not being re-cut."""
    if wanted is None:
        return [row for row in existing if row["video_id"] not in transcript_ids]
    return [row for row in existing if row["video_id"] not in wanted]


def slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", ascii_text).strip("_") or "Uncategorized"


def load_categories() -> dict[str, str]:
    if not VIDEOS_CSV.exists():
        return {}
    with VIDEOS_CSV.open(newline="", encoding="utf-8") as f:
        return {row["video_id"]: row["category"] for row in csv.DictReader(f)}


def split_into_runs(items: list[dict]) -> list[list[dict]]:
    """Split the token list at audio events, so no segment contains one."""
    runs, current = [], []
    for item in items:
        if item["type"] == "audio_event":
            if current:
                runs.append(current)
            current = []
        elif item["type"] == "word":
            current.append(item)
    if current:
        runs.append(current)
    return runs


def cut_run(words: list[dict]) -> list[tuple[int, int]]:
    """Return (first, last) word indexes for each segment of one run."""
    segments, first, candidates = [], 0, []
    for j in range(len(words)):
        duration = words[j]["end"] - words[first]["start"]
        is_last = j == len(words) - 1
        gap = math.inf if is_last else words[j + 1]["start"] - words[j]["end"]
        if is_last or (gap >= FORCE_CUT_PAUSE):
            segments.append((first, j))
            first, candidates = j + 1, []
            continue
        if duration >= MIN_SECONDS:
            score = gap
            if words[j]["text"].rstrip().endswith((".", "?", "!")):
                score += 0.5
            if words[j].get("speaker_id") != words[j + 1].get("speaker_id"):
                score += 0.3
            candidates.append((score, j))
        next_duration = words[j + 1]["end"] - words[first]["start"]
        if next_duration > MAX_SECONDS:
            cut = max(candidates)[1] if candidates else j
            segments.append((first, cut))
            first = cut + 1
            candidates = [c for c in candidates if c[1] >= first]
            # Candidates after the cut were scored against the old start; rescore lazily
            candidates = [c for c in candidates if words[c[1]]["end"] - words[first]["start"] >= MIN_SECONDS]
    return segments


def clean_text(words: list[dict], drop_fillers: bool) -> str:
    kept: list[str] = []
    for w in words:
        token = w["text"].strip()
        if drop_fillers and FILLER.match(token):
            # "greške je, eh, razlika" -> "greške je razlika": the commas belonged to the filler.
            if token.endswith(",") and kept and kept[-1].endswith(","):
                kept[-1] = kept[-1][:-1]
            continue
        kept.append(token)
    text = " ".join(kept)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"^[,;:\s]+", "", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Capitalize after removed sentence-initial fillers ("Eh, danas" -> "Danas").
    text = re.sub(r"(^|[.!?]\s+)([a-zčćđšž])", lambda m: m.group(1) + m.group(2).upper(), text)
    return words_to_digits(text)


def has_overlap(words: list[dict]) -> bool:
    for a, b in zip(words, words[1:]):
        if a.get("speaker_id") != b.get("speaker_id") and b["start"] < a["end"] - 0.05:
            return True
    return False


def write_wav(source: wave.Wave_read, start: float, end: float, target: Path) -> None:
    rate = source.getframerate()
    source.setpos(int(start * rate))
    frames = source.readframes(int((end - start) * rate))
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as out:
        out.setnchannels(source.getnchannels())
        out.setsampwidth(source.getsampwidth())
        out.setframerate(rate)
        out.writeframes(frames)


def process_video(video_id: str, category: str, drop_fillers: bool) -> list[dict]:
    data = json.loads((TRANSCRIPTS / f"{video_id}.json").read_text(encoding="utf-8"))
    items = data["words"]
    folder = SEGMENTS / slug(category) / video_id
    if folder.exists():
        shutil.rmtree(folder)

    rows = []
    with wave.open(str(RAW / f"{video_id}.wav"), "rb") as source:
        total = source.getnframes() / source.getframerate()
        for run in split_into_runs(items):
            for first, last in cut_run(run):
                words = run[first:last + 1]
                # Pad into the pause, but never into a neighbouring word.
                before = run[first - 1]["end"] if first > 0 else words[0]["start"] - MAX_PAD
                after = run[last + 1]["start"] if last + 1 < len(run) else words[-1]["end"] + MAX_PAD
                start = max(0.0, words[0]["start"] - min(MAX_PAD, (words[0]["start"] - before) / 2))
                end = min(total, words[-1]["end"] + min(MAX_PAD, (after - words[-1]["end"]) / 2))
                duration = end - start
                text = clean_text(words, drop_fillers)
                unsure_list = [w for w in words if math.exp(w.get("logprob", 0.0)) < LOW_WORD_PROB]
                unsure = len(unsure_list) / len(words)
                reason = ""
                if duration < SHORTEST_KEPT or not text:
                    reason = "too short"
                elif duration > 30.0:
                    reason = "too long"
                elif unsure > MAX_UNSURE_SHARE:
                    reason = "low confidence"
                index = len(rows) + 1
                path = folder / f"{video_id}_{index:04d}.wav"
                write_wav(source, start, end, path)
                rows.append({
                    "file_path": path.relative_to(ROOT).as_posix(),
                    "text": text,
                    "video_id": video_id,
                    "category": category,
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "duration": round(duration, 2),
                    "speakers": len({w.get("speaker_id") for w in words}),
                    "overlap": int(has_overlap(words)),
                    "unsure_share": round(unsure, 3),
                    # Words to check by ear, with the second they occur in the original video.
                    "unsure_words": "; ".join(f"{w['text'].strip()} @{w['start']:.1f}s" for w in unsure_list),
                    "keep": int(not reason),
                    "drop_reason": reason,
                })
    return rows


def run(ids=None, clean=False) -> int:
    categories = load_categories()
    wanted = set(ids) if ids is not None else None
    existing = []
    if MANIFEST.exists():
        with MANIFEST.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    transcript_ids = {transcript.stem for transcript in TRANSCRIPTS.glob("*.json")}
    existing = rows_to_keep(existing, wanted, transcript_ids)

    rows = existing
    for transcript in sorted(TRANSCRIPTS.glob("*.json")):
        video_id = transcript.stem
        if wanted is not None and video_id not in wanted:
            continue
        video_rows = process_video(video_id, categories.get(video_id, ""), clean)
        kept = [r for r in video_rows if r["keep"]]
        hours = sum(r["duration"] for r in kept) / 3600
        print(f"{video_id}: {len(video_rows)} segments, {len(kept)} kept ({hours * 60:.1f} min), "
              f"{sum(r['overlap'] for r in video_rows)} overlap")
        rows += video_rows

    with MANIFEST.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"manifest: {MANIFEST.relative_to(ROOT)} ({len(rows)} rows)")
    # A .txt transcript next to every .wav (approved text where you have one).
    print(f"{sync_texts(wanted)} segment transcripts written")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", default="", help="only re-cut these video ids, comma separated")
    parser.add_argument("--clean", action="store_true", help="remove fillers (eh, hm, mhm) from the text")
    args = parser.parse_args()
    ids = set(args.ids.split(",")) if args.ids else None
    run(ids=ids, clean=args.clean)


if __name__ == "__main__":
    main()
