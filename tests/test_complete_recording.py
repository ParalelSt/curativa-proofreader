import csv
import json
import random
import sys
import tempfile
import threading
import unittest
import urllib.request
from array import array
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "prep"), str(ROOT / "review")]
from prep import complete_recording as cr  # noqa: E402
import texts  # noqa: E402
import server  # noqa: E402

download, segment, transcribe = cr.download, cr.segment, cr.transcribe
RATE = 8000
VID = "abcdefghijk"
OFFSET = 37.3


def make_audio(seconds, seed):
    """Noise whose loudness changes every 0.3 s, so a position in it is unambiguous."""
    rng = random.Random(seed)
    out = array("h")
    for _ in range(int(seconds / 0.3) + 1):
        gain = rng.uniform(0.05, 1.0) * 6000
        out.extend(max(-32768, min(32767, int(rng.gauss(0, 1) * gain))) for _ in range(int(0.3 * RATE)))
    return out[: int(seconds * RATE)]


FULL = make_audio(150, 1)
OTHER = make_audio(150, 99)


def write_wav(path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    with cr.wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / "data"
        for target, name, value in [
            (download, "RAW", data / "raw"), (download, "VIDEOS_CSV", data / "videos.csv"),
            (transcribe, "TRANSCRIPTS", data / "transcripts"), (segment, "ROOT", self.root),
            (segment, "RAW", data / "raw"), (segment, "TRANSCRIPTS", data / "transcripts"),
            (segment, "SEGMENTS", data / "segments"), (segment, "VIDEOS_CSV", data / "videos.csv"),
            (segment, "MANIFEST", data / "manifest.csv"), (texts, "ROOT", self.root),
            (texts, "MANIFEST", data / "manifest.csv"), (texts, "CORRECTIONS", data / "corrections.json"),
            (server, "VIDEOS", data / "videos.csv"), (server, "HIDDEN", data / "hidden_videos.json"),
        ]:
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        (data / "raw").mkdir(parents=True)
        (data / "transcripts").mkdir()

    def make_recording(self, cut, clips=((2.0, 40.0), (50.0, 88.0)), videos=()):
        """An existing, proofread recording: its wav, index row, transcript and manifest rows."""
        write_wav(download.RAW / f"{VID}.wav", cut)
        (transcribe.TRANSCRIPTS / f"{VID}.json").write_text('{"words": []}', encoding="utf-8")
        row = {"video_id": VID, "url": "https://www.youtube.com/watch?v=" + VID, "title": "T", "category": "Cat",
               "duration_s": "90", "clean_audio": "Da", "comment": "c"}
        with download.VIDEOS_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=download.FIELDS)
            writer.writeheader()
            writer.writerows([row] + [{**row, "video_id": v} for v in videos])
        with segment.MANIFEST.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=segment.FIELDS)
            writer.writeheader()
            for video, items in ((VID, clips), ("otherother1", ((1.0, 9.0),))):
                for i, (start, end) in enumerate(items, 1):
                    writer.writerow({"file_path": f"data/segments/Cat/{video}/{video}_{i:04d}.wav", "text": f"clip {i}",
                                     "video_id": video, "category": "Cat", "start": start, "end": end,
                                     "duration": end - start, "speakers": 1, "overlap": 0, "unsure_share": 0,
                                     "unsure_words": "", "keep": 1, "drop_reason": ""})

    def standard(self, **kwargs):
        self.make_recording(FULL[int(OFFSET * RATE):int((OFFSET + 90) * RATE)], **kwargs)
        path = self.root / "full.wav"
        write_wav(path, FULL)
        return path


