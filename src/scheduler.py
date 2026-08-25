import os
import sys
import time
import json
import signal
import shutil
import logging
import threading
import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List

import schedule

# Proje kök dizinini Python path'e ekle
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.models import CoverTask, TaskStatus, ErrorCategory
from src.queue_manager import QueueManager
from src.audio_pipeline import AudioPipeline
from src.video_generator import VideoGenerator
from src.youtube_uploader import YouTubeUploader
from src.utils import sanitize_error_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("CoverCraft.Scheduler")

TIMEZONE_STR = os.environ.get("TZ", "Europe/Istanbul")
try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_STR)
except Exception:
    LOCAL_TZ = datetime.timezone.utc
    logger.warning(f"Geçersiz saat dilimi ({TIMEZONE_STR}), UTC kullanılıyor.")


class HeartbeatWorker(threading.Thread):
    def __init__(self, heartbeat_file: str, get_status_callback, interval: float = 15.0):
        super().__init__(daemon=True, name="HeartbeatWorker")
        self.heartbeat_file = heartbeat_file
        self.get_status_callback = get_status_callback
        self.interval = interval
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                os.makedirs(os.path.dirname(self.heartbeat_file), exist_ok=True)
                status_info = self.get_status_callback()
                data = {
                    "last_heartbeat": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "status": status_info.get("status", "running"),
                    "pid": os.getpid(),
                    "active_task": status_info.get("active_task"),
                    "timezone": TIMEZONE_STR
                }
                tmp = f"{self.heartbeat_file}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.heartbeat_file)
            except Exception as e:
                logger.warning(f"Heartbeat yazma uyarısı: {e}")

            time.sleep(self.interval)


