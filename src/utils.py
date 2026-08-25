import re
import os
import sys
import signal
import logging
import subprocess

logger = logging.getLogger("CoverCraft.Utils")


def sanitize_error_message(msg: str) -> str:
    """
    Hata mesajlarından hassas URL parametrelerini, API anahtarlarını,
    token'ları ve yerel kullanıcı yolu bilgilerini temizler.
    """
    if not msg:
        return ""

    sanitized = str(msg).strip()

    # 1. URL parametrelerindeki token/key/secret bilgilerini maskele
    sanitized = re.sub(
        r"(token|key|secret|auth|password|signature|sig)=([^\s&\"']+)",
        r"\1=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE
    )

    # 2. Bearer / OAuth token desenlerini maskele
    sanitized = re.sub(
        r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}",
        r"\1[REDACTED_TOKEN]",
        sanitized,
        flags=re.IGNORECASE
    )

    # 3. Windows ve Linux yerel kullanıcı dizin yollarını genel etiketle değiştir
    sanitized = re.sub(r"[A-Za-z]:\\[Uu]sers\\[^\\]+", "[USER_HOME]", sanitized)
    sanitized = re.sub(r"/home/[^/]+", "[USER_HOME]", sanitized)

    # 4. Kısalt ve temizle
    if len(sanitized) > 350:
        sanitized = sanitized[:347] + "..."

    return sanitized


def terminate_process_tree(proc: subprocess.Popen, timeout_seconds: float = 5.0) -> None:
    """
    Çalışan alt süreci ve onun oluşturduğu tüm alt süreçleri (process tree / group)
    POSIX'te SIGTERM -> SIGKILL, Windows'ta taskkill ile hiyerarşik olarak temizce sonlandırır.
    """
    if proc is None or proc.poll() is not None:
        return

    pid = proc.pid
    logger.warning(f"Alt süreç ağacı sonlandırılıyor (PID: {pid})...")

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # POSIX Process Group Termination
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.warning(f"Süreç SIGTERM ile durdurulamadı, SIGKILL uygulanıyor (PGID: {pgid})...")
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=2.0)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Süreç grubu sonlandırma uyarısı (PID: {pid}): {e}")
            try:
                proc.kill()
            except Exception:
                pass
