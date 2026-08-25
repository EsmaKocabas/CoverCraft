import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from src.models import CoverTask, TaskStatus, ErrorCategory
from src.queue_manager import QueueManager, FileLock


@pytest.fixture
def temp_queue(tmp_path):
    queue_file = tmp_path / "queue.json"
    initial_tasks = [
        {
            "id": "task-1",
            "date": "2026-08-12",
            "youtube_url": "https://www.youtube.com/watch?v=1",
            "model_name": "model_1",
            "video_title": "Song 1",
            "rights_confirmed": True,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3
        },
        {
            "id": "task-2",
            "date": "2026-08-13",
            "youtube_url": "https://www.youtube.com/watch?v=2",
            "model_name": "model_2",
            "video_title": "Song 2",
            "rights_confirmed": True,
            "status": "completed",
            "youtube_video_id": "yt_already_done"
        }
    ]
    queue_file.write_text(json.dumps(initial_tasks, indent=2), encoding="utf-8")
    return str(queue_file)


def test_file_lock_mechanism(tmp_path):
    lock_file = str(tmp_path / "test.lock")
    with FileLock(lock_file, timeout=2.0) as lock1:
        assert os.path.exists(lock_file)
        # Kilit serbest bırakılınca tekrar alınabilmeli
    with FileLock(lock_file, timeout=2.0) as lock2:
        assert os.path.exists(lock_file)


def test_state_machine_full_lifecycle(temp_queue):
    qm = QueueManager(temp_queue)

    # 1. Başlangıçta 1 adet çalıştırılabilir görev olmalı
    runnable = qm.get_runnable_tasks()
    assert len(runnable) == 1
    assert runnable[0].id == "task-1"

    # 2. mark_in_progress
    t1 = qm.mark_in_progress("task-1")
    assert t1.status == TaskStatus.IN_PROGRESS
    assert t1.attempts == 1
    assert t1.started_at is not None

    # IN_PROGRESS iken runnable listesinde görünmemeli
    assert len(qm.get_runnable_tasks()) == 0

    # 3. mark_uploading
    t1_up = qm.mark_uploading("task-1")
    assert t1_up.status == TaskStatus.UPLOADING

    # UPLOADING iken de runnable listesinde görünmemeli
    assert len(qm.get_runnable_tasks()) == 0

    # 4. mark_completed
    t1_done = qm.mark_completed("task-1", youtube_video_id="yt_vid_999")
    assert t1_done.status == TaskStatus.COMPLETED
    assert t1_done.youtube_video_id == "yt_vid_999"
    assert t1_done.processed_at is not None


def test_mark_failed_exponential_backoff(temp_queue):
    qm = QueueManager(temp_queue)
    qm.mark_in_progress("task-1")

    # 1. Transient Hata -> 1. deneme sonrası 900 saniye (15 dk) backoff
    now_before = datetime.now(timezone.utc)
    t1_fail = qm.mark_failed(
        task_id="task-1",
        error_message="Network Timeout",
        error_category=ErrorCategory.TRANSIENT,
        base_backoff_seconds=900
    )
    assert t1_fail.status == TaskStatus.FAILED
    assert t1_fail.error_category == ErrorCategory.TRANSIENT
    assert t1_fail.next_retry_at is not None
    # Yaklaşık 15 dakika sonrasına ayarlanmış olmalı
    diff_secs = (t1_fail.next_retry_at - now_before).total_seconds()
    assert 890 <= diff_secs <= 910

    # Şu an çalıştırılmamalı
    assert len(qm.get_runnable_tasks(now_utc=now_before)) == 0
    # 16 dakika sonra çalıştırılabilir olmalı
    future_time = now_before + timedelta(minutes=16)
    assert len(qm.get_runnable_tasks(now_utc=future_time)) == 1

    # 2. Permanent Hata durumunda retry_at None olmalı
    qm.mark_in_progress("task-1")
    t1_perm = qm.mark_failed(
        task_id="task-1",
        error_message="RVC Model Not Found",
        error_category=ErrorCategory.PERMANENT
    )
    assert t1_perm.status == TaskStatus.FAILED
    assert t1_perm.error_category == ErrorCategory.PERMANENT
    assert t1_perm.next_retry_at is None
    # Gelecekte de çalıştırılmamalı
    assert len(qm.get_runnable_tasks(now_utc=future_time)) == 0


def test_crash_recovery_for_in_progress_and_uploading(temp_queue):
    qm = QueueManager(temp_queue)
    
    # 1. Görev IN_PROGRESS olarak başlatılıp 3 saat önce çökmüş gibi simüle edelim
    tasks = qm.load_tasks()
    t1 = next(t for t in tasks if t.id == "task-1")
    t1.status = TaskStatus.IN_PROGRESS
    t1.started_at = datetime.now(timezone.utc) - timedelta(hours=3)
    qm.save_tasks(tasks)

    recovered, alerts = qm.recover_crashed_tasks(timeout_seconds=7200) # 2 saat timeout
    assert recovered == 1
    assert len(alerts) == 0

    t1_recovered = qm.get_task_by_id("task-1")
    assert t1_recovered.status == TaskStatus.FAILED
    assert t1_recovered.error_category == ErrorCategory.TRANSIENT
    assert "zaman aşımına uğradı" in t1_recovered.last_error

    # 2. Görev UPLOADING durumunda çökmüşse otomatik retry YAPILMAMALI, alert üretmeli
    tasks = qm.load_tasks()
    t1 = next(t for t in tasks if t.id == "task-1")
    t1.status = TaskStatus.UPLOADING
    qm.save_tasks(tasks)

    recovered_up, alerts_up = qm.recover_crashed_tasks(timeout_seconds=7200)
    assert recovered_up == 0
    assert "task-1" in alerts_up

    # Manuel uzlaştırma (reconcile) testi
    reconciled = qm.reconcile_uploading_task("task-1", resolve_as=TaskStatus.COMPLETED, youtube_video_id="yt_reconciled_123")
    assert reconciled.status == TaskStatus.COMPLETED
    assert reconciled.youtube_video_id == "yt_reconciled_123"
