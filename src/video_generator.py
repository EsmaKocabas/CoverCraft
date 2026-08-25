import os
import sys
import json
import logging
import subprocess
from typing import Dict, Any, Optional

from src.utils import sanitize_error_message, terminate_process_tree

logger = logging.getLogger("CoverCraft.VideoGenerator")

TIMEOUT_FFMPEG = int(os.environ.get("COVERCRAFT_TIMEOUT_FFMPEG", "600"))


class VideoGenerator:
    def __init__(self, default_cover_path: str = "./assets/cover.jpg"):
        self.default_cover_path = os.path.abspath(default_cover_path)
        self._current_process: Optional[subprocess.Popen] = None

    def terminate_current_process(self) -> None:
        """SIGTERM veya kapanma sinyali alındığında çalışan alt süreci ve çocuklarını temizce sonlandırır."""
        if self._current_process:
            terminate_process_tree(self._current_process)
            self._current_process = None

    def _run_command(self, cmd: list, task_name: str, timeout: int = 600) -> str:
        logger.info(f"[{task_name}] Video komutu başlatılıyor (Timeout: {timeout}s): {' '.join(cmd[:4])}...")
        try:
            popen_kwargs: Dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True
            }
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True

            self._current_process = subprocess.Popen(cmd, **popen_kwargs)
            stdout, stderr = self._current_process.communicate(timeout=timeout)
            ret_code = self._current_process.returncode

            if ret_code != 0:
                full_stderr = (stderr or "").strip()
                logger.error(f"[{task_name}] FFmpeg işlemi başarısız (Kod {ret_code})!\nSTDERR: {full_stderr}")
                err_lines = [line.strip() for line in full_stderr.splitlines() if line.strip() and not line.startswith("frame=")]
                raw_summary = " | ".join(err_lines[-3:]) if err_lines else f"Kod: {ret_code}"
                clean_summary = sanitize_error_message(f"[{task_name}] {raw_summary}")
                raise RuntimeError(clean_summary)

            return stdout or ""

        except subprocess.TimeoutExpired as e:
            self.terminate_current_process()
            logger.error(f"[{task_name}] FFmpeg render işlemi zaman aşımına uğradı ({timeout}s)!")
            raise RuntimeError(f"[{task_name}] Video render işlemi zaman aşımına uğradı.") from e
        except FileNotFoundError as e:
            logger.error(f"FFmpeg sistemde bulunamadı: {e}")
            raise RuntimeError("FFmpeg sistemde kurulu değil.") from e
        finally:
            self._current_process = None

    def ensure_cover_exists(self, cover_path: Optional[str] = None) -> str:
        target_path = cover_path or self.default_cover_path
        if not os.path.exists(target_path):
            logger.info(f"Kapak görseli bulunamadı. Otomatik placeholder üretiliyor: {target_path}")
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "color=c=0x181825:s=1920x1080:d=1",
                "-frames:v", "1",
                target_path
            ]
            self._run_command(cmd, "create_placeholder_cover", timeout=30)
        return target_path

    def validate_video_file(
        self,
        video_path: str,
        expected_audio_duration: Optional[float] = None,
        tolerance_seconds: float = 2.0,
        min_size_bytes: int = 10240
    ) -> Dict[str, Any]:
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Üretilen video dosyası bulunamadı: {video_path}")

        file_size = os.path.getsize(video_path)
        if file_size < min_size_bytes:
            raise RuntimeError(f"Video dosyası boyutu beklenenden çok küçük ({file_size} bytes): {video_path}")

        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name,duration:format=duration,size",
            "-of", "json",
            video_path
        ]
        raw_output = self._run_command(cmd, "validate_video_streams", timeout=30)

        try:
            data = json.loads(raw_output)
            streams = data.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)

            if not has_video:
                raise ValueError("Video akışı (video stream) bulunamadı.")
            if not has_audio:
                raise ValueError("Ses akışı (audio stream) bulunamadı.")

            format_info = data.get("format", {})
            duration = float(format_info.get("duration", 0.0))
            if duration <= 0:
                raise ValueError(f"Geçersiz video süresi: {duration}s")

            if expected_audio_duration is not None and expected_audio_duration > 0:
                diff = abs(duration - expected_audio_duration)
                if diff > tolerance_seconds:
                    raise ValueError(
                        f"Video süresi ({duration:.2f}s) ile ses süresi ({expected_audio_duration:.2f}s) "
                        f"arasındaki fark ({diff:.2f}s) tolerans sınırını ({tolerance_seconds}s) aşıyor!"
                    )

            return {
                "duration": duration,
                "size_bytes": file_size,
                "streams_count": len(streams)
            }
        except Exception as e:
            logger.error(f"Video doğrulama hatası ({video_path}): {e}")
            raise RuntimeError(f"Video dosyası bütünlüğü doğrulanamadı: {e}")

    def create_video(
        self,
        audio_path: str,
        output_video_path: str,
        cover_path: Optional[str] = None,
        expected_audio_duration: Optional[float] = None
    ) -> str:
        active_cover = self.ensure_cover_exists(cover_path)
        os.makedirs(os.path.dirname(output_video_path), exist_ok=True)

        logger.info(f"MP4 Video Render Ediliyor -> {output_video_path}")

        video_filter = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1"

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", active_cover,
            "-i", audio_path,
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "medium",
            "-tune", "stillimage",
            "-profile:v", "high",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            output_video_path
        ]

        self._run_command(cmd, "render_video_mp4", timeout=TIMEOUT_FFMPEG)

        info = self.validate_video_file(
            video_path=output_video_path,
            expected_audio_duration=expected_audio_duration
        )
        logger.info(f"Video render ve doğrulama başarılı: {output_video_path} (Süre: {info['duration']:.2f}s, Boyut: {info['size_bytes']} bytes)")
        return output_video_path