class MissingPartsTest(Base):
    def test_locate_finds_non_60_offset(self):
        path = self.standard()
        self.assertAlmostEqual(cr.locate(download.RAW / f"{VID}.wav", path), OFFSET, delta=0.001)

    def test_head_and_tail_from_clip_boundaries(self):
        by_id = {p["part_id"]: p for p in cr.missing_parts(VID, self.standard())}
        self.assertEqual(set(by_id), {f"{VID}-p0", f"{VID}-p2"})
        self.assertAlmostEqual(by_id[f"{VID}-p0"]["start"], 0.0)
        self.assertAlmostEqual(by_id[f"{VID}-p0"]["end"], 39.3, places=2)   # offset 37.3 + first clip start 2.0
        self.assertAlmostEqual(by_id[f"{VID}-p2"]["start"], 125.3, places=2)  # offset 37.3 + last clip end 88.0
        self.assertAlmostEqual(by_id[f"{VID}-p2"]["end"], 150.0, places=2)

    def test_short_parts_skipped(self):
        # Cut starts 1.0 s into the video and first clip starts at 2.0: head is 3 s. Last clip ends 2 s before the end.
        self.make_recording(FULL[RATE:RATE * 91], clips=((2.0, 40.0), (50.0, 88.0)))
        path = self.root / "full.wav"
        write_wav(path, FULL[:RATE * 93])
        self.assertEqual(cr.missing_parts(VID, path), [])

    def test_already_full_recording_yields_nothing(self):
        self.make_recording(FULL, clips=((0.5, 70.0), (80.0, 149.0)))
        path = self.root / "full.wav"
        write_wav(path, FULL)
        self.assertEqual(cr.missing_parts(VID, path), [])

    def test_existing_part_is_skipped(self):
        path = self.standard(videos=[f"{VID}-p2"])
        self.assertEqual([p["part_id"] for p in cr.missing_parts(VID, path)], [f"{VID}-p0"])
        write_wav(download.RAW / f"{VID}-p0.wav", FULL[:RATE * 10])
        self.assertEqual(cr.missing_parts(VID, path), [])

    def test_no_match_raises_and_adds_nothing(self):
        self.standard()
        other = self.root / "other.wav"
        write_wav(other, OTHER)
        before = sorted(p.name for p in download.RAW.iterdir()), download.VIDEOS_CSV.read_bytes()
        with self.assertRaises(ValueError):
            cr.missing_parts(VID, other)
        with mock.patch.object(cr, "download_full", lambda url, folder: (other, {})):
            with self.assertRaises(ValueError):
                cr.complete(VID, transcribe_parts=False)
        self.assertEqual((sorted(p.name for p in download.RAW.iterdir()), download.VIDEOS_CSV.read_bytes()), before)

    def test_cut_wav_is_sample_exact(self):
        path = self.root / "full.wav"
        write_wav(path, FULL)
        cr.cut_wav(path, 1.0, 3.5, self.root / "part.wav")
        self.assertEqual(cr.read_samples(self.root / "part.wav", 0, 10), FULL[RATE:int(3.5 * RATE)])


