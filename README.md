# ClipForge — klip vertikal 9:16 dari video YouTube

Tempel link YouTube → pilih 15/30/45/60 detik → dapatkan klip MP4 vertikal (720×1280) dengan subtitle bertimestamp.
Frontend statis (Vercel) + backend FastAPI (FFmpeg, yt-dlp, faster-whisper). Target biaya: **Rp0**.

> **Gunakan hanya untuk video milik Anda atau yang Anda punya izinnya.** Mengunduh dan mengedit video orang lain dapat melanggar
> Ketentuan Layanan YouTube dan hak cipta. Antarmuka mewajibkan centang pernyataan hak/izin sebelum memproses.

## Yang sebenarnya dilakukan (tanpa klaim berlebihan)

| Tahap | Teknologi | Catatan |
|---|---|---|
| Unduh | yt-dlp + Deno + yt-dlp-ejs | maks 720p; default ≤ 10 menit dan ≤ 200 MB |
| Transkripsi + timestamp | faster-whisper (AI), model `base`, CPU | akurasi bahasa Indonesia terbatas; `small` lebih baik tetapi lebih lambat |
| Pemilihan momen | **Heuristik**: jendela dengan kata terpadat (+ bonus tanda ? dan !) | **Bukan** pemahaman semantik AI. Tanpa ucapan → klip dari awal video, dan hasil menyatakannya |
| Render | FFmpeg: crop tengah ke 9:16, H.264/AAC | tanpa pelacakan wajah |
| Subtitle | `.srt` (bisa diunduh) + tertanam di video | kalimat panjang dipecah ≤ 6 kata per cue |

Aplikasi hanya menampilkan "Klip siap" bila job berstatus `done` **dan** hasilnya lolos validasi `ffprobe` (720×1280, durasi sesuai).
Kegagalan di tahap mana pun ditampilkan apa adanya; folder sementara job gagal langsung dihapus.

## Arsitektur gratis yang realistis

```
HP / komputer ──► Frontend statis (Vercel) ──HTTPS──► Backend FastAPI (Docker) ──► YouTube
                                                        └ FFmpeg · yt-dlp · Whisper (CPU)
```

| Opsi backend | Biaya | Status |
|---|---|---|
| **A. Komputer sendiri + Docker + Cloudflare quick tunnel** (disarankan) | Rp0 | IP rumahan jauh lebih jarang diblokir YouTube daripada IP datacenter. Komputer harus menyala; URL tunnel berubah tiap dijalankan |
| B. VM/host Docker lain (mis. VM gratis seperti Oracle Always Free, atau free tier PaaS) | bervariasi | Tidak saya uji. Periksa batas RAM/CPU terkini; bila RAM kecil, Whisper bisa gagal (job berstatus galat, bukan sukses palsu) |
| C. Hugging Face Docker Space | **berbayar** | Dokumentasi resmi HF (dibaca 9 Okt 2026): Space Docker/Gradio butuh paket PRO untuk dibuat; hanya Static Space yang gratis. Jangan dipakai tanpa persetujuan biaya |

Hosting cloud gratis umumnya memakai IP datacenter yang sering diblokir YouTube ("Sign in to confirm you're not a bot"). Itu sebabnya opsi A direkomendasikan.

## Struktur repositori

```
index.html, config.js        frontend satu file (tanpa framework)
scripts/build.mjs            build statis: salin ke dist/, tulis config.js dari env CLIPFORGE_API_URL
vercel.json, package.json    konfigurasi Vercel (statis)
backend/main.py              API FastAPI (job asinkron)
backend/Dockerfile           image: Python 3.11, FFmpeg, Deno, model Whisper ter-cache
backend/tests/test_api.py    tes pytest
docker-compose.yml           jalankan backend lokal
```

## Menjalankan lokal

**Backend (Docker):** `docker compose up --build` → cek `http://localhost:7860/health`.

**Backend (tanpa Docker):** butuh Python 3.11+, FFmpeg, dan font Liberation (`fonts-liberation`).
```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 7860
```

**Frontend:** `python -m http.server 3000` di root repo, buka `http://localhost:3000` (otomatis memakai `http://localhost:7860`).

**Tes:** `cd backend && pip install -r requirements-dev.txt && pytest -q` (butuh `ffmpeg`/`ffprobe`; YouTube dan unduhan model Whisper dimock, FFmpeg nyata).

## Deploy

### 1. Frontend → Vercel
1. Vercel → *Add New → Project* → impor repo ini. Preset: **Other** (`vercel.json` sudah mengatur `node scripts/build.mjs` → `dist`).
2. *Environment Variables* (opsional): `CLIPFORGE_API_URL` = alamat backend HTTPS. Bisa juga diisi nanti lewat **Pengaturan backend** di halaman. Ubah env var → redeploy.
3. Nama repo ini (`-ai-video-editor.`) berawalan "-" dan berakhiran "."; beri nama proyek Vercel sendiri (mis. `clipforge`) atau ganti nama repo di GitHub *Settings*.
4. Paket Hobby Vercel ditujukan untuk penggunaan non-komersial; periksa ketentuan terbaru.

