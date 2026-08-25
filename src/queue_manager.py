import os
import sys
import json
import time
import shutil
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from src.models import CoverTask, TaskStatus, ErrorCategory
from src.utils import sanitize_error_message

logger = logging.getLogger("CoverCraft.QueueManager")


class FileLock:
    """
    İşletim sistemleri arası güvenli dosya kilitleme yöneticisi (Windows msvcrt / POSIX fcntl).
    Kilit dosyası asla os.replace ile silinmez veya yeniden adlandırılmaz; sabit kalır.
    """
    def __init__(self, lock_file: str, timeout: float = 15.0, delay: float = 0.05):
        self.lock_file = os.path.abspath(lock_file)
        self.timeout = timeout
        self.delay = delay
        self._fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        start_time = time.time()

        # Windows msvcrt kilidi için dosyanın en az 1 byte veri içermesi gerekir
        if not os.path.exists(self.lock_file) or os.path.getsize(self.lock_file) == 0:
            try:
                with open(self.lock_file, "wb") as f:
                    f.write(b"\0")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass

        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fd = os.open(self.lock_file, os.O_RDWR)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    self._fd = os.open(self.lock_file, os.O_RDWR)
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError, PermissionError):
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except Exception:
                        pass
                    self._fd = None

                if time.time() - start_time >= self.timeout:
                    raise TimeoutError(f"Dosya kilidi zaman aşımına uğradı ({self.timeout}s): {self.lock_file}")
                time.sleep(self.delay)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception as e:
                logger.warning(f"Kilit serbest bırakılırken uyarı: {e}")
            finally:
                self._fd = None