class CompleteTest(Base):
    def run_complete(self, **kwargs):
        full_path = self.standard()
        calls = []

        def fake_download(url, folder):
            calls.append(url)
            return full_path, {}
        with mock.patch.object(cr, "download_full", fake_download):
            return cr.complete(VID, **kwargs), calls

    def test_without_transcribing_is_idempotent_and_touches_nothing(self):
        full_path = self.standard()
        old = {p: p.read_bytes() for p in (download.RAW / f"{VID}.wav", segment.MANIFEST,
                                           transcribe.TRANSCRIPTS / f"{VID}.json")}
        old_index = download.VIDEOS_CSV.read_bytes()
        calls = []
        with mock.patch.object(cr, "download_full", lambda url, folder: (calls.append(url), (full_path, {}))[1]):
            result = cr.complete(VID, transcribe_parts=False)
            self.assertEqual([p["part_id"] for p in result["parts"]], [f"{VID}-p0", f"{VID}-p2"])
            self.assertAlmostEqual(cr.wav_seconds(download.RAW / f"{VID}-p0.wav"), 39.3, places=2)
            index = download.VIDEOS_CSV.read_bytes()
            self.assertTrue(index.startswith(old_index))
            rows = download.load_index()
            self.assertEqual(rows[f"{VID}-p2"]["url"], rows[VID]["url"])
            self.assertEqual(rows[f"{VID}-p2"]["title"], "T (rest)")
            self.assertEqual(rows[f"{VID}-p0"]["title"], "T (start)")
            self.assertEqual(rows[f"{VID}-p2"]["duration_s"], "25")
            self.assertEqual(rows[f"{VID}-p2"]["clean_audio"], "Da")
            again = cr.complete(VID, transcribe_parts=False)
            self.assertEqual(again["parts"], [])
            self.assertEqual(len(calls), 1)  # the second run did not even download
            self.assertEqual(download.VIDEOS_CSV.read_bytes(), index)
        self.assertEqual({p: p.read_bytes() for p in old}, old)
        self.assertEqual(list(transcribe.TRANSCRIPTS.glob("*-p*.json")), [])

    def fake_transcribe(self):
        def fake(wav, key):
            self.paid.append(wav.stem)
            return {"words": [{"type": "word", "text": f"w{i}", "start": 1.0 + i * 1.2, "end": 2.0 + i * 1.2,
                               "logprob": 0.0, "speaker_id": "s0"} for i in range(8)]}
        self.paid = []
        return mock.patch.object(transcribe, "transcribe", fake), mock.patch.object(cr.tools, "read_key", lambda: "not-a-key")

    def test_transcribes_and_cuts_only_the_parts(self):
        full_path = self.standard()
        old_manifest = segment.MANIFEST.read_bytes()
        old_transcript = (transcribe.TRANSCRIPTS / f"{VID}.json").read_bytes()
        a, b = self.fake_transcribe()
        with a, b, mock.patch.object(cr, "download_full", lambda url, folder: (full_path, {})):
            steps = []
            result = cr.complete(VID, on_step=steps.append)
        self.assertEqual(sorted(self.paid), [f"{VID}-p0", f"{VID}-p2"])
        self.assertEqual(steps[0], "downloading")
        self.assertEqual(steps[-1], "ready")
        self.assertTrue(all(p["clips"] > 0 for p in result["parts"]))
        # segment.run rewrote the manifest, but every existing row is byte-identical and still first.
        self.assertTrue(segment.MANIFEST.read_bytes().startswith(old_manifest))
        with segment.MANIFEST.open(newline="", encoding="utf-8") as f:
            ids = [r["video_id"] for r in csv.DictReader(f)]
        self.assertEqual(ids.count(VID), 2)
        self.assertEqual(ids.count("otherother1"), 1)
        self.assertEqual((transcribe.TRANSCRIPTS / f"{VID}.json").read_bytes(), old_transcript)
        self.assertFalse((segment.SEGMENTS / "Cat" / VID).exists())

    def test_rerun_finishes_an_untranscribed_part_without_downloading(self):
        full_path = self.standard()
        with mock.patch.object(cr, "download_full", lambda url, folder: (full_path, {})):
            cr.complete(VID, transcribe_parts=False)
        a, b = self.fake_transcribe()
        with a, b, mock.patch.object(cr, "download_full", side_effect=AssertionError("must not download")):
            result = cr.complete(VID)
            self.assertEqual(len(result["parts"]), 2)
            self.assertEqual(cr.complete(VID)["parts"], [])
        self.assertEqual(len(self.paid), 2)  # each part was paid for once