### 2. Backend → komputer sendiri + tunnel (Rp0)
1. `docker compose up --build`
2. Pasang [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/), lalu `cloudflared tunnel --url http://localhost:7860` dan salin URL `https://xxxx.trycloudflare.com`.
3. Buka situs Vercel → *Pengaturan backend* → tempel URL → *Simpan*. Titik status harus hijau ("Backend: siap").
4. Amankan: buat file `.env` berisi `ALLOWED_ORIGINS=https://NAMA-PROYEK.vercel.app`, lalu `docker compose up -d`. Siapa pun yang tahu URL tunnel tetap bisa memanggil API langsung; batasi dengan rate limit dan jangan sebar URL.

Quick tunnel Cloudflare: tanpa jaminan uptime, hostname berubah tiap dijalankan, maks 200 request bersamaan, tanpa SSE (aplikasi ini memakai polling).

## Konfigurasi backend (environment variable)

| Variabel | Default | Fungsi |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` | Origin CORS, pisahkan koma. **Isi dengan URL Vercel Anda** |
| `MAX_SOURCE_SECONDS` | 600 | Durasi video sumber maksimum |
| `MAX_DOWNLOAD_MB` | 200 | Ukuran unduhan maksimum |
| `MAX_QUEUE` | 3 | Job berjalan + menunggu; selebihnya ditolak (503) |
| `RATE_LIMIT_PER_HOUR` | 6 | Job per IP per jam (429); 0 = nonaktif |
| `JOB_TTL_SECONDS` | 1800 | Hasil dihapus setelah ini |
| `JOB_TIMEOUT_SECONDS` | 900 | Batas total satu job |
| `MIN_FREE_MB` | 500 | Tolak job bila disk kosong kurang dari ini |
| `WHISPER_MODEL` | `base` | `tiny`/`base`/`small`; ubah via build arg `WHISPER_MODEL` agar model ter-cache di image |
| `WORK_DIR` | `/tmp/clipforge` | Folder kerja sementara |
| `YTDLP_COOKIES` | kosong | Isi `cookies.txt` (rahasia) bila YouTube meminta login. Berisiko untuk akun; tidak ada jaminan |

## API

- `GET /health` → 200 `ok` atau 503 `degraded` (ffmpeg, yt-dlp, faster-whisper, atau Deno tidak ada)
- `POST /api/jobs` `{"url": "...", "duration": 15|30|45|60, "subtitles": true}` → 202 `{id, status, ...}`
- `GET /api/jobs/{id}` → `status` (`queued|running|done|error`), `stage`, `progress` 0–100, `message`, `error`, `result`
- `GET /api/jobs/{id}/download` (MP4) · `GET /api/jobs/{id}/subtitles.srt`
- Kode galat: 400 URL tidak valid · 422 input salah · 429 batas per jam · 503 antrean penuh/disk · 404/409 job tidak ada/belum selesai

## Batasan

- **YouTube dapat menolak server** (anti-bot). Pesan galatnya ditampilkan; tidak ada jaminan berhasil di hosting cloud.
- **yt-dlp harus sering diperbarui** karena YouTube berubah. Bangun ulang image (`docker compose build --no-cache`) atau `pip install -U "yt-dlp[default]"`. Deno wajib (sudah dipasang lewat `requirements.txt`).
- Satu proses berat sekaligus; tanpa autentikasi; hasil hilang saat server restart atau setelah ±30 menit.
- Crop tengah (bukan pelacakan wajah); subtitle mengikuti akurasi Whisper `base`.
- Hanya video publik, bukan siaran langsung, dan ≤ batas durasi.

## Troubleshooting

| Gejala | Penyebab / solusi |
|---|---|
| Titik status merah "tidak terjangkau" | Alamat salah, backend mati, atau tunnel berganti URL → tempel URL terbaru. Cek CORS (`ALLOWED_ORIGINS`) |
| "tidak lengkap (js_runtime…)" | Deno belum terpasang → pasang dari `requirements.txt` / rebuild image |
| "YouTube memblokir server ini" | Jalankan backend di jaringan rumah, coba lagi nanti, atau `YTDLP_COOKIES` |
| "Video tidak tersedia… perlu pembaruan yt-dlp" | Perbarui yt-dlp (lihat Batasan) |
| "Transkripsi gagal" | Model Whisper belum terunduh (koneksi ke huggingface.co) atau RAM habis → pakai `WHISPER_MODEL=tiny` |
| "Antrean penuh" / "Batas 6 proses per jam" | Tunggu, atau ubah `MAX_QUEUE` / `RATE_LIMIT_PER_HOUR` |
| Klip jadi tanpa subtitle | Tidak ada ucapan terdeteksi; aplikasi menyatakannya di hasil |

## Status pengujian

Diuji di sandbox: 15 tes pytest (API, FFmpeg/ffprobe nyata, subtitle tertanam terverifikasi per piksel, kegagalan, batas, TTL, pembersihan),
integrasi UI (jsdom) ↔ uvicorn dengan skenario sukses/gagal, build statis, `pip check`, deteksi Deno + yt-dlp-ejs oleh yt-dlp, preflight CORS, lint Dockerfile.

**Belum diuji:** unduhan YouTube sungguhan dan transkripsi Whisper sungguhan (sandbox tidak punya akses ke YouTube/huggingface.co), build image Docker, deploy Vercel, Cloudflare tunnel, dan pemutaran di browser Android nyata.

Versi yang diuji: Python 3.12, fastapi 0.143.0, uvicorn 0.54.0, pydantic 2.14.0, yt-dlp 2026.8.19, yt-dlp-ejs 0.8.0, deno 2.9.7, faster-whisper 1.2.1, ctranslate2 4.8.2, FFmpeg 6.1.1.
