# 🎤 CoverCraft

> **Otomatik AI Şarkı Kapağı (AI Cover) ve YouTube Video Yayınlama Otomasyonu**  
> Demucs v4 stem ayrıştırma, RVC v2 ses dönüştürme, FFmpeg EBU R128 iki geçişli ses normalizasyonu & 1080p video oluşturma ve YouTube Data API v3 otomatik yükleyicisini bir araya getiren dayanıklı otomasyon sistemi.

---

## 🌟 Mimari ve Öne Çıkan Özellikler

- 🛡️ **Güvenli RVC Dönüşüm Hattı**: RVC veya model hatası durumunda asla orijinal vokali kopyalamaz; telif ve hatalı yükleme riskini kesin olarak engeller.
- 📋 **Pydantic Tabanlı Durum Makinesi & Exponential Backoff**:
  - `pending` → `in_progress` → `uploading` → `completed` (veya `failed`).
  - Hata sınıflandırması (`transient` / `permanent`) ve üstel gecikmeyle (`next_retry_at`) güvenli yeniden deneme.
  - Path traversal (`../`) koruması (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$`).
- 🔒 **Süreçler Arası Dosya Kilitleme & Kurtarma (`src/queue_manager.py`)**:
  - Ayrık `queue.lock` dosyası üzerinden atomik okuma/yazma (`fsync` + `os.replace`).
  - Bozuk JSON durumunda `.corrupted` arşivleme ve `.bak` yedeğinden otomatik onarım.
  - Çökmüş `in_progress` görevlerini zaman aşımı ile kurtarma, `uploading` durumunda askıda kalan görevler için mükerrer yükleme koruması ve `--reconcile` desteği.
- 🎚️ **Profesyonel Ses & Video İşleme**:
  - Demucs `htdemucs` iki kanallı vokal/enstrümantal ayrıştırma.
  - İki geçişli FFmpeg `loudnorm` (EBU R128) ses seviyesi normalizasyonu (`amix=duration=longest`).
  - Görsel en-boy oranını bozmadan 1920x1080 çerçeveye sığdırma ve `ffprobe` ile süre/bütünlük/tolerans doğrulaması.
- 💓 **Bağımsız Heartbeat & Canlılık Takibi**:
  - Uzun süren (20+ dk) GPU/Demucs işlemlerinde container'ın sahte `unhealthy` durumuna düşmesini engelleyen bağımsız arka plan heartbeat iş parçacığı.
- 🔑 **Headless / Docker Uyumlu JSON OAuth**:
  - `pickle` yerine güvenli `token.json` serileştirmesi ve `0o600` dosya izinleri.
  - Tek komutla (`python -m src.main --auth`) yerel tarayıcı üzerinden yetkilendirme.
- ⏰ **Saat Dilimi, Sinyal Yönetimi & Catch-up Desteği**:
  - `Europe/Istanbul` desteği, son 7 günün sınırlandırılmış catch-up taraması ve döngü başına 1 video yayın limiti.
  - `SIGTERM`/`SIGINT` durumunda alt süreç ağacını (`terminate_process_tree`) hiyerarşik ve temiz sonlandırma.

---

## 📁 Proje Klasör Yapısı

```text
CoverCraft/
│
├── models/                    # RVC modelleri (.pth ve .index)
├── assets/                    # Arka plan görselleri (cover.jpg)
├── outputs/                   # Görev bazlı çıktı dizinleri (outputs/<task_id>/)
│
├── config/
│   ├── queue.json             # Pydantic şemalı görev kuyruğu
│   ├── queue.json.bak         # Otomatik yedek dosyası
│   ├── queue.lock             # Ayrık dosya kilidi
│   ├── client_secret.json     # Google Cloud OAuth istemci anahtarı
│   ├── token.json             # Üretilen OAuth erişim token'ı (gitignore korumalı)
│   └── heartbeat.json         # Zamanlayıcı canlılık (heartbeat) dosyası
│
├── src/
│   ├── __init__.py
│   ├── models.py              # CoverTask, TaskStatus, ErrorCategory Pydantic modelleri
│   ├── queue_manager.py       # Ayrık kilitli atomik kuyruk & durum yönetimi
│   ├── audio_pipeline.py      # yt-dlp, Demucs stem ayırma & RVC dönüşümü & EBU R128 miks
│   ├── video_generator.py     # 1920x1080 MP4 render & ffprobe doğrulama
│   ├── youtube_uploader.py    # 429/5xx retry destekli, JSON token'lı YouTube API yükleyicisi
│   ├── scheduler.py           # Arka plan heartbeat iş parçacıklı zamanlayıcı
│   ├── utils.py               # Hata mesajı sanitizasyonu & hiyerarşik süreç sonlandırma
│   └── main.py                # Kapsamlı CLI yönetim arayüzü
│
├── tests/                     # 17 adet pytest birim ve entegrasyon testi
├── Dockerfile                 # Non-root appuser (UID 1000) + FFmpeg + CUDA
├── compose.yaml               # GPU rezervasyonlu servis yapılandırması
├── pytest.ini                 # Katı uyarı filtreli test yapılandırması
├── requirements.txt           # Python bağımlılıkları
├── .gitignore                 # Güvenlik ve temizlik kuralları
└── README.md                  # Proje dokümantasyonu
```

---

## ⚙️ Kuyruk Yapılandırması (`config/queue.json`)

```json
[
  {
    "id": "2026-08-12-baris-manco-ornek",
    "date": "2026-08-12",
    "youtube_url": "https://www.youtube.com/watch?v=EXAMPLE_1",
    "model_name": "baris_manco",
    "pitch_shift": 0,
    "video_title": "Barış Manço AI Cover - Örnek Şarkı",
    "video_description": "CoverCraft otomasyonu ve RTX GPU kullanılarak üretilmiştir.",
    "tags": ["Barış Manço", "AICover", "CoverCraft", "Music"],
    "rights_confirmed": true,
    "synthetic_declaration": true,
    "status": "pending",
    "attempts": 0,
    "max_attempts": 3,
    "started_at": null,
    "processed_at": null,
    "next_retry_at": null,
    "error_category": null,
    "last_error": null,
    "youtube_video_id": null
  }
]
```

---

## 🚀 Kullanım ve CLI Komutları (Modül Biçimi)

### 1. Testleri Çalıştırma
```bash
python -m pytest -v
```

### 2. Sistem ve Ortam Doğrulaması
```bash
python -m src.main --validate
```

### 3. YouTube OAuth Yetkilendirmesi (JSON Token Üretimi)
Docker veya sunucuya geçmeden önce yerel ortamda bir defaya mahsus:
```bash
python -m src.main --auth
```
Bu işlem `config/token.json` dosyasını oluşturur.

### 4. Bekleyen Görevleri Hemen Çalıştırma
```bash
python -m src.main --now
```

### 5. Tekil Bir Görevi ID ile Çalıştırma
```bash
python -m src.main --task-id 2026-08-12-baris-manco-ornek
```

### 6. Askıda Kalan Görevi Uzlaştırma (Reconciliation)
```bash
python -m src.main --reconcile 2026-08-12-baris-manco-ornek --resolve-as completed --video-id <YOUTUBE_ID>
```

### 7. Zamanlayıcıyı Başlatma
```bash
python -m src.main --scheduler --time 18:00
```

---

## 🐳 Docker ile Canlı GPU ve Yayın Doğrulama Protokolü

```bash
# 1. Testlerin doğrulanması
python -m pytest -v

# 2. Docker compose konfigürasyon doğrulaması
docker compose config

# 3. İmajın derlenmesi
docker compose build

# 4. GPU erişim kontrolü (NVIDIA Container Toolkit)
docker compose run --rm covercraft nvidia-smi

# 5. PyTorch CUDA kontrolü
docker compose run --rm covercraft python -c "import torch; print('CUDA Aktif:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0))"

# 6. Container içi araç doğrulaması
docker compose run --rm covercraft python -m src.main --validate

# 7. Servisi arka planda başlatma
docker compose up -d

# 8. Canlı durum ve Healthcheck takibi
docker compose ps
docker inspect --format '{{json .State.Health}}' covercraft_app
docker compose logs -f covercraft
```
