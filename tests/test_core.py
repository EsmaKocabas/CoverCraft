import os
import sys
import unittest
import tempfile
import json
from datetime import datetime, timezone, timedelta

# Proje kök dizinini Python path'e ekle
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.models import CoverTask, TaskStatus, ErrorCategory
from src.queue_manager import QueueManager


class TestCoverCraftCore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue_file = os.path.join(self.temp_dir.name, "test_queue.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pydantic_cover_task_validation(self):
        task_data = {
            "id": "2026-08-12-test-song",
            "date": "2026-08-12",
            "youtube_url": "https://www.youtube.com/watch?v=12345",
            "model_name": "test_singer",
            "pitch_shift": 12,
            "video_title": "Test AI Cover",
            "video_description": "Description test",
            "rights_confirmed": True
        }
        task = CoverTask(**task_data)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.attempts, 0)
        self.assertEqual(task.max_attempts, 3)

        # Geçersiz tarih formatı testi
        invalid_data = dict(task_data, date="12-08-2026")
        with self.assertRaises(ValueError):
            CoverTask(**invalid_data)

    def test_queue_manager_state_transitions(self):
        initial_tasks = [
            {
                "id": "task-1",
                "date": "2026-08-12",
                "youtube_url": "https://www.youtube.com/watch?v=1",
                "model_name": "singer_1",
                "video_title": "Title 1",
                "rights_confirmed": True,
                "status": "pending",
                "attempts": 0,
                "max_attempts": 3
            },
            {
                "id": "task-2",
                "date": "2026-08-13",
                "youtube_url": "https://www.youtube.com/watch?v=2",
                "model_name": "singer_2",
                "video_title": "Title 2",
                "rights_confirmed": True,
                "status": "completed",
                "youtube_video_id": "yt_id_2"
            }
        ]
        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump(initial_tasks, f)

        qm = QueueManager(self.queue_file)
        tasks = qm.load_tasks()
        self.assertEqual(len(tasks), 2)

        # 1. Başlatma
        qm.mark_in_progress("task-1")
        t1 = qm.get_task_by_id("task-1")
        self.assertEqual(t1.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(t1.attempts, 1)

        # 2. Hata durumu (base_backoff_seconds=0 ile anında yeniden denenebilir yapalım)
        qm.mark_failed("task-1", "RVC Model Error", error_category=ErrorCategory.TRANSIENT, base_backoff_seconds=0)
        t1 = qm.get_task_by_id("task-1")
        self.assertEqual(t1.status, TaskStatus.FAILED)
        self.assertEqual(t1.last_error, "RVC Model Error")

        # 3. Yeniden denenebilir mi? (backoff süresi 0 olduğu için anında runnable)
        runnable = qm.get_runnable_tasks()
        self.assertEqual(len(runnable), 1)
        self.assertEqual(runnable[0].id, "task-1")

        # 4. Tamamlama
        qm.mark_completed("task-1", youtube_video_id="yt_success_123")
        t1 = qm.get_task_by_id("task-1")
        self.assertEqual(t1.status, TaskStatus.COMPLETED)
        self.assertEqual(t1.youtube_video_id, "yt_success_123")
        self.assertIsNone(t1.last_error)

        # Artık hiç çalıştırılabilir görev kalmamalı
        runnable_after = qm.get_runnable_tasks()
        self.assertEqual(len(runnable_after), 0)


if __name__ == "__main__":
    unittest.main()