class QueueManager:
    def __init__(self, queue_file: str = "./config/queue.json", lock_timeout: float = 15.0):
        self.queue_file = os.path.abspath(queue_file)
        self.lock_file = f"{self.queue_file}.lock"
        self.backup_file = f"{self.queue_file}.bak"
        self.lock_timeout = lock_timeout
        self._ensure_queue_file_exists()

    def _ensure_queue_file_exists(self) -> None:
        if not os.path.exists(self.queue_file):
            os.makedirs(os.path.dirname(self.queue_file), exist_ok=True)
            with open(self.queue_file, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2, ensure_ascii=False)

    def _read_tasks_unlocked(self) -> List[CoverTask]:
        """Kilit altındayken diskten okur. Bozuk/yarım JSON durumunda yedekten kurtarma yapar."""
        if not os.path.exists(self.queue_file):
            return []

        try:
            with open(self.queue_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            tasks = []
            for item in raw_data:
                if "id" not in item:
                    model = item.get("model_name", "unknown")
                    date = item.get("date", "nodate")
                    item["id"] = f"{date}-{model}"
                tasks.append(CoverTask(**item))
            return tasks

        except Exception as e:
            logger.critical(f"Kuyruk dosyası bozulmuş veya okunamıyor ({self.queue_file}): {e}")
            
            # Bozuk dosyayı inceleme için koruma altına al
            corrupt_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            corrupt_dest = f"{self.queue_file}.corrupted.{corrupt_timestamp}"
            try:
                shutil.copy2(self.queue_file, corrupt_dest)
                logger.warning(f"Bozuk dosya yedeklendi: {corrupt_dest}")
            except Exception:
                pass

            # .bak yedeğinden kurtarmayı dene
            if os.path.exists(self.backup_file):
                logger.info(f"Yedek dosyadan ({self.backup_file}) kurtarma deneniyor...")
                try:
                    with open(self.backup_file, "r", encoding="utf-8") as bf:
                        raw_data = json.load(bf)
                    tasks = [CoverTask(**item) for item in raw_data]
                    # Bozuk dosyayı yedek veriyle onar
                    self._write_tasks_unlocked(tasks)
                    logger.info("Kuyruk dosyası yedekten başarıyla onarıldı.")
                    return tasks
                except Exception as be:
                    logger.critical(f"Yedek dosyadan kurtarma da başarısız oldu: {be}")

            return []

    def _write_tasks_unlocked(self, tasks: List[CoverTask]) -> None:
        """
        Kilit altındayken atomik yazma:
        1. .tmp dosyasına yaz
        2. flush() ve os.fsync() ile diske göm
        3. os.replace() ile atomik yer değiştir
        4. POSIX ise üst dizini fsync() et
        5. .bak yedeğini güncelle
        """
        tmp_file = f"{self.queue_file}.tmp"
        data = [task.model_dump(mode="json") for task in tasks]

        os.makedirs(os.path.dirname(self.queue_file), exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_file, self.queue_file)

        # POSIX sistemlerde üst dizin metaverisini fsync et
        if os.name != "nt":
            try:
                dir_fd = os.open(os.path.dirname(self.queue_file), os.O_RDONLY)
                os.fsync(dir_fd)
                os.close(dir_fd)
            except Exception:
                pass

        # Her başarılı yazmada .bak yedeğini güncelle
        try:
            with open(self.backup_file, "w", encoding="utf-8") as bf:
                json.dump(data, bf, indent=2, ensure_ascii=False)
                bf.flush()
                os.fsync(bf.fileno())
        except Exception as e:
            logger.warning(f"Yedek (.bak) yazılırken uyarı: {e}")

    def load_tasks(self) -> List[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            return self._read_tasks_unlocked()

    def save_tasks(self, tasks: List[CoverTask]) -> None:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            self._write_tasks_unlocked(tasks)

    def get_task_by_id(self, task_id: str) -> Optional[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            return next((t for t in tasks if t.id == task_id), None)

    def get_runnable_tasks(
        self,
        target_date: Optional[str] = None,
        max_catchup_days: int = 7,
        now_utc: Optional[datetime] = None
    ) -> List[CoverTask]:
        current_time = now_utc or datetime.now(timezone.utc)

        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            runnable = []

            min_date_str = None
            if target_date:
                try:
                    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
                    min_date_dt = target_dt - timedelta(days=max_catchup_days)
                    min_date_str = min_date_dt.strftime("%Y-%m-%d")
                except ValueError:
                    min_date_str = None

            for task in tasks:
                if task.status in (TaskStatus.COMPLETED, TaskStatus.UPLOADING):
                    continue

                if target_date and task.date > target_date:
                    continue
                if min_date_str and task.date < min_date_str:
                    continue

                if task.status == TaskStatus.PENDING:
                    runnable.append(task)
                elif task.status == TaskStatus.FAILED and task.is_retryable(now_utc=current_time):
                    runnable.append(task)

            return runnable

    def recover_crashed_tasks(self, timeout_seconds: int = 7200) -> Tuple[int, List[str]]:
        now = datetime.now(timezone.utc)
        recovered_count = 0
        uploading_alerts = []

        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            modified = False

            for task in tasks:
                if task.status == TaskStatus.IN_PROGRESS:
                    if task.started_at:
                        started = task.started_at if task.started_at.tzinfo else task.started_at.replace(tzinfo=timezone.utc)
                        elapsed = (now - started).total_seconds()
                        if elapsed > timeout_seconds:
                            task.status = TaskStatus.FAILED
                            task.error_category = ErrorCategory.TRANSIENT
                            task.last_error = f"Görev zaman aşımına uğradı veya işlem çöktü ({int(elapsed)}s sürdü)."
                            task.processed_at = now
                            task.next_retry_at = now + timedelta(minutes=10)
                            modified = True
                            recovered_count += 1
                            logger.warning(f"Askıda kalmış görev kurtarıldı (FAILED -> Transient Retry): {task.id}")

                elif task.status == TaskStatus.UPLOADING:
                    uploading_alerts.append(task.id)
                    logger.error(
                        f"DİKKAT: Görev UPLOADING durumunda askıda kalmış: {task.id}. "
                        f"Mükerrer video yüklemesini önlemek için otomatik yeniden denenmeyecektir. "
                        f"Lütfen YouTube kanalınızı kontrol edip 'python src/main.py --reconcile {task.id}' çalıştırın."
                    )

            if modified:
                self._write_tasks_unlocked(tasks)

        return recovered_count, uploading_alerts

    def mark_in_progress(self, task_id: str) -> Optional[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            target = None
            for task in tasks:
                if task.id == task_id:
                    task.status = TaskStatus.IN_PROGRESS
                    task.started_at = datetime.now(timezone.utc)
                    task.attempts += 1
                    target = task
                    break
            if target:
                self._write_tasks_unlocked(tasks)
                logger.info(f"Görev başlatıldı: {task_id} (Deneme {target.attempts}/{target.max_attempts})")
            return target

    def mark_uploading(self, task_id: str) -> Optional[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            target = None
            for task in tasks:
                if task.id == task_id:
                    task.status = TaskStatus.UPLOADING
                    target = task
                    break
            if target:
                self._write_tasks_unlocked(tasks)
                logger.info(f"Görev yükleme aşamasına geçti: {task_id}")
            return target

    def mark_completed(self, task_id: str, youtube_video_id: str) -> Optional[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            target = None
            for task in tasks:
                if task.id == task_id:
                    task.status = TaskStatus.COMPLETED
                    task.youtube_video_id = youtube_video_id
                    task.processed_at = datetime.now(timezone.utc)
                    task.last_error = None
                    task.next_retry_at = None
                    target = task
                    break
            if target:
                self._write_tasks_unlocked(tasks)
                logger.info(f"Görev başarıyla tamamlandı: {task_id} (YouTube ID: {youtube_video_id})")
            return target

    def mark_failed(
        self,
        task_id: str,
        error_message: str,
        error_category: ErrorCategory = ErrorCategory.TRANSIENT,
        base_backoff_seconds: int = 900
    ) -> Optional[CoverTask]:
        now = datetime.now(timezone.utc)
        clean_err = sanitize_error_message(error_message)

        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            target = None
            for task in tasks:
                if task.id == task_id:
                    task.status = TaskStatus.FAILED
                    task.error_category = error_category
                    task.last_error = clean_err
                    task.processed_at = now

                    if error_category == ErrorCategory.TRANSIENT and task.attempts < task.max_attempts:
                        delay_secs = base_backoff_seconds * (2 ** max(0, task.attempts - 1))
                        task.next_retry_at = now + timedelta(seconds=delay_secs)
                        logger.info(f"Geçici hata: {task_id} {delay_secs}s sonra tekrar denenecek ({task.next_retry_at.isoformat()}).")
                    else:
                        task.next_retry_at = None
                        logger.warning(f"Kalıcı hata veya hak eksikliği: {task_id} tekrar denenmeyecek.")

                    target = task
                    break

            if target:
                self._write_tasks_unlocked(tasks)
                logger.error(f"Görev başarısız kaydedildi: {task_id} [{error_category.value}] - {clean_err}")
            return target

    def reconcile_uploading_task(self, task_id: str, resolve_as: TaskStatus, youtube_video_id: Optional[str] = None) -> Optional[CoverTask]:
        with FileLock(self.lock_file, timeout=self.lock_timeout):
            tasks = self._read_tasks_unlocked()
            target = None
            for task in tasks:
                if task.id == task_id and task.status == TaskStatus.UPLOADING:
                    task.status = resolve_as
                    task.processed_at = datetime.now(timezone.utc)
                    if resolve_as == TaskStatus.COMPLETED and youtube_video_id:
                        task.youtube_video_id = youtube_video_id
                        task.last_error = None
                    elif resolve_as == TaskStatus.FAILED:
                        task.error_category = ErrorCategory.PERMANENT
                        task.last_error = "UPLOADING durumundan manuel olarak FAILED'a çekildi."
                    target = task
                    break
            if target:
                self._write_tasks_unlocked(tasks)
                logger.info(f"UPLOADING görevi uzlaştırıldı: {task_id} -> {resolve_as.value}")
            return target
