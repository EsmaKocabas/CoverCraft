import os
import json
import pytest
from unittest.mock import MagicMock, patch
from src.models import CoverTask, TaskStatus, ErrorCategory
from src.scheduler import CoverCraftOrchestrator


@pytest.fixture
def test_env(tmp_path):
    queue_file = tmp_path / "queue.json"
    models_dir = tmp_path / "models"
    outputs_dir = tmp_path / "outputs"
    assets_dir = tmp_path / "assets"
    
    models_dir.mkdir()
    outputs_dir.mkdir()
    assets_dir.mkdir()
    (assets_dir / "cover.jpg").write_bytes(b"dummy image content")

    return {
        "queue_file": str(queue_file),
        "models_dir": str(models_dir),
        "outputs_dir": str(outputs_dir),
        "assets_dir": str(assets_dir),
        "tmp_path": tmp_path
    }


def test_rights_not_confirmed_rejection(test_env):
    tasks = [
        {
            "id": "unauthorized-song",
            "date": "2026-08-12",
            "youtube_url": "https://www.youtube.com/watch?v=1",
            "model_name": "model_1",
            "video_title": "Unauthorized Cover",
            "rights_confirmed": False, # HAK ONAYLANMAMIŞ
            "status": "pending"
        }
    ]
    with open(test_env["queue_file"], "w", encoding="utf-8") as f:
        json.dump(tasks, f)

    orchestrator = CoverCraftOrchestrator(
        queue_file=test_env["queue_file"],
        models_dir=test_env["models_dir"],
        outputs_dir=test_env["outputs_dir"],
        assets_dir=test_env["assets_dir"]
    )

    unauth_task = orchestrator.queue_manager.get_task_by_id("unauthorized-song")
    result = orchestrator.execute_task(unauth_task)

    assert result is False
    updated_task = orchestrator.queue_manager.get_task_by_id("unauthorized-song")
    assert updated_task.status == TaskStatus.FAILED
    assert updated_task.error_category == ErrorCategory.PERMANENT
    assert "hakları doğrulanmamış" in updated_task.last_error
    assert updated_task.next_retry_at is None


def test_catchup_window_and_cycle_limit(test_env):
    tasks = [
        {
            "id": "task-too-old",
            "date": "2026-08-01",  # 11 gün önce (7 gün sınırını aşıyor)
            "youtube_url": "https://www.youtube.com/watch?v=1",
            "model_name": "model_1",
            "video_title": "Too Old Song",
            "rights_confirmed": True,
            "status": "pending"
        },
        {
            "id": "task-in-window-1",
            "date": "2026-08-10",  # 2 gün önce (pencere içinde)
            "youtube_url": "https://www.youtube.com/watch?v=2",
            "model_name": "model_2",
            "video_title": "Window Song 1",
            "rights_confirmed": True,
            "status": "pending"
        },
        {
            "id": "task-in-window-2",
            "date": "2026-08-11",  # 1 gün önce (pencere içinde)
            "youtube_url": "https://www.youtube.com/watch?v=3",
            "model_name": "model_3",
            "video_title": "Window Song 2",
            "rights_confirmed": True,
            "status": "pending"
        }
    ]
    with open(test_env["queue_file"], "w", encoding="utf-8") as f:
        json.dump(tasks, f)

    orchestrator = CoverCraftOrchestrator(
        queue_file=test_env["queue_file"],
        models_dir=test_env["models_dir"],
        outputs_dir=test_env["outputs_dir"],
        assets_dir=test_env["assets_dir"],
        max_catchup_days=7,
        max_tasks_per_cycle=1
    )

    # 2026-08-12 tarihinde çalıştırdığımızı varsayalım
    runnable = orchestrator.queue_manager.get_runnable_tasks(target_date="2026-08-12", max_catchup_days=7)
    runnable_ids = [t.id for t in runnable]
    
    # Eski görev elenmeli, sadece penceredeki 2 görev gelmeli
    assert "task-too-old" not in runnable_ids
    assert "task-in-window-1" in runnable_ids
    assert "task-in-window-2" in runnable_ids
    assert len(runnable) == 2

    # max_tasks_per_cycle=1 olduğu için bir döngüde yalnızca 1 görev çalıştırılmalı
    with patch.object(orchestrator, "execute_task", return_value=True) as mock_exec:
        processed = orchestrator.process_pending_tasks(target_date="2026-08-12")
        assert processed == 1
        assert mock_exec.call_count == 1
        # İlk işlenen görev task-in-window-1 olmalı
        assert mock_exec.call_args[0][0].id == "task-in-window-1"
