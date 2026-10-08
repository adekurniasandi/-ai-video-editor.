# ClipForge — YouTube Shorts Maker

Mobile-friendly, free-first prototype for creating vertical MP4 clips from permitted YouTube videos.

## Status
- Responsive frontend: `index.html`
- FastAPI processing scaffold: `backend/main.py`
- Docker deployment files: `backend/Dockerfile`, `backend/README.md`
- Not yet deployed or end-to-end tested. Configure a cloud backend before using the processing button.

## Deploy from phone
1. Create a new Docker Space on Hugging Face.
2. Upload the contents of the `backend/` folder to the root of that Space, including its Dockerfile and README.
3. Wait for the Space build and test `https://YOUR-SPACE.hf.space/health`.
4. Import this repository into Vercel as a static site (Framework Preset: Other).
5. In the site, open Pengaturan backend, enter the Space URL, and save it.
6. Check backend status and test with a short video you own or have permission to process.

## Limitations
Free cloud servers can sleep, throttle, or time out. The current clip selector uses speech density, not semantic understanding; crop is centered and does not track faces. The prototype has no authentication, queue, or rate limit, so do not expose the backend publicly until these controls are added. Respect YouTube terms and copyright.

## API
- `GET /health`: health check
- `POST /process`: JSON body with `url`, `duration` (15–60), and `subtitles` (boolean); returns MP4 on success.
- `GET /docs`: API documentation
