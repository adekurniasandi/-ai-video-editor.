"""ClipForge API: URL YouTube -> klip vertikal 9:16 + subtitle (SRT dan tertanam).

Prototipe gratis: satu proses berat sekaligus, antrean kecil, file sementara dibersihkan otomatis.
Transkripsi = Whisper (AI). Pemilihan segmen = HEURISTIK kepadatan ucapan, BUKAN analisis semantik AI.
"""
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

VERSION = "1.0.0"
log = logging.getLogger("clipforge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/clipforge"))
MAX_SOURCE_SECONDS = _int("MAX_SOURCE_SECONDS", 600)   # durasi video sumber maksimum
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 200)         # ukuran unduhan maksimum
MAX_QUEUE = _int("MAX_QUEUE", 3)                       # job berjalan + menunggu
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 6)   # per IP; 0 = nonaktif
JOB_TTL = _int("JOB_TTL_SECONDS", 1800)                # hasil dihapus setelah ini
JOB_TIMEOUT = _int("JOB_TIMEOUT_SECONDS", 900)         # batas total satu job
MIN_FREE_MB = _int("MIN_FREE_MB", 500)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
OUT_W, OUT_H = 720, 1280

JOBS = {}
HITS = defaultdict(deque)
LOCK = threading.Lock()
SLOT = threading.Semaphore(1)  # satu proses berat sekaligus (CPU gratis terbatas)


class JobError(Exception):
    """Pesan aman ditampilkan ke pengguna (bahasa Indonesia)."""


class JobRequest(BaseModel):
    url: str
    duration: Literal[15, 30, 45, 60] = 30
    subtitles: bool = True


