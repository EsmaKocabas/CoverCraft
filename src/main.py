import os
import sys
import json
import shutil
import argparse
import logging
from datetime import datetime, timezone, timedelta

# Windows konsol unicode çıktı uyumluluğu
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Proje kök dizinini Python path'e ekle
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.models import TaskStatus
from src.queue_manager import QueueManager
from src.scheduler import CoverCraftOrchestrator
from src.youtube_uploader import YouTubeUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("CoverCraft.CLI")


def run_healthcheck(heartbeat_file: str = "./config/heartbeat.json", max_stale_seconds: int = 90) -> bool:
    """
    Container Healthcheck için canlılık ve heartbeat kontrolü:
    1. Gerekli araçların varlığı
    2. Heartbeat dosyasının tazeliği (<= 90 saniye)
    """
    abs_heartbeat = os.path.abspath(heartbeat_file)
    if not os.path.exists(abs_heartbeat):
        # Servis henüz yeni başlamış olabilir veya scheduler modunda değil
        # Temel dizin ve dosya yazma kontrolü yap
        try:
            test_tmp = "./config/.health_test"
            with open(test_tmp, "w") as f:
                f.write("ok")
            os.remove(test_tmp)
            print("[HEALTHCHECK OK] Dizinler erisilebilir, heartbeat henuz olusturulmamis.")
            return True
        except Exception as e:
            print(f"[HEALTHCHECK FAIL] Dizin erisimi basarisiz: {e}", file=sys.stderr)
            return False

    try:
        with open(abs_heartbeat, "r", encoding="utf-8") as f:
            data = json.load(f)

        last_hb_str = data.get("last_heartbeat")
        if not last_hb_str:
            print("[HEALTHCHECK FAIL] Heartbeat zaman damgasi yok.", file=sys.stderr)
            return False

        last_hb = datetime.fromisoformat(last_hb_str)
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=timezone.utc)

        elapsed = (datetime.now(timezone.utc) - last_hb).total_seconds()
        if elapsed > max_stale_seconds:
            print(f"[HEALTHCHECK FAIL] Heartbeat bayat ({int(elapsed)}s once, limit {max_stale_seconds}s).", file=sys.stderr)
            return False

        print(f"[HEALTHCHECK OK] Scheduler aktif (Son sinyal: {int(elapsed)}s once, Durum: {data.get('status')}).")
        return True

    except Exception as e:
        print(f"[HEALTHCHECK FAIL] Heartbeat okuma hatasi: {e}", file=sys.stderr)
        return False


