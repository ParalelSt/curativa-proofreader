"""Add the missing parts of a recording that was downloaded cut (first 60 s skipped, capped at 22 min).

Usage:
  python prep/complete_recording.py --check                 # which recordings miss audio (no download, no paid call)
  python prep/complete_recording.py <video_id> [<video_id>] # download the full video, add the missing parts
  python prep/complete_recording.py <video_id> --no-transcribe   # stop after the part WAVs and index rows

Nothing that exists is touched: data/raw/<id>.wav, its transcript, its clips and its
manifest rows stay as they are, so the proofreading stays valid. The missing audio is
added as its own recordings, like fYBlKc0UozQ-p2:
  <id>-p0   the start of the video, up to the first existing clip
  <id>-p2   after the last existing clip, to the end of the video
The existing WAV is located inside the full audio by matching the sound, not by
assuming 60 s. Parts that are already in data/videos.csv or data/raw are skipped, so
it is safe to rerun (a part that has a WAV but no transcript yet is transcribed then).
Transcribing the parts is the only paid step. Standard library only.
"""
import argparse
import csv
import json
import math
import re
import sys
import tempfile
import wave
from array import array
from operator import mul
from pathlib import Path

try:
    from prep import add_recording, download, segment, tools, transcribe
except ImportError:
    import add_recording
    import download
    import segment
    import tools
    import transcribe

MIN_PART_SECONDS = 5.0      # a shorter part is not worth a recording
COMPLETE_TOLERANCE = 5.0    # video this much longer than our audio still counts as complete
EXCERPT_SECONDS = 25.0
MIN_CUT_SECONDS = 10.0
COARSE = 0.1                # seconds per frame of the first, whole-file search
FINE = 0.01                 # seconds per frame when refining around a candidate
STRIDE = 4                  # every 4th sample is enough to measure loudness
SEARCH_MARGIN = 0.3         # the fine search looks this far around a coarse candidate
CANDIDATES = 5
MIN_MATCH = 0.8             # envelope correlation needed to accept a match
PROBE_SECONDS = 0.5         # raw samples used for the last, sample-exact step
PART_ID = re.compile(r"-p\d+$")


def open_wav(path: Path) -> wave.Wave_read:
    source = wave.open(str(path), "rb")
    if source.getnchannels() != 1 or source.getsampwidth() != 2:
        source.close()
        raise ValueError(f"{Path(path).name}: expected mono 16-bit audio")
    return source


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def read_samples(path: Path, start: float, seconds: float) -> array:
    with open_wav(path) as source:
        source.setpos(min(source.getnframes(), max(0, int(start * source.getframerate()))))
        samples = array("h")
        samples.frombytes(source.readframes(int(seconds * source.getframerate())))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def loudness(samples: array, frame: int) -> list[float]:
    """One value per frame: log of the RMS of every STRIDE-th sample."""
    out = []
    for first in range(0, len(samples) - frame + 1, frame):
        chunk = samples[first:first + frame:STRIDE]
        out.append(math.log1p(math.sqrt(sum(map(mul, chunk, chunk)) / len(chunk))))
    return out


def loudness_of_file(path: Path, seconds_per_frame: float) -> list[float]:
    """Loudness of a whole file, read in pieces so a long recording never sits in memory."""
    out = []
    with open_wav(path) as source:
        frame = max(STRIDE, int(source.getframerate() * seconds_per_frame))
        while True:
            samples = array("h")
            samples.frombytes(source.readframes(frame * 600))
            if sys.byteorder == "big":
                samples.byteswap()
            if len(samples) < frame:
                return out
            out += loudness(samples, frame)


