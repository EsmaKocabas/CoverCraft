import os
import json
import time
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.audio_pipeline import AudioPipeline
from src.video_generator import VideoGenerator
from src.scheduler import HeartbeatWorker
from src.utils import terminate_process_tree


def test_heartbeat_worker_continuous_update(tmp_path):
    heartbeat_file = tmp_path / "heartbeat.json"
    status_data = {"status": "processing_test_task", "active_task": "test-123"}
    
    worker = HeartbeatWorker(
        heartbeat_file=str(heartbeat_file),
        get_status_callback=lambda: status_data,
        interval=0.1
    )
    worker.start()
    time.sleep(0.35)
    worker.stop()

    assert heartbeat_file.exists()
    with open(heartbeat_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["status"] == "processing_test_task"
    assert data["active_task"] == "test-123"
    assert "last_heartbeat" in data


def test_ffprobe_tolerance_validation_logic(tmp_path):
    vg = VideoGenerator()
    dummy_video = tmp_path / "test.mp4"
    dummy_video.write_bytes(b"A" * 15000) # > 10KB

    # 1. Ses ve video süre farkı 1 saniye (tolerans <= 2.0s içinde -> BAŞARILI)
    mock_ffprobe_good = json.dumps({
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "duration": "100.0"},
            {"codec_type": "audio", "codec_name": "aac", "duration": "100.0"}
        ],
        "format": {"duration": "100.0", "size": "15000"}
    })
    with patch.object(vg, "_run_command", return_value=mock_ffprobe_good):
        info = vg.validate_video_file(str(dummy_video), expected_audio_duration=99.0, tolerance_seconds=2.0)
        assert info["duration"] == 100.0

    # 2. Ses ve video süre farkı 5 saniye (tolerans 2.0s aşıldı -> HATA FIRLATMALI)
    with patch.object(vg, "_run_command", return_value=mock_ffprobe_good):
        with pytest.raises(RuntimeError) as exc_info:
            vg.validate_video_file(str(dummy_video), expected_audio_duration=94.0, tolerance_seconds=2.0)
        assert "tolerans" in str(exc_info.value).lower()

    # 3. Yalnızca video akışı var, ses akışı yok -> HATA FIRLATMALI
    mock_ffprobe_no_audio = json.dumps({
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "duration": "100.0"}
        ],
        "format": {"duration": "100.0", "size": "15000"}
    })
    with patch.object(vg, "_run_command", return_value=mock_ffprobe_no_audio):
        with pytest.raises(RuntimeError) as exc_info:
            vg.validate_video_file(str(dummy_video))
        assert "ses akışı" in str(exc_info.value).lower()