# ---------- util ----------
def canonical_url(raw):
    try:
        p = urlparse((raw or "").strip())
    except ValueError:
        raise HTTPException(400, "URL tidak valid.")
    host = (p.hostname or "").lower()
    hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
    if p.scheme not in ("http", "https") or host not in hosts:
        raise HTTPException(400, "Hanya URL video YouTube yang didukung.")
    parts = [x for x in p.path.split("/") if x]
    if host == "youtu.be":
        vid = parts[0] if parts else ""
    elif parts[:1] in (["shorts"], ["embed"], ["live"]):
        vid = parts[1] if len(parts) > 1 else ""
    else:
        vid = parse_qs(p.query).get("v", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise HTTPException(400, "Link harus mengarah ke satu video YouTube yang valid.")
    return "https://www.youtube.com/watch?v=" + vid


def run(cmd, timeout, cwd=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise JobError("Proses melewati batas waktu server. Coba durasi atau video yang lebih pendek.")
    except FileNotFoundError:
        raise JobError(f"Program '{cmd[0]}' tidak ditemukan di server.")


YT_ERRORS = (
    ("sign in to confirm", "YouTube memblokir server ini (verifikasi anti-bot). Coba lagi nanti atau pasang cookies (lihat README)."),
    ("private video", "Video ini privat."),
    ("members-only", "Video khusus member tidak didukung."),
    ("confirm your age", "Video dibatasi usia dan tidak dapat diproses."),
    ("video unavailable", "Video tidak tersedia atau tidak dapat diakses dari server."),
    ("not available", "Video tidak tersedia dari lokasi server atau perlu pembaruan yt-dlp."),
    ("http error 429", "YouTube membatasi permintaan (429). Coba lagi beberapa menit lagi."),
    ("live event", "Siaran langsung belum didukung."),
)


def explain_ytdlp(text):
    low = text.lower()
    for key, msg in YT_ERRORS:
        if key in low:
            return msg
    last = (text.strip().splitlines() or ["tidak diketahui"])[-1]
    return "Gagal mengambil video dari YouTube: " + last[:200]


def ytdlp(args, timeout, job_dir):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
           "--socket-timeout", "20", "--retries", "2"]
    cookies = os.getenv("YTDLP_COOKIES", "").strip()  # opsional: isi cookies.txt (rahasia)
    if cookies:
        cookie_file = Path(job_dir) / "cookies.txt"
        cookie_file.write_text(cookies + "\n", encoding="utf-8")
        cmd += ["--cookies", str(cookie_file)]
    r = run(cmd + args, timeout, cwd=job_dir)
    if r.returncode != 0:
        raise JobError(explain_ytdlp(r.stderr or r.stdout or ""))
    return r


# ---------- tahap pipeline (dipisah agar mudah diuji) ----------
def probe_url(url, job_dir):
    r = ytdlp(["--skip-download", "--print", "duration", "--print", "is_live", "--print", "title", url], 60, job_dir)
    lines = r.stdout.strip().splitlines()
    try:
        duration = float(lines[0])
    except (ValueError, IndexError):
        duration = None
    return {"duration": duration, "live": (lines[1:2] == ["True"]), "title": (lines[2] if len(lines) > 2 else "")[:120]}


def download_source(url, job_dir):
    ytdlp(["-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
           "--max-filesize", f"{MAX_DOWNLOAD_MB}M", "-o", str(Path(job_dir) / "source.%(ext)s"), url], 300, job_dir)
    files = sorted((f for f in Path(job_dir).glob("source.*") if f.suffix not in (".part", ".ytdl")),
                   key=lambda f: f.stat().st_size, reverse=True)
    if not files:
        raise JobError(f"File video tidak ditemukan setelah diunduh (mungkin melebihi batas {MAX_DOWNLOAD_MB} MB).")
    if files[0].stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise JobError(f"Ukuran unduhan melebihi batas {MAX_DOWNLOAD_MB} MB.")
    return str(files[0])


_model = None
_model_lock = threading.Lock()


def transcribe(path, on_progress=None):
    """Transkripsi dengan faster-whisper. Mengembalikan (segmen, bahasa)."""
    global _model
    from faster_whisper import WhisperModel
    with _model_lock:
        if _model is None:
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = _model.transcribe(path, beam_size=1, vad_filter=True)
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            out.append({"start": float(s.start), "end": float(s.end), "text": text})
        if on_progress and info.duration:
            on_progress(min(1.0, s.end / info.duration))
    return out, info.language


def pick_segment(transcript, source_dur, clip_len):
    """HEURISTIK: jendela dengan kata paling padat (+ bonus tanda tanya/seru). Bukan pemahaman semantik."""
    length = min(clip_len, int(source_dur))
    max_start = max(0.0, source_dur - length)
    if not transcript:
        return 0.0, length, "awal_video", 0.0
    best_start, best_score = 0.0, -1.0
    for start in sorted({min(s["start"], max_start) for s in transcript}):
        end = start + length
        words = emph = 0.0
        for s in transcript:
            if s["end"] <= start or s["start"] >= end:
                continue
            share = (min(end, s["end"]) - max(start, s["start"])) / max(0.01, s["end"] - s["start"])
            words += len(s["text"].split()) * share
            emph += (s["text"].count("?") + s["text"].count("!")) * share
        score = words + 2 * emph
        if score > best_score:
            best_start, best_score = start, score
    return best_start, length, "kepadatan_ucapan", round(best_score, 1)


def make_cues(transcript, start, length, max_words=6):
    """Potong kalimat panjang jadi cue pendek; waktu relatif terhadap awal klip."""
    cues, end_limit = [], start + length
    for s in transcript:
        words = s["text"].split()
        chunks = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
        total = sum(len(c) for c in chunks) or 1
        t, span = s["start"], s["end"] - s["start"]
        for c in chunks:
            d = span * len(c) / total
            a, b = max(t, start), min(t + d, end_limit)
            if b - a >= 0.2:
                cues.append((a - start, b - start, c))
            t += d
    return cues


def srt_time(x):
    ms = int(round(max(0.0, x) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(path, cues):
    Path(path).write_text("".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n\n"
                                  for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")


# Skala SRT di libass = 288 tinggi virtual (x4,44 pada 1280 px): FontSize=12 ~ 53 px, MarginV=32 ~ 142 px.
SUB_STYLE = ("FontName=Liberation Sans,FontSize=12,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
             "BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginV=32,MarginL=10,MarginR=10")


def render(job_dir, src, start, length, burn):
    vf = f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},setsar=1"
    if burn:  # berkas relatif terhadap cwd=job_dir agar tidak perlu escape path
        vf += f",subtitles=captions.srt:force_style='{SUB_STYLE}'"
    r = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", src,
             "-t", str(length), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "clip.mp4"],
            300, cwd=job_dir)
    if r.returncode != 0:
        log.error("ffmpeg gagal: %s", r.stderr[-800:])
        raise JobError("Render video gagal: " + ((r.stderr.strip().splitlines() or ["ffmpeg error"])[-1][:200]))


def validate_output(path, expected):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)], 30)
    try:
        info = json.loads(r.stdout)
        st, dur = info["streams"][0], float(info["format"]["duration"])
    except (ValueError, KeyError, IndexError):
        raise JobError("Hasil render tidak valid (tidak dapat dibaca ffprobe).")
    if (st["width"], st["height"]) != (OUT_W, OUT_H) or abs(dur - expected) > 2.5 or Path(path).stat().st_size < 2048:
        raise JobError("Hasil render tidak sesuai (ukuran/durasi salah). Coba lagi.")
    return dur


