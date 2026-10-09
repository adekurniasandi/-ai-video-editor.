"""Tes backend. yt-dlp (YouTube) dan Whisper (unduh model) dimock; ffmpeg/ffprobe/FastAPI berjalan NYATA."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402

TRANSCRIPT = (
    [{"start": s, "end": s + 2, "text": "halo ya"} for s in (5, 12, 20)]
    + [{"start": 30 + 3 * i, "end": 33 + 3 * i, "text": "ini bagian padat dengan banyak sekali kata menarik, benarkah?"} for i in range(10)]
)


@pytest.fixture(scope="session")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "70", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(out)], check=True)
    return out


@pytest.fixture()
def client(tmp_path, sample, monkeypatch):
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(main, "RATE_LIMIT_PER_HOUR", 0)
    monkeypatch.setattr(main, "MAX_QUEUE", 5)
    main.JOBS.clear()
    main.HITS.clear()
    monkeypatch.setattr(main, "probe_url", lambda url, jd: {"duration": 70.0, "live": False, "title": "Video Tes"})
    monkeypatch.setattr(main, "download_source", lambda url, jd: shutil.copy(sample, Path(jd) / "source.mp4") and str(Path(jd) / "source.mp4"))
    monkeypatch.setattr(main, "transcribe", lambda path, cb=None: (TRANSCRIPT, "id"))
    with TestClient(main.app) as c:
        yield c


URL = "https://youtu.be/dQw4w9WgXcQ"


def wait(client, jid, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.3)
    raise AssertionError("job tidak selesai tepat waktu")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return d["streams"][0]["width"], d["streams"][0]["height"], float(d["format"]["duration"])


def gray_frame(path, t):
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout


def band_diff(a, b, y0, y1, w=720):
    xs = range(y0 * w, y1 * w)
    return sum(abs(a[i] - b[i]) for i in xs) / len(xs)


# ---------- unit ----------
def test_canonical_url():
    assert main.canonical_url("https://youtu.be/dQw4w9WgXcQ?t=5").endswith("v=dQw4w9WgXcQ")
    assert main.canonical_url("https://www.youtube.com/shorts/dQw4w9WgXcQ").endswith("v=dQw4w9WgXcQ")
    assert main.canonical_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=x").endswith("v=dQw4w9WgXcQ")
    for bad in ("https://evil.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/playlist?list=PL1", "ftp://youtu.be/dQw4w9WgXcQ", "x"):
        with pytest.raises(Exception):
            main.canonical_url(bad)


def test_pick_segment_prefers_dense_region():
    start, length, method, _ = main.pick_segment(TRANSCRIPT, 70.0, 30)
    assert (start, length, method) == (30, 30, "kepadatan_ucapan")
    assert main.pick_segment([], 70.0, 30)[2] == "awal_video"


def test_cues_and_srt(tmp_path):
    cues = main.make_cues(TRANSCRIPT, 30, 30)
    assert cues[0][0] == 0 and all(0 <= a < b <= 30.001 for a, b, _ in cues)
    assert all(len(t.split()) <= 6 for _, _, t in cues)
    main.write_srt(tmp_path / "c.srt", cues)
    text = (tmp_path / "c.srt").read_text()
    assert text.startswith("1\n00:00:00,000 --> ") and main.srt_time(3661.5) == "01:01:01,500"


# ---------- API ----------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and all(r.json()["checks"].values())


def test_cors_preflight(client):
    r = client.options("/api/jobs", headers={"Origin": "https://app.vercel.app", "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] in ("*", "https://app.vercel.app")


def test_rejects_bad_input(client):
    assert client.post("/api/jobs", json={"url": "https://evil.com/x", "duration": 30}).status_code == 400
    assert client.post("/api/jobs", json={"url": URL, "duration": 20}).status_code == 422
    assert client.get("/api/jobs/tidakada").status_code == 404


def test_full_pipeline_burned_subtitles_and_srt(client, tmp_path):
    jid = client.post("/api/jobs", json={"url": URL, "duration": 30, "subtitles": True}).json()["id"]
    j = wait(client, jid)
    assert j["status"] == "done" and j["progress"] == 100, j
    res = j["result"]
    assert res["start"] == 30 and res["subtitles"]["burned"] and res["subtitles"]["cues"] > 0
    mp4 = client.get(f"/api/jobs/{jid}/download")
    assert mp4.status_code == 200 and mp4.headers["content-type"] == "video/mp4"
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(mp4.content)
    w, h, d = probe(clip)
    assert (w, h) == (720, 1280) and abs(d - 30) < 1.5
    srt = client.get(f"/api/jobs/{jid}/subtitles.srt")
    assert srt.status_code == 200 and "-->" in srt.text and srt.text.startswith("1\n00:00:00,000")

    # bandingkan dengan render tanpa subtitle: hanya area bawah yang boleh berbeda
    jid2 = client.post("/api/jobs", json={"url": URL, "duration": 30, "subtitles": False}).json()["id"]
    j2 = wait(client, jid2)
    assert j2["status"] == "done" and not j2["result"]["subtitles"]["burned"]
    assert client.get(f"/api/jobs/{jid2}/subtitles.srt").status_code == 404
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(client.get(f"/api/jobs/{jid2}/download").content)
    a, b = gray_frame(clip, 1.5), gray_frame(plain, 1.5)
    top, bottom = band_diff(a, b, 0, 600), band_diff(a, b, 900, 1200)
    print(f"selisih area atas={top:.2f} area subtitle={bottom:.2f}")
    assert bottom > 3 * max(top, 0.3), (top, bottom)


def test_failure_is_reported_not_faked(client, monkeypatch):
    def boom(url, jd):
        raise main.JobError("YouTube memblokir server ini (uji)")
    monkeypatch.setattr(main, "download_source", boom)
    jid = client.post("/api/jobs", json={"url": URL}).json()["id"]
    j = wait(client, jid)
    assert j["status"] == "error" and "memblokir" in j["error"] and j["result"] is None
    assert client.get(f"/api/jobs/{jid}/download").status_code == 409
    assert not list(main.WORK_DIR.glob("job-*"))  # folder sementara langsung dibersihkan


def test_source_too_long_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "probe_url", lambda url, jd: {"duration": 5000.0, "live": False, "title": ""})
    j = wait(client, client.post("/api/jobs", json={"url": URL}).json()["id"])
    assert j["status"] == "error" and "terlalu panjang" in j["error"]


def test_transcription_failure_fails_job(client, monkeypatch):
    def bad(path, cb=None):
        raise RuntimeError("model tidak bisa diunduh")
    monkeypatch.setattr(main, "transcribe", bad)
    j = wait(client, client.post("/api/jobs", json={"url": URL}).json()["id"])
    assert j["status"] == "error" and "Transkripsi gagal" in j["error"]


def test_queue_limit(client, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(main, "MAX_QUEUE", 1)
    monkeypatch.setattr(main, "probe_url", lambda url, jd: gate.wait(20) and {"duration": 70.0, "live": False, "title": ""})
    first = client.post("/api/jobs", json={"url": URL}).json()["id"]
    assert client.post("/api/jobs", json={"url": URL}).status_code == 503
    gate.set()
    assert wait(client, first)["status"] == "done"


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_HOUR", 2)
    monkeypatch.setattr(main, "probe_url", lambda url, jd: (_ for _ in ()).throw(main.JobError("x")))
    codes = [client.post("/api/jobs", json={"url": URL}).status_code for _ in range(3)]
    assert codes == [202, 202, 429]


def test_ttl_cleanup(client):
    jid = client.post("/api/jobs", json={"url": URL, "duration": 15, "subtitles": False}).json()["id"]
    assert wait(client, jid)["status"] == "done"
    d = Path(main.JOBS[jid]["dir"])
    assert d.exists()
    main.JOBS[jid]["finished"] = time.time() - main.JOB_TTL - 5
    assert main.purge_expired() == 1
    assert not d.exists() and client.get(f"/api/jobs/{jid}").status_code == 404


def test_health_degraded_without_js_runtime(client, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(main.shutil, "which", lambda n, *a, **k: None if n == "deno" else real(n, *a, **k))
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["checks"]["js_runtime"] is False


def test_startup_purges_only_orphan_job_dirs(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "job-orphan").mkdir(parents=True)
    (work / "bukan-job").mkdir()
    monkeypatch.setattr(main, "WORK_DIR", work)
    with TestClient(main.app):
        pass
    assert not (work / "job-orphan").exists() and (work / "bukan-job").exists()
