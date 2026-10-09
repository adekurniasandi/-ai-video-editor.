FROM python:3.11-slim

# HF Spaces menjalankan container sebagai user 1000: siapkan HOME & PATH yang bisa ditulis.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface WORK_DIR=/tmp/clipforge

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-liberation \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 user
USER user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --user -r requirements.txt

# Unduh model Whisper saat build agar job pertama tidak menunggu (tidak fatal jika gagal).
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], device='cpu', compute_type='int8')" \
 || echo "PERINGATAN: prefetch model gagal; model diunduh saat job pertama."

COPY --chown=user main.py .
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:7860/health', timeout=4)"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