# ---------- manajemen job ----------
def set_job(job, **kw):
    with LOCK:
        job.update(kw)


def check_deadline(job):
    if time.time() > job["deadline"]:
        raise JobError("Proses melewati batas waktu server gratis. Coba video atau durasi yang lebih pendek.")


def fail(job, msg):
    set_job(job, status="error", error=msg, message=msg, finished=time.time())
    shutil.rmtree(job["dir"], ignore_errors=True)


def run_job(job):
    jd = job["dir"]
    try:
        with SLOT:
            job["deadline"] = time.time() + JOB_TIMEOUT
            set_job(job, status="running", stage="memeriksa", progress=3, message="Memeriksa video…")
            meta = probe_url(job["url"], jd)
            if meta.get("live"):
                raise JobError("Siaran langsung belum didukung.")
            src_dur = meta.get("duration") or 0
            if src_dur < 1:
                raise JobError("Durasi video tidak terbaca. Pastikan video publik dan bukan siaran langsung.")
            if src_dur > MAX_SOURCE_SECONDS:
                raise JobError(f"Video terlalu panjang ({int(src_dur // 60)} menit). Batas saat ini {MAX_SOURCE_SECONDS // 60} menit.")
            set_job(job, stage="mengunduh", progress=10, message="Mengunduh video…")
            src = download_source(job["url"], jd)
            check_deadline(job)

            set_job(job, stage="transkripsi", progress=35, message="Mentranskripsi suara (Whisper)…")

            def on_progress(f):
                set_job(job, progress=35 + int(40 * f))
                check_deadline(job)
            try:
                transcript, lang = transcribe(src, on_progress)
            except JobError:
                raise
            except Exception:
                log.exception("transkripsi gagal")
                raise JobError("Transkripsi gagal (model Whisper tidak dapat dimuat atau memori server habis).")

            set_job(job, stage="memilih", progress=78, message="Memilih segmen…")
            start, length, method, score = pick_segment(transcript, src_dur, job["duration"])
            cues = make_cues(transcript, start, length) if job["subtitles"] else []
            if cues:
                write_srt(Path(jd) / "captions.srt", cues)
            set_job(job, stage="render", progress=82, message="Membuat video vertikal 9:16…")
            render(jd, src, start, length, burn=bool(cues))
            check_deadline(job)

            set_job(job, stage="validasi", progress=96, message="Memeriksa hasil…")
            out_dur = validate_output(Path(jd) / "clip.mp4", length)
            for f in Path(jd).glob("source.*"):
                f.unlink(missing_ok=True)
            (Path(jd) / "cookies.txt").unlink(missing_ok=True)

            note = ("Heuristik kepadatan ucapan (bukan analisis semantik AI)." if method == "kepadatan_ucapan"
                    else "Tidak ada ucapan terdeteksi; klip diambil dari awal video tanpa analisis.")
            result = {
                "title": meta.get("title", ""), "start": round(start, 2), "end": round(start + length, 2),
                "duration": round(out_dur, 2), "requested_duration": job["duration"], "method": method,
                "method_note": note, "score": score, "language": lang, "ttl_seconds": JOB_TTL,
                "size_bytes": (Path(jd) / "clip.mp4").stat().st_size,
                "subtitles": {"requested": job["subtitles"], "burned": bool(cues), "cues": len(cues), "srt": bool(cues)},
            }
            msg = "Klip siap diunduh."
            if job["subtitles"] and not cues:
                msg = "Klip siap, tetapi TANPA subtitle: tidak ada ucapan terdeteksi."
            set_job(job, status="done", stage="selesai", progress=100, message=msg, result=result, finished=time.time())
    except JobError as exc:
        fail(job, str(exc))
    except Exception:
        log.exception("job %s gagal tak terduga", job["id"])
        fail(job, "Terjadi kesalahan tak terduga di server. Coba lagi nanti.")


