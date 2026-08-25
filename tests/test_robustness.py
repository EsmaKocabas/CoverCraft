import os
import json
import time
import pytest
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

from src.models import CoverTask, TaskStatus, ErrorCategory
from src.queue_manager import QueueManager, FileLock
from src.utils import sanitize_error_message
from src.main import run_healthcheck


def test_sensitive_error_sanitization():
    # 1. URL parametrelerinde gizli token/key
    raw_error = "Failed connecting to https://api.youtube.com/upload?token=SECRET_TOKEN_12345&sig=ABCDEF"
    sanitized = sanitize_error_message(raw_error)
    assert "SECRET_TOKEN_12345" not in sanitized
    assert "token=[REDACTED]" in sanitized
    assert "sig=[REDACTED]" in sanitized

    # 2. Bearer token
    bearer_error = "AuthError: Invalid Bearer ya29.a0AfH6SMD_very_long_secret_token_here provided"
    sanitized_bearer = sanitize_error_message(bearer_error)
    assert "very_long_secret_token" not in sanitized_bearer
    assert "[REDACTED_TOKEN]" in sanitized_bearer

    # 3. Yerel kullanıcı yolu
    path_error = r"FileNotFound: C:\Users\Esma\Desktop\CoverCraft\models\model.pth not found"
    sanitized_path = sanitize_error_message(path_error)
    assert "Esma" not in sanitized_path
    assert "[USER_HOME]" in sanitized_path


def test_corrupted_queue_json_recovery(tmp_path):
    queue_file = tmp_path / "queue.json"
    backup_file = tmp_path / "queue.json.bak"
    
    # 1. Geçerli başlangıç verisi yaz ve kaydet
    initial_tasks = [
        CoverTask(
            id="task-valid-1",
            date="2026-08-12",
            youtube_url="https://www.youtube.com/watch?v=1",
            model_name="model1",
            video_title="Song 1",
            rights_confirmed=True
        )
    ]
    qm = QueueManager(str(queue_file))
    qm.save_tasks(initial_tasks)
    assert backup_file.exists()

    # 2. queue.json dosyasını bozalım (yarım / geçersiz JSON yazalım)
    queue_file.write_text("{ broken json content ... ", encoding="utf-8")

    # 3. QueueManager okumaya çalıştığında .bak yedeğinden kurtarmalı
    recovered_tasks = qm.load_tasks()
    assert len(recovered_tasks) == 1
    assert recovered_tasks[0].id == "task-valid-1"

    # 4. Bozuk dosyanın .corrupted olarak arşivlendiğini doğrula
    corrupted_files = list(tmp_path.glob("queue.json.corrupted.*"))
    assert len(corrupted_files) >= 1


def test_concurrent_queue_locking_stress(tmp_path):
    queue_file = tmp_path / "concurrent_queue.json"
    qm = QueueManager(str(queue_file))

    # 10 adet başlangıç görevi ekle
    initial = [
        CoverTask(
            id=f"concurrent-task-{i}",
            date="2026-08-12",
            youtube_url="https://www.youtube.com/watch?v=1",
            model_name="model1",
            video_title=f"Song {i}",
            rights_confirmed=True
        )
        for i in range(10)
    ]
    qm.save_tasks(initial)

    # 10 ayrı thread aynı anda farklı görevleri in_progress ve completed yapsın
    def worker(task_idx):
        thread_qm = QueueManager(str(queue_file))
        task_id = f"concurrent-task-{task_idx}"
        thread_qm.mark_in_progress(task_id)
        time.sleep(0.01)
        thread_qm.mark_completed(task_id, youtube_video_id=f"yt_{task_idx}")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        for f in futures:
            f.result()

    # Tüm 10 görevin COMPLETED olduğu ve veri kaybı yaşanmadığı doğrulanmalı
    final_tasks = qm.load_tasks()
    assert len(final_tasks) == 10
    for t in final_tasks:
        assert t.status == TaskStatus.COMPLETED
        assert t.youtube_video_id.startswith("yt_")


def test_heartbeat_and_healthcheck(tmp_path):
    heartbeat_file = tmp_path / "heartbeat.json"

    # 1. Taze heartbeat ile kontrol
    data = {
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "status": "healthy",
        "pid": 1234
    }
    heartbeat_file.write_text(json.dumps(data), encoding="utf-8")
    assert run_healthcheck(str(heartbeat_file), max_stale_seconds=90) is True

    # 2. Bayat heartbeat (120 saniye önce) ile kontrol
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    stale_data = {
        "last_heartbeat": stale_time.isoformat(),
        "status": "running",
        "pid": 1234
    }
    heartbeat_file.write_text(json.dumps(stale_data), encoding="utf-8")
    assert run_healthcheck(str(heartbeat_file), max_stale_seconds=90) is False