def slide(signal, template) -> list[float]:
    """Normalized cross-correlation of the template at every position of the signal (-1..1)."""
    n = len(template)
    mean = sum(template) / n
    centered = [x - mean for x in template]
    norm = math.sqrt(sum(x * x for x in centered))
    sums, squares = [0.0], [0.0]
    for x in signal:
        sums.append(sums[-1] + x)
        squares.append(squares[-1] + x * x)
    scores = []
    for i in range(len(signal) - n + 1):
        window_sum = sums[i + n] - sums[i]
        spread = math.sqrt(max(squares[i + n] - squares[i] - window_sum * window_sum / n, 0.0)) * norm
        scores.append(sum(map(mul, signal[i:i + n], centered)) / spread if spread > 1e-9 else 0.0)
    return scores


def refine(cut: Path, full: Path, rate: int, start: float, length: float, guess: float) -> tuple[float, float]:
    """Around a coarse guess (excerpt start inside the full audio, seconds), find the
    exact place. Returns (correlation, seconds at which the cut file starts in the full one)."""
    frame = int(rate * FINE)
    excerpt = loudness(read_samples(cut, start, length), frame)
    window_start = max(0.0, guess - SEARCH_MARGIN)
    window = loudness(read_samples(full, window_start, length + 2 * SEARCH_MARGIN), frame)
    if len(window) < len(excerpt):
        return 0.0, 0.0
    scores = slide(window, excerpt)
    best = max(range(len(scores)), key=scores.__getitem__)
    offset = window_start + best * FINE - start
    # Last step, sample-exact: line up half a second of raw samples within one frame either way.
    probe = int(PROBE_SECONDS * rate)
    probe_at = int((start + length / 2) * rate)
    probe_samples = read_samples(cut, probe_at / rate, PROBE_SECONDS)
    around = int((offset + probe_at / rate) * rate) - frame
    raw = slide(read_samples(full, max(0, around) / rate, PROBE_SECONDS + 2 * frame / rate), probe_samples)
    if len(raw) and len(probe_samples) == probe:
        shift = max(range(len(raw)), key=raw.__getitem__)
        if raw[shift] >= 0.9:
            offset = (max(0, around) + shift - probe_at) / rate
    return scores[best], offset


def locate(cut: Path, full: Path) -> float:
    """Seconds at which the `cut` file starts inside the `full` file. Raises if there is no confident match."""
    with open_wav(cut) as a, open_wav(full) as b:
        rate, cut_seconds, full_seconds = a.getframerate(), a.getnframes() / a.getframerate(), b.getnframes() / b.getframerate()
        if rate != b.getframerate():
            raise ValueError("The two recordings have different sample rates.")
    if cut_seconds < MIN_CUT_SECONDS or full_seconds < cut_seconds - 1:
        raise ValueError("The existing audio does not fit into the full audio.")
    length = min(EXCERPT_SECONDS, cut_seconds / 2)
    full_loudness = loudness_of_file(full, COARSE)
    for position in (0.5, 0.25, 0.75):  # another excerpt if the first one sits in silence or music
        start = round((cut_seconds - length) * position / COARSE) * COARSE
        excerpt = loudness(read_samples(cut, start, length), int(rate * COARSE))
        scores = slide(full_loudness, excerpt)
        candidates = []
        for peak in sorted(range(len(scores)), key=scores.__getitem__, reverse=True):
            if len(candidates) == CANDIDATES:
                break
            if all(abs(peak - other) >= 20 for other in candidates):  # one candidate per place
                candidates.append(peak)
        confidence, offset = max(refine(cut, full, rate, start, length, peak * COARSE) for peak in candidates)
        if confidence >= MIN_MATCH and -0.05 <= offset <= full_seconds - cut_seconds + 1:
            return max(0.0, offset)
    raise ValueError("The existing audio was not found in the full audio with enough confidence; nothing was added.")


def clip_bounds(video_id: str) -> tuple[float, float]:
    """Start of the first and end of the last existing clip of the recording, in its own seconds."""
    with segment.MANIFEST.open(newline="", encoding="utf-8") as f:
        rows = [row for row in csv.DictReader(f) if row["video_id"] == video_id]
    if not rows:
        raise ValueError(f"{video_id} has no clips in the manifest.")
    return min(float(r["start"]) for r in rows), max(float(r["end"]) for r in rows)