def public(job):
    return {k: job.get(k) for k in ("id", "status", "stage", "progress", "message", "error", "result")}


def purge_expired(now=None):
    now = now or time.time()
    with LOCK:
        old = [j for j in JOBS.values() if j["status"] in ("done", "error") and now - j["finished"] > JOB_TTL]
        for j in old:
            JOBS.pop(j["id"], None)
        for ip in [k for k, q in HITS.items() if not q or now - q[-1] > 3600]:
            HITS.pop(ip, None)
    for j in old:
        shutil.rmtree(j["dir"], ignore_errors=True)
    return len(old)


def janitor():
    while True:
        time.sleep(30)
        try:
            purge_expired()
        except Exception:
            log.exception("janitor gagal")


@asynccontextmanager
async def lifespan(_app):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for p in WORK_DIR.glob("job-*"):  # sisa proses sebelumnya (crash/restart)
        shutil.rmtree(p, ignore_errors=True)
    threading.Thread(target=janitor, daemon=True, name="janitor").start()
    yield


app = FastAPI(title="ClipForge API", version=VERSION, lifespan=lifespan)
origins = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


def client_ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@app.get("/")
def root():
    return {"name": "ClipForge API", "version": VERSION, "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    checks = {
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "yt_dlp": importlib.util.find_spec("yt_dlp") is not None,
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "js_runtime": bool(shutil.which("deno")),  # wajib untuk YouTube: hanya deno yang aktif default di yt-dlp
    }
    ready = all(checks.values())
    with LOCK:
        active = sum(1 for j in JOBS.values() if j["status"] in ("queued", "running"))
    body = {"status": "ok" if ready else "degraded", "service": "clipforge-api", "version": VERSION,
            "checks": checks, "active_jobs": active, "whisper_model": WHISPER_MODEL,
            "limits": {"max_source_seconds": MAX_SOURCE_SECONDS, "max_download_mb": MAX_DOWNLOAD_MB,
                       "max_queue": MAX_QUEUE, "rate_limit_per_hour": RATE_LIMIT_PER_HOUR, "job_ttl_seconds": JOB_TTL}}
    return JSONResponse(body, status_code=200 if ready else 503)


@app.post("/api/jobs", status_code=202)
def create_job(req: JobRequest, request: Request):
    url = canonical_url(req.url)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(WORK_DIR).free < MIN_FREE_MB * 1024 * 1024:
        raise HTTPException(503, "Penyimpanan server hampir penuh. Coba lagi nanti.")
    with LOCK:
        if sum(1 for j in JOBS.values() if j["status"] in ("queued", "running")) >= MAX_QUEUE:
            raise HTTPException(503, "Antrean penuh. Coba lagi beberapa menit lagi.")
        if RATE_LIMIT_PER_HOUR > 0:
            now, q = time.time(), HITS[client_ip(request)]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= RATE_LIMIT_PER_HOUR:
                raise HTTPException(429, f"Batas {RATE_LIMIT_PER_HOUR} proses per jam tercapai. Coba lagi nanti.")
            q.append(now)
        jid = uuid.uuid4().hex[:16]
        job = {"id": jid, "url": url, "duration": req.duration, "subtitles": req.subtitles, "status": "queued",
               "stage": "antre", "progress": 0, "message": "Menunggu giliran…", "error": None, "result": None,
               "dir": tempfile.mkdtemp(prefix="job-", dir=WORK_DIR), "finished": 0, "deadline": time.time() + JOB_TIMEOUT}
        JOBS[jid] = job
    threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{jid}").start()
    return public(job)


def get_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan atau sudah kedaluwarsa.")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    with LOCK:
        return public(job)


def job_file(job_id, name):
    job = get_job(job_id)
    path = Path(job["dir"]) / name
    if job["status"] != "done" or not path.is_file():
        raise HTTPException(409 if job["status"] != "done" else 404, "Berkas belum tersedia.")
    return job, path


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job, path = job_file(job_id, "clip.mp4")
    return FileResponse(path, media_type="video/mp4", filename=f"clipforge-{job['id']}.mp4")


@app.get("/api/jobs/{job_id}/subtitles.srt")
def subtitles(job_id: str):
    job, path = job_file(job_id, "captions.srt")
    return FileResponse(path, media_type="application/x-subrip", filename=f"clipforge-{job['id']}.srt")