def run_validation():
    """Sistemin çalışması için gerekli araçları, dizinleri ve kuyruk dosyasını doğrular."""
    print("=" * 60)
    print(" CoverCraft Sistem ve Ortam Doğrulaması")
    print("=" * 60)

    # 1. Komut satırı araçları kontrolü
    tools = ["ffmpeg", "ffprobe", "yt-dlp", "demucs"]
    for tool in tools:
        path = shutil.which(tool)
        status = f"[OK] ({path})" if path else "[EKSIK] (PATH'e ekleyin veya Docker imajında kurun)"
        print(f"* {tool:<12}: {status}")

    # 2. RVC CLI kontrolü
    rvc_path = shutil.which("rvc")
    if rvc_path:
        print(f"* {'rvc':<12}: [OK] ({rvc_path})")
    else:
        print(f"* {'rvc':<12}: [UYARI] CLI bulunamadi (rvc-python paketinin yuklu oldugundan emin olun)")

    # 3. Dizinler ve Dosyalar
    print("\n[Dizin ve Dosya Kontrolleri]")
    checks = [
        ("./models", "RVC Model Klasoru"),
        ("./assets", "Gorsel Varliklar Klasoru"),
        ("./outputs", "Cikti Klasoru"),
        ("./config/queue.json", "Kuyruk Dosyasi"),
        ("./config/client_secret.json", "Google OAuth Client Secret"),
        ("./config/token.json", "Kayitli OAuth JSON Token'i"),
        ("./config/heartbeat.json", "Zamanlayici Canlilik (Heartbeat) Dosyasi")
    ]

    for rel_path, desc in checks:
        abs_p = os.path.abspath(rel_path)
        exists = os.path.exists(abs_p)
        status = "[MEVCUT]" if exists else "[BULUNAMADI]"
        print(f"* {desc:<36} -> {status} ({rel_path})")

    # 4. Kuyruk Doğrulaması
    print("\n[Kuyruk Dosyasi Dogrulamasi]")
    try:
        qm = QueueManager("./config/queue.json")
        tasks = qm.load_tasks()
        print(f"* Toplam Gorev Sayisi: {len(tasks)}")
        for t in tasks:
            rights = "HAK ONAYLI" if t.rights_confirmed else "HAK ONAYSIZ"
            print(f"  - [{t.status.value.upper()}] ID: {t.id} | Tarih: {t.date} | Model: {t.model_name} | {rights} | Baslik: {t.video_title}")
    except Exception as e:
        print(f"[HATA] Kuyruk dogrulamasi basarisiz: {e}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="CoverCraft - Otomatik AI Cover & YouTube Pipeline",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--now", action="store_true", help="Bekleyen veya zamani gecmis gorevleri hemen isler.")
    parser.add_argument("--scheduler", action="store_true", help="Arka plan zamanlayici servisini baslatir.")
    parser.add_argument("--time", type=str, default="18:00", help="Gunluk otomatik calisma saati (varsayilan: 18:00).")
    parser.add_argument("--task-id", type=str, default=None, help="Kuyruktaki belirli tek bir gorevi ID ile calistirir.")
    parser.add_argument("--auth", action="store_true", help="YouTube OAuth yetkilendirmesini yerel tarayicida baslatir ve JSON token kaydeder.")
    parser.add_argument("--validate", action="store_true", help="Sistem bagimliliklarini ve kuyruk durumunu dogrular.")
    parser.add_argument("--healthcheck", action="store_true", help="Docker container icin canlilik ve heartbeat kontrolu yapar.")
    parser.add_argument("--reconcile", type=str, default=None, metavar="TASK_ID", help="Askida kalan UPLOADING gorevini manuel uzlastirir.")
    parser.add_argument("--resolve-as", type=str, choices=["completed", "failed"], default="completed", help="Uzlastirma hedef durumu.")
    parser.add_argument("--video-id", type=str, default=None, help="Uzlastirma sirasinda kaydedilecek YouTube Video ID'si.")

    args = parser.parse_args()
    orchestrator = CoverCraftOrchestrator()

    if args.healthcheck:
        healthy = run_healthcheck()
        sys.exit(0 if healthy else 1)
    elif args.validate:
        run_validation()
    elif args.auth:
        logger.info("YouTube OAuth interaktif yetkilendirmesi baslatiliyor...")
        uploader = YouTubeUploader()
        uploader.authorize_interactive()
    elif args.reconcile:
        resolve_status = TaskStatus.COMPLETED if args.resolve_as == "completed" else TaskStatus.FAILED
        logger.info(f"UPLOADING gorevi uzlastiriliyor: {args.reconcile} -> {resolve_status.value}")
        task = orchestrator.queue_manager.reconcile_uploading_task(
            task_id=args.reconcile,
            resolve_as=resolve_status,
            youtube_video_id=args.video_id
        )
        if task:
            logger.info(f"Gorev basariyla guncellendi: {task.id} (Yeni Durum: {task.status.value})")
        else:
            logger.error(f"Gorev bulunamadi veya durumu UPLOADING degil: {args.reconcile}")
    elif args.task_id:
        task = orchestrator.queue_manager.get_task_by_id(args.task_id)
        if not task:
            logger.error(f"Belirtilen ID ile gorev bulunamadi: {args.task_id}")
            sys.exit(1)
        logger.info(f"Tekil gorev calistiriliyor: {task.id}")
        success = orchestrator.execute_task(task)
        if not success:
            sys.exit(1)
    elif args.now:
        logger.info("Bekleyen gorevler hemen isleniyor...")
        processed = orchestrator.process_pending_tasks()
        logger.info(f"Islem tamamlandi. Toplam {processed} gorev islendi.")
    elif args.scheduler:
        logger.info(f"Zamanlayici baslatiliyor (Saat: {args.time})...")
        orchestrator.start_scheduler_daemon(target_time=args.time)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