def already_added(part_id: str) -> bool:
    return part_id in download.load_index() or (download.RAW / f"{part_id}.wav").exists()


def missing_parts(video_id: str, full_wav_path: Path) -> list[dict]:
    """Head and tail of the full audio that the existing recording does not cover (not yet added ones only)."""
    wanted = [p for p in ("p0", "p2") if not already_added(f"{video_id}-{p}")]
    if not wanted:
        return []
    full_wav_path = Path(full_wav_path)
    offset = locate(download.RAW / f"{video_id}.wav", full_wav_path)
    first_start, last_end = clip_bounds(video_id)
    full_seconds = wav_seconds(full_wav_path)
    spans = {"p0": (0.0, offset + first_start), "p2": (offset + last_end, full_seconds)}
    # Audio the existing WAV never contained. Silence or music inside it (before the first clip,
    # after the last one) was already transcribed and is not missing.
    uncovered = {"p0": offset, "p2": full_seconds - offset - wav_seconds(download.RAW / f"{video_id}.wav")}
    parts = []
    for name in wanted:
        start, end = spans[name]
        if uncovered[name] >= MIN_PART_SECONDS and end - start >= MIN_PART_SECONDS:
            parts.append({"part_id": f"{video_id}-{name}", "start": round(start, 3), "end": round(end, 3),
                          "seconds": round(end - start, 1)})
    return parts


