import pytest
from datetime import datetime, timezone, timedelta
from src.models import CoverTask, TaskStatus, ErrorCategory


def test_cover_task_valid_creation():
    task = CoverTask(
        id="2026-08-12-baris-manco-gulpembe",
        date="2026-08-12",
        youtube_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        model_name="baris_manco_v2",
        pitch_shift=0,
        video_title="Barış Manço - Gülpembe AI Cover",
        rights_confirmed=True
    )
    assert task.status == TaskStatus.PENDING
    assert task.rights_confirmed is True
    assert task.attempts == 0
    assert task.max_attempts == 3
    assert task.started_at is None
    assert task.next_retry_at is None


def test_path_traversal_prevention_on_id_and_model():
    # Güvensiz karakterler veya dizin geçişi içeren id ve model_name reddedilmeli
    invalid_ids = [
        "../evil_task",
        "../../etc/passwd",
        "task;rm -rf /",
        "task with spaces",
        "/absolute/path",
        "",
        "a" * 130
    ]
    for bad_id in invalid_ids:
        with pytest.raises(ValueError):
            CoverTask(
                id=bad_id,
                date="2026-08-12",
                youtube_url="https://www.youtube.com/watch?v=test",
                model_name="valid_model",
                video_title="Title"
            )

    invalid_models = [
        "../models/hack",
        "model;inject",
        "model name with space",
        "\\windows\\path"
    ]
    for bad_model in invalid_models:
        with pytest.raises(ValueError):
            CoverTask(
                id="valid-id-123",
                date="2026-08-12",
                youtube_url="https://www.youtube.com/watch?v=test",
                model_name=bad_model,
                video_title="Title"
            )


def test_is_retryable_logic():
    now = datetime.now(timezone.utc)
    task = CoverTask(
        id="test-task",
        date="2026-08-12",
        youtube_url="https://www.youtube.com/watch?v=test",
        model_name="model1",
        video_title="Title",
        status=TaskStatus.FAILED,
        error_category=ErrorCategory.TRANSIENT,
        attempts=1,
        max_attempts=3,
        next_retry_at=now - timedelta(minutes=1)
    )
    # next_retry_at geçmişse ve attempts < max_attempts ise retryable olmalı
    assert task.is_retryable(now_utc=now) is True

    # next_retry_at gelecekteyse henüz çalıştırılmamalı
    task.next_retry_at = now + timedelta(minutes=15)
    assert task.is_retryable(now_utc=now) is False

    # error_category PERMANENT ise asla retryable olmamalı
    task.next_retry_at = now - timedelta(minutes=5)
    task.error_category = ErrorCategory.PERMANENT
    assert task.is_retryable(now_utc=now) is False

    # attempts >= max_attempts ise retryable olmamalı
    task.error_category = ErrorCategory.TRANSIENT
    task.attempts = 3
    assert task.is_retryable(now_utc=now) is False
