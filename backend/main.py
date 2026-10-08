import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

app = FastAPI(title="ClipForge API", version="0.1.0")

allowed_origins = [
    origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/clipforge"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_SOURCE_SECONDS = int(os.getenv("MAX_SOURCE_SECONDS", "180"))
MAX_OUTPUT_SECONDS = 60


class ProcessRequest(BaseModel):
    url: str
    duration: int = Field(default=30, ge=15, le=60)
    subtitles: bool = True


def validate_youtube_url(raw: str) -> str:
    try:
        parsed = urlparse(raw.strip())
    except Exception as exc:
        raise HTTPException(400, "URL tidak valid.") from exc
    host = (parsed.hostname or "").lower()
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
    if parsed.scheme not in {"http", "https"} or host not in allowed:
        raise HTTPException(400, "Hanya URL video YouTube yang didukung.")
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/")
    elif parsed.path.startswith("/shorts/"):
        video_id = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
    else:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise HTTPException(400, "Link harus mengarah ke satu video YouTube yang valid.")
    return "https://www.youtube.com/watch?v=" + video_id


def run_cmd(args, timeout=180):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Proses melewati batas waktu server gratis. Coba video lebih pendek.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "Perintah gagal")[-1800:]
        raise HTTPException(422, "Pemrosesan gagal: " + detail)
    return result.stdout


def cleanup(path: str):
    shutil.rmtree(path, ignore_errors=True)


@app.get("/")
def root():
    return {"name": "ClipForge API", "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "clipforge-api"}


@app.post("/process")
def process_video(req: ProcessRequest):
    canonical_url = validate_youtube_url(req.url)
    job_dir = tempfile.mkdtemp(prefix="job-", dir=str(WORK_DIR))
    try:
        # Metadata-only first: reject long videos before downloading.
        metadata_text = run_cmd([
            "yt-dlp", "--no-warnings", "--no-playlist", "--skip-download",
            "--print", "%(duration)s", canonical_url
        ], timeout=30).strip()
        try:
            source_duration = float(metadata_text.splitlines()[-1])
        except (ValueError, IndexError):
            raise HTTPException(422, "Durasi video tidak dapat dibaca. Pastikan video publik dan dapat diakses.")
        if source_duration <= 0 or source_duration > MAX_SOURCE_SECONDS:
            raise HTTPException(413, f"Durasi sumber harus maksimal {MAX_SOURCE_SECONDS} detik pada versi awal.")

        source_path = os.path.join(job_dir, "source.mp4")
        run_cmd([
            "yt-dlp", "--no-warnings", "--no-playlist",
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "--merge-output-format", "mp4", "-o", source_path, canonical_url
        ], timeout=120)
        if not os.path.exists(source_path):
            # yt-dlp may append a container extension on some formats.
            candidates = list(Path(job_dir).glob("source.*"))
            if not candidates:
                raise HTTPException(422, "File video tidak ditemukan setelah diunduh.")
            source_path = str(candidates[0])

        from faster_whisper import WhisperModel
        model_size = os.getenv("WHISPER_MODEL", "tiny")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments_iter, _ = model.transcribe(source_path, beam_size=1, vad_filter=True)
        transcript = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if text:
                transcript.append({"start": float(seg.start), "end": float(seg.end), "text": text})
        del model

        clip_duration = min(req.duration, MAX_OUTPUT_SECONDS, int(source_duration))
        if clip_duration < 1:
            raise HTTPException(422, "Video terlalu pendek.")
        # Choose a speech-dense segment as a transparent, lightweight baseline.
        # A semantic model can replace this heuristic in a later version.
        max_start = max(0.0, source_duration - clip_duration)
        step = max(1.0, min(5.0, clip_duration / 4))
        candidates = [i * step for i in range(int(max_start / step) + 1)]
        best_start, best_score = 0.0, -1.0
        for start in candidates:
            end = start + clip_duration
            words = sum(len(item["text"].split()) for item in transcript if item["start"] < end and item["end"] > start)
            covered = sum(max(0.0, min(end, item["end"]) - max(start, item["start"])) for item in transcript)
            score = words + covered * 0.15
            if score > best_score:
                best_start, best_score = start, score
        clip_path = os.path.join(job_dir, "clip.mp4")
        # Center crop to a 9:16 canvas, preserving audio. This is center crop, not face tracking.
        vf = "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"
        if req.subtitles and transcript:
            ass_path = os.path.join(job_dir, "captions.ass")
            write_ass(ass_path, transcript, best_start, clip_duration)
            vf += ",ass=" + ass_path.replace("\\", "/").replace(":", "\\:")
        run_cmd([
            "ffmpeg", "-y", "-ss", str(best_start), "-i", source_path,
            "-t", str(clip_duration), "-vf", vf, "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", "27", "-c:a", "aac",
            "-b:a", "96k", "-movflags", "+faststart", clip_path
        ], timeout=180)
        if not os.path.isfile(clip_path) or os.path.getsize(clip_path) < 1024:
            raise HTTPException(500, "File hasil tidak berhasil dibuat.")
        return FileResponse(
            clip_path,
            media_type="video/mp4",
            filename="clipforge-shorts.mp4",
            background=BackgroundTask(cleanup, job_dir),
        )
    except HTTPException:
        cleanup(job_dir)
        raise
    except Exception as exc:
        cleanup(job_dir)
        raise HTTPException(500, "Kesalahan server: " + str(exc)[:500]) from exc


def write_ass(path, transcript, clip_start, clip_duration):
    def stamp(seconds):
        seconds = max(0.0, seconds)
        centiseconds = int(seconds * 100)
        hours, remainder = divmod(centiseconds, 360000)
        minutes, remainder = divmod(remainder, 6000)
        secs, cs = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,54,&H00FFFFFF,&H0000FFFF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,42,42,115,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in transcript:
        start = max(item["start"], clip_start)
        end = min(item["end"], clip_start + clip_duration)
        if end <= start:
            continue
        text = item["text"].replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        text = text.replace("\n", " ")
        lines.append(f"Dialogue: 0,{stamp(start-clip_start)},{stamp(end-clip_start)},Default,,0,0,0,,{text}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")