def cut_wav(source_path: Path, start: float, end: float, target: Path) -> None:
    """Copy seconds start..end of one WAV into a new WAV file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open_wav(source_path) as source, wave.open(str(target), "wb") as out:
        rate = source.getframerate()
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        first = int(start * rate)
        source.setpos(first)
        left = min(int(end * rate), source.getnframes()) - first
        while left > 0:
            data = source.readframes(min(left, 1_000_000))
            out.writeframes(data)
            left -= len(data) // 2


def download_full(url: str, folder: Path) -> tuple[Path, dict]:
    """Download the whole video into `folder` as 16 kHz mono WAV. Never writes into data/raw."""
    options = {"format": "bestaudio/best", "outtmpl": str(folder / "%(id)s.%(ext)s"),
               "quiet": True, "no_warnings": True, "noprogress": True}
    with download.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        source = Path(ydl.prepare_filename(info))
    wav = folder / "full.wav"
    download.to_wav(source, wav)
    source.unlink()
    return wav, info


def add_index_rows(parent: dict, parts: list[dict]) -> None:
    """Append one videos.csv row per part, copied from the parent (appended: other rows stay byte-identical)."""
    path = download.VIDEOS_CSV
    needs_newline = path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
    with path.open("a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\r\n")
        writer = csv.DictWriter(f, fieldnames=download.FIELDS)
        for part in parts:
            label = "rest" if part["part_id"].endswith("-p2") else "start"
            writer.writerow({**parent, "video_id": part["part_id"], "title": f"{parent['title']} ({label})",
                             "duration_s": round(part["seconds"])})


def count_clips(video_id: str) -> int:
    with segment.MANIFEST.open(newline="", encoding="utf-8") as f:
        return sum(1 for row in csv.DictReader(f) if row["video_id"] == video_id)


def transcribe_and_cut(part: dict, step) -> None:
    target = transcribe.TRANSCRIPTS / f"{part['part_id']}.json"
    if not target.exists():
        step("transcribing")
        key = tools.read_key()
        if not key:
            raise RuntimeError("The ElevenLabs key is missing. Paste it in the tool first.")
        try:
            result = transcribe.transcribe(download.RAW / f"{part['part_id']}.wav", key)
        except Exception as error:
            if "401" in str(error) and "http" in str(error).casefold():
                raise RuntimeError("The ElevenLabs key was not accepted.") from None
            raise
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    step("cutting")
    segment.run({part["part_id"]})
    part["clips"] = count_clips(part["part_id"])


def complete(video_id: str, on_step=None, transcribe_parts: bool = True) -> dict:
    """Add the missing head/tail of one recording as its own recordings. Existing files are never changed."""
    parent = download.load_index().get(video_id)
    if parent is None or not (download.RAW / f"{video_id}.wav").exists():
        raise ValueError(f"{video_id} is not a downloaded recording.")
    step = on_step or (lambda name: None)
    ids = [f"{video_id}-p0", f"{video_id}-p2"]
    parts = []
    if not all(already_added(part_id) for part_id in ids):  # otherwise nothing to download
        try:
            with tempfile.TemporaryDirectory(prefix="complete_recording_") as folder:
                step("downloading")
                full_wav, _ = download_full(parent["url"], Path(folder))
                parts = missing_parts(video_id, full_wav)
                for part in parts:
                    cut_wav(full_wav, part["start"], part["end"], download.RAW / f"{part['part_id']}.wav")
                if parts:
                    add_index_rows(parent, parts)
        except Exception as error:
            if add_recording._is_connection_error(error):
                raise RuntimeError("No internet connection, or the video could not be reached.") from None
            raise
    if transcribe_parts:
        # Also finish a part from an earlier run that has its WAV but was never transcribed.
        done = {part["part_id"] for part in parts}
        parts += [{"part_id": part_id, "seconds": round(wav_seconds(download.RAW / f"{part_id}.wav"), 1)}
                  for part_id in ids if part_id not in done and (download.RAW / f"{part_id}.wav").exists()
                  and not (transcribe.TRANSCRIPTS / f"{part_id}.json").exists()]
        for part in parts:
            transcribe_and_cut(part, step)
    step("ready")
    return {"video_id": video_id, "parts": parts}


def check() -> list[dict]:
    """For each recording, how many seconds of the video are not in any of our WAVs. Metadata only:
    no audio is downloaded and nothing paid is called. A failure for one recording does not stop the rest."""
    results = []
    with download.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True}) as ydl:
        for video_id, row in download.load_index().items():
            wav = download.RAW / f"{video_id}.wav"
            if PART_ID.search(video_id) or not wav.exists():
                continue
            result = {"video_id": video_id, "title": row.get("title") or video_id}
            try:
                real = float(ydl.extract_info(row["url"], download=False)["duration"])
                have = wav_seconds(wav) + sum(wav_seconds(p) for p in download.RAW.glob(f"{video_id}-p*.wav"))
            except Exception as error:  # report and continue with the next video
                results.append({**result, "error": (str(error).splitlines() or ["failed"])[0]})
                continue
            missing = real - have
            results.append({**result, "video_s": real, "have_s": round(have, 1),
                            "missing_s": round(missing, 1) if missing >= COMPLETE_TOLERANCE else 0.0})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="video ids to complete")
    parser.add_argument("--check", action="store_true", help="list recordings with missing audio (no download)")
    parser.add_argument("--no-transcribe", action="store_true", help="stop after the part WAVs and index rows")
    args = parser.parse_args()
    if args.check:
        for result in check():
            if "error" in result:
                print(f"{result['video_id']}: could not check ({result['error']})")
            elif result["missing_s"]:
                print(f"{result['video_id']}: {result['missing_s']:.0f} s missing")
            else:
                print(f"{result['video_id']}: complete")
        return 0
    if not args.ids:
        parser.error("give video ids or --check")
    failed = 0
    for video_id in args.ids:
        try:
            added = complete(video_id, on_step=lambda name: print(f"  {name} ...", flush=True),
                             transcribe_parts=not args.no_transcribe)
        except Exception as error:  # report and continue with the next video
            print(f"FAILED {video_id}: {error}", file=sys.stderr)
            failed += 1
            continue
        for part in added["parts"]:
            print(f"{part['part_id']}: {part['seconds']:.0f} s added")
        if not added["parts"]:
            print(f"{video_id}: nothing to add")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