class CoverCraftOrchestrator:
    def __init__(
        self,
        models_dir: str = "./models",
        outputs_dir: str = "./outputs",
        assets_dir: str = "./assets",
        queue_file: str = "./config/queue.json",
        client_secret: str = "./config/client_secret.json",
        token_path: str = "./config/token.json",
        heartbeat_file: str = "./config/heartbeat.json",
        max_catchup_days: int = 7,
        max_tasks_per_cycle: int = 1
    ):
        self.models_dir = os.path.abspath(models_dir)
        self.outputs_dir = os.path.abspath(outputs_dir)
        self.assets_dir = os.path.abspath(assets_dir)
        self.heartbeat_file = os.path.abspath(heartbeat_file)
        self.cover_image = os.path.join(self.assets_dir, "cover.jpg")
        self.max_catchup_days = max_catchup_days
        self.max_tasks_per_cycle = max_tasks_per_cycle

        self.queue_manager = QueueManager(queue_file=queue_file)
        self.audio_pipeline = AudioPipeline(models_dir=self.models_dir, outputs_dir=self.outputs_dir)
        self.video_generator = VideoGenerator(default_cover_path=self.cover_image)
        self.youtube_uploader = YouTubeUploader(client_secret_path=client_secret, token_path=token_path)
        
        self._is_processing = False
        self._shutdown_requested = False
        self._current_task_id: Optional[str] = None
        self._status: str = "idle"

        self._heartbeat_worker = HeartbeatWorker(
            heartbeat_file=self.heartbeat_file,
            get_status_callback=self._get_heartbeat_status
        )

        try:
            signal.signal(signal.SIGINT, self._handle_shutdown)
            signal.signal(signal.SIGTERM, self._handle_shutdown)
        except Exception:
            pass

    def _get_heartbeat_status(self) -> dict:
        return {
            "status": self._status,
            "active_task": self._current_task_id
        }

    def _handle_shutdown(self, signum, frame):
        logger.info(f"Kapanma sinyali alındı ({signum}). Çalışan alt süreçler hiyerarşik olarak durduruluyor...")
        self._shutdown_requested = True
        self._status = "shutting_down"
        self.audio_pipeline.terminate_current_process()
        self.video_generator.terminate_current_process()

    @staticmethod
    def get_current_date() -> str:
        return datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    def execute_task(self, task: CoverTask, cleanup_temp_files: bool = True) -> bool:
        if task.status == TaskStatus.COMPLETED:
            logger.info(f"Görev zaten tamamlanmış, atlanıyor: {task.id} (Video ID: {task.youtube_video_id})")
            return True

        if task.status == TaskStatus.UPLOADING:
            logger.warning(
                f"Görev UPLOADING durumunda askıda kalmış: {task.id}. "
                "Mükerrer video yüklemesini önlemek için manuel kontrol veya 'python -m src.main --reconcile' gereklidir."
            )
            return False

        if not task.rights_confirmed:
            error_msg = "Yayın ve kullanım hakları doğrulanmamış (rights_confirmed=False). Telif ve yayın güvenliği gereği reddedildi."
            logger.error(f"GÖREV REDDEDİLDİ: {task.id} - {error_msg}")
            self.queue_manager.mark_failed(task.id, error_message=error_msg, error_category=ErrorCategory.PERMANENT)
            return False

        self._current_task_id = task.id
        self._status = f"processing_{task.id}"
        self.queue_manager.mark_in_progress(task.id)
        task_dir = os.path.join(self.outputs_dir, task.id)
        os.makedirs(task_dir, exist_ok=True)

        logger.info(f"{'='*60}")
        logger.info(f"GÖREV BAŞLATILDI: {task.id} | Başlık: {task.video_title}")
        logger.info(f"Tarih: {task.date} | Model: {task.model_name} | Pitch: {task.pitch_shift} | Gizlilik: {task.privacy_status}")
        logger.info(f"{'='*60}")

        try:
            # 1. Ses İndirme
            logger.info("Adım 1/6: Kaynak ses indiriliyor...")
            raw_audio = self.audio_pipeline.download_audio(
                youtube_url=task.youtube_url,
                task_dir=task_dir,
                output_prefix="step1"
            )

            # 2. Stem Ayrıştırma
            logger.info("Adım 2/6: Demucs ile vokal ve enstrümantal ayrıştırılıyor...")
            vocal_path, instrumental_path = self.audio_pipeline.separate_stems(
                audio_path=raw_audio,
                task_dir=task_dir
            )

            # 3. RVC Vokal Dönüşümü
            logger.info(f"Adım 3/6: RVC v2 ile ses dönüştürülüyor ({task.model_name})...")
            converted_vocal = self.audio_pipeline.convert_voice(
                vocals_path=vocal_path,
                model_name=task.model_name,
                pitch_shift=task.pitch_shift,
                task_dir=task_dir,
                output_prefix="step3"
            )

            # 4. İki Geçişli Miksleme ve Loudnorm
            logger.info("Adım 4/6: Dönüştürülen vokal ve altyapı miksleniyor (EBU R128 İki Geçişli Loudnorm)...")
            mixed_audio = self.audio_pipeline.mix_audio(
                converted_vocal=converted_vocal,
                instrumental=instrumental_path,
                task_dir=task_dir,
                output_prefix=task.id
            )
            audio_info = self.audio_pipeline.get_audio_info(mixed_audio)

            # 5. 1080p Video Render & Doğrulama
            logger.info("Adım 5/6: 1080p MP4 video render ve doğrulama yapılıyor...")
            final_video_path = os.path.join(task_dir, f"{task.id}_final.mp4")
            self.video_generator.create_video(
                audio_path=mixed_audio,
                output_video_path=final_video_path,
                expected_audio_duration=audio_info["duration"]
            )

            # 6. YouTube Yükleme Aşaması
            logger.info(f"Adım 6/6: Durum UPLOADING olarak kilitleniyor ve video YouTube'a yükleniyor (Gizlilik: {task.privacy_status})...")
            self.queue_manager.mark_uploading(task.id)

            video_id = self.youtube_uploader.upload_video(
                video_path=final_video_path,
                title=task.video_title,
                description=task.video_description,
                tags=task.tags,
                privacy_status=task.privacy_status
            )

            # 7. Başarılı Tamamlama
            self.queue_manager.mark_completed(task.id, youtube_video_id=video_id)
            logger.info(f"TÜM İŞLEMLER BAŞARIYLA TAMAMLANDI! Görev: {task.id} -> https://youtu.be/{video_id}")

            if cleanup_temp_files:
                self._cleanup_intermediates(task_dir, keep_files=[mixed_audio, final_video_path])

            return True

        except FileNotFoundError as e:
            err_msg = sanitize_error_message(str(e))
            logger.error(f"Kalıcı hata nedeniyle görev durduruldu ({task.id}): {err_msg}")
            self.queue_manager.mark_failed(task.id, error_message=err_msg, error_category=ErrorCategory.PERMANENT)
            return False

        except Exception as e:
            err_msg = sanitize_error_message(f"{type(e).__name__}: {str(e)}")
            logger.exception(f"Görev sırasında hata meydana geldi ({task.id}): {err_msg}")
            self.queue_manager.mark_failed(task.id, error_message=err_msg, error_category=ErrorCategory.TRANSIENT)
            return False

        finally:
            self._current_task_id = None
            self._status = "idle"

    def _cleanup_intermediates(self, task_dir: str, keep_files: list) -> None:
        try:
            keep_basenames = [os.path.basename(f) for f in keep_files]
            for root, dirs, files in os.walk(task_dir, topdown=False):
                for name in files:
                    if name not in keep_basenames:
                        try:
                            os.remove(os.path.join(root, name))
                        except Exception:
                            pass
                for dir_name in dirs:
                    try:
                        shutil.rmtree(os.path.join(root, dir_name), ignore_errors=True)
                    except Exception:
                        pass
            logger.info(f"Geçici ara dosyalar temizlendi: {task_dir}")
        except Exception as e:
            logger.warning(f"Ara dosya temizliği uyarısı: {e}")

    def process_pending_tasks(self, target_date: Optional[str] = None) -> int:
        if self._is_processing or self._shutdown_requested:
            return 0

        self._is_processing = True
        try:
            recovered, alerts = self.queue_manager.recover_crashed_tasks(timeout_seconds=7200)
            if recovered > 0:
                logger.info(f"Başlangıçta {recovered} adet askıda kalmış görev kurtarıldı.")

            runnable_tasks = self.queue_manager.get_runnable_tasks(
                target_date=target_date,
                max_catchup_days=self.max_catchup_days
            )

            if not runnable_tasks:
                logger.info(f"İşlenecek bekleyen görev bulunamadı (Hedef: {target_date or 'Tümü'}, Max Catch-up: {self.max_catchup_days} gün).")
                return 0

            logger.info(f"Toplam {len(runnable_tasks)} adet işlenebilir görev bulundu (Bu döngüde en fazla {self.max_tasks_per_cycle} adet işlenecek).")
            processed_count = 0

            for task in runnable_tasks[:self.max_tasks_per_cycle]:
                if self._shutdown_requested:
                    logger.info("Kapanma isteği nedeniyle sıradaki görevler bekletiliyor.")
                    break

                success = self.execute_task(task)
                if success:
                    processed_count += 1

            return processed_count
        finally:
            self._is_processing = False

    def start_scheduler_daemon(self, target_time: str = "18:00") -> None:
        logger.info("CoverCraft Zamanlayıcı Servisi Başlatıldı.")
        logger.info(f"Saat Dilimi: {TIMEZONE_STR} | Günlük Çalışma Saati: {target_time} | Max Catchup: {self.max_catchup_days} Gün")

        self._heartbeat_worker.start()
        self._status = "running"

        today = self.get_current_date()
        logger.info(f"Başlangıç taraması yapılıyor (Tarih: {today})...")
        self.process_pending_tasks(target_date=today)

        schedule.every().day.at(target_time).do(
            lambda: self.process_pending_tasks(target_date=self.get_current_date())
        )

        logger.info(f"Zamanlayıcı dinlemede... (Her gün {target_time} - {TIMEZONE_STR})")
        while not self._shutdown_requested:
            schedule.run_pending()
            time.sleep(15)

        self._heartbeat_worker.stop()
        self._status = "stopped"
        logger.info("CoverCraft Zamanlayıcı Servisi güvenle sonlandırıldı.")


def main():
    orchestrator = CoverCraftOrchestrator()
    orchestrator.start_scheduler_daemon()


if __name__ == "__main__":
    main()