class CheckTest(Base):
    def test_check_lists_missing_seconds_without_downloading(self):
        self.standard(videos=["zzzzzzzzzz2", f"{VID}-p2"])
        write_wav(download.RAW / f"{VID}-p2.wav", FULL[:RATE * 20])
        write_wav(download.RAW / "zzzzzzzzzz2.wav", FULL[:RATE * 60])
        durations = {"https://www.youtube.com/watch?v=" + VID: 200.0}

        class FakeDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                assert download is False
                if "zzzzzzzzzz2" in url or url not in durations:
                    raise RuntimeError("no network")
                return {"duration": durations[url]}

        # zzzzzzzzzz2 reuses the VID url in the index, so point it elsewhere.
        rows = download.load_index()
        rows["zzzzzzzzzz2"]["url"] = "https://www.youtube.com/watch?v=zzzzzzzzzz2"
        with download.VIDEOS_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=download.FIELDS)
            writer.writeheader()
            writer.writerows(rows.values())
        with mock.patch.object(download, "YoutubeDL", FakeDL):
            results = {r["video_id"]: r for r in cr.check()}
        self.assertEqual(set(results), {VID, "zzzzzzzzzz2"})  # the -p2 row is not a recording of its own
        self.assertEqual(results[VID]["missing_s"], 200.0 - 90.0 - 20.0)
        self.assertIn("error", results["zzzzzzzzzz2"])
        durations[download.load_index()[VID]["url"]] = 94.0   # within 5 s: complete
        with mock.patch.object(download, "YoutubeDL", FakeDL):
            self.assertEqual({r["video_id"]: r for r in cr.check()}[VID]["missing_s"], 0.0)


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(server, "complete_recording", cr),
                      mock.patch.object(server.tools, "has_key", lambda: True),
                      mock.patch.object(server, "JOBS", [])):
            patch.start()
            self.addCleanup(patch.stop)
        self.standard(videos=["zzzzzzzzzz2"])

    def test_queue_complete_rules(self):
        with mock.patch.object(server.tools, "has_key", lambda: False):
            self.assertEqual(server.queue_complete(VID)[1], 400)
        self.assertEqual(server.queue_complete("nope")[1], 400)
        self.assertEqual(server.queue_complete(f"{VID}-p2")[1], 400)
        payload, code = server.queue_complete(VID)
        self.assertEqual((code, payload["queued"]), (200, 1))
        self.assertEqual(payload["jobs"][0]["kind"], "complete")
        self.assertEqual(server.queue_complete(VID)[0]["queued"], 0)  # already queued

    def test_run_job_uses_complete_for_complete_jobs(self):
        job = {"kind": "complete", "video_id": VID, "url": "u"}
        fake = {"video_id": VID, "parts": [{"clips": 3}, {"clips": 4}]}
        with mock.patch.object(cr, "complete", return_value=fake) as complete:
            self.assertEqual(server.run_job(job, print), (VID, 7))
        complete.assert_called_once_with(VID, on_step=print)
        with mock.patch.object(server.add_recording, "add", return_value={"video_id": "x", "clips": 2}):
            self.assertEqual(server.run_job({"url": "u"}, print), ("x", 2))

    def test_missing_payload_includes_hidden_and_survives_failure(self):
        server.HIDDEN.write_text(json.dumps([VID]), encoding="utf-8")
        rows = [{"video_id": VID, "title": "T", "missing_s": 60.0}]
        with mock.patch.object(cr, "check", return_value=rows):
            self.assertEqual(server.missing_payload(), {"rows": rows})
        with mock.patch.object(cr, "check", side_effect=RuntimeError("boom")):
            self.assertEqual(server.missing_payload()["rows"], [])

    def test_routes(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base = f"http://127.0.0.1:{http.server_address[1]}"
        with mock.patch.object(cr, "check", return_value=[{"video_id": VID, "title": "T", "missing_s": 60.0}]):
            with urllib.request.urlopen(base + "/api/missing") as response:
                self.assertEqual(json.load(response)["rows"][0]["video_id"], VID)
        request = urllib.request.Request(base + "/api/complete", json.dumps({"video": VID}).encode(),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)["queued"], 1)
        with urllib.request.urlopen(base + "/api/jobs") as response:
            self.assertEqual(json.load(response)["jobs"][0]["video_id"], VID)


if __name__ == "__main__":
    unittest.main()
