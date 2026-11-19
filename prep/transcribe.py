"""Step 2: transcribe each downloaded WAV with ElevenLabs Scribe.

Usage:
  python prep/transcribe.py                 # every WAV in data/raw without a transcript
  python prep/transcribe.py --ids id1,id2   # only these videos

Output:
  data/transcripts/<video_id>.json   raw Scribe response (words with timestamps,
                                     speaker ids, audio events like music/laughter)
Videos that already have a transcript are skipped, so nothing is paid for twice.
The API key is read from Curativa-launcher/.env.local and never printed.
"""
import argparse
import json
import sys
from pathlib import Path

import requests

try:
    from prep import tools
except ImportError:
    import tools

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
TRANSCRIPTS = ROOT / "data" / "transcripts"
ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"  # same model Curativa uses


def read_key() -> str:
    key = tools.read_key()
    if key is None:
        sys.exit("No ElevenLabs key found. Paste it in the tool once, or set ELEVENLABS_API_KEY.")
    return key


def transcribe(wav: Path, key: str) -> dict:
    with wav.open("rb") as audio:
        response = requests.post(
            ENDPOINT,
            headers={"xi-api-key": key},
            data={
                "model_id": MODEL,
                "language_code": "hr",
                "diarize": "true",
                "timestamps_granularity": "word",
                "tag_audio_events": "true",  # marks music/laughter so step 3 can filter them
            },
            files={"file": (wav.name, audio, "audio/wav")},
            timeout=1800,
        )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", default="", help="only these video ids, comma separated")
    args = parser.parse_args()

    wavs = sorted(RAW.glob("*.wav"))
    if args.ids:
        wanted = set(args.ids.split(","))
        wavs = [w for w in wavs if w.stem in wanted]
    if not wavs:
        sys.exit("no WAV files to transcribe; run prep/download.py first")

    key = read_key()
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    failed = 0
    for wav in wavs:
        target = TRANSCRIPTS / f"{wav.stem}.json"
        if target.exists():
            print(f"skip {wav.stem} (already transcribed)")
            continue
        print(f"transcribe {wav.stem} ...", flush=True)
        try:
            result = transcribe(wav, key)
        except Exception as error:  # report and continue with the next video
            print(f"FAILED {wav.stem}: {error}", file=sys.stderr)
            failed += 1
            continue
        target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        words = [w for w in result.get("words", []) if w.get("type") == "word"]
        speakers = {w.get("speaker_id") for w in words}
        print(f"  {len(words)} words, {len(speakers)} speakers, language {result.get('language_code')}")
    print(f"done: {len(wavs) - failed} ok, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
