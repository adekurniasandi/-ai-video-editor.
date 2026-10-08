---
title: ClipForge API
emoji: 🎬
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# ClipForge API

Backend API for the ClipForge YouTube Shorts Maker. Deploy this `backend/` folder as a Docker Space on Hugging Face. The free CPU tier may sleep and video rendering can time out; it is not an always-on production service.

## Local run

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 7860
```

FFmpeg must be installed separately. `GET /health` checks readiness. `POST /process` accepts JSON: `{"url":"https://youtu.be/VIDEO_ID","duration":30,"subtitles":true}` and returns an MP4.

## Limits and safety

- Only direct YouTube video URLs are accepted; playlists are not supported.
- Source videos are limited to 180 seconds; output clips are limited to 60 seconds.
- The current segment picker uses speech density, not semantic AI. Crop is centered, not face-tracked.
- Processing downloads video to temporary storage and deletes the job directory after the response; crashes can leave temporary files until the Space restarts.
- This is a prototype, not hardened for public multi-user use. Do not expose it publicly without authentication, request limits, concurrency control, and cleanup monitoring.
- Only process videos you have permission to download and edit. Respect YouTube terms and copyright.
