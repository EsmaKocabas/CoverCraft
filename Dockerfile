FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Europe/Istanbul
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Sistem bağımlılıkları, FFmpeg, git, wget ve Saat Dilimi verisi
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    wget \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Non-root kullanıcı oluşturma (UID 1000)
RUN useradd -m -u 1000 -s /bin/bash appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# RVC CLI paketini yüklüyoruz
RUN pip install --no-cache-dir rvc-python

# Gerekli volume dizinlerini önceden oluştur ve sahipliği appuser'a ver
RUN mkdir -p /app/models /app/assets /app/outputs /app/config \
    && chown -R appuser:appuser /app

COPY . .
RUN chown -R appuser:appuser /app

USER appuser

# Graceful shutdown sinyali
STOPSIGNAL SIGTERM

# Container canlılık ve heartbeat kontrolü (Healthcheck)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -m src.main --healthcheck || exit 1

CMD ["python", "-m", "src.main", "--scheduler"]
