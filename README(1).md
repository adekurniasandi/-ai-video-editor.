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

Backend FastAPI untuk ClipForge; dibangun dengan `Dockerfile` di folder ini (`docker compose up --build` dari root repo).
Front matter di atas hanya dipakai bila folder ini diunggah sebagai Docker Space Hugging Face — **Space Docker kini butuh paket berbayar**
(lihat README root). Panduan lengkap: README di root repositori.

Endpoint: `GET /health`, `POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/download`,
`GET /api/jobs/{id}/subtitles.srt`, `GET /docs`.
