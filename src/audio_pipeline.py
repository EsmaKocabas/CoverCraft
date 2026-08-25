import os
import sys
import json
import re
import logging
import subprocess
from typing import Tuple, Dict, Any, Optional

from src.utils import sanitize_error_message, terminate_process_tree

logger = logging.getLogger("CoverCraft.AudioPipeline")

TIMEOUT_DOWNLOAD = int(os.environ.get("COVERCRAFT_TIMEOUT_DOWNLOAD", "600"))
TIMEOUT_DEMUCS = int(os.environ.get("COVERCRAFT_TIMEOUT_DEMUCS", "1200"))
TIMEOUT_RVC = int(os.environ.get("COVERCRAFT_TIMEOUT_RVC", "1200"))
TIMEOUT_FFMPEG = int(os.environ.get("COVERCRAFT_TIMEOUT_FFMPEG", "600"))


class AudioPipeline:
    def __init__(self, models_dir: str = "./models", outputs_dir: str = "./outputs"):
        self.models_dir = os.path.abspath(models_dir)
        self.outputs_dir = os.path.abspath(outputs_dir)
        self._current_process: Optional[subprocess.Popen] = None
        os.makedirs(self.models_dir, exist_ok=True)
        os.makedirs(self.outputs_dir, exist_ok=True)

    def terminate_current_process(self) -> None:
        """SIGTERM veya kapanma sinyali alındığında çalışan alt süreci ve çocuklarını temizce sonlandırır."""
        if self._current_process:
            terminate_process_tree(self._current_process)
            self._current_process = None

    def _run_command(self, cmd: list, task_name: str, timeout: int = 600) -> str:
        """Subprocess komutlarını process group izolasyonu ve zaman aşımıyla güvenli çalıştırır."""
        logger.info(f"[{task_name}] Komut başlatılıyor (Timeout: {timeout}s): {' '.join(cmd[:4])}...")
        try:
            # POSIX'te alt süreçleri grup lideri olarak başlat (start_new_session=True)
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
                full_stdout = (stdout or "").strip()
                logger.error(f"[{task_name}] Komut başarısız oldu (Kod {ret_code})!\nSTDERR: {full_stderr}\nSTDOUT: {full_stdout}")
                
                err_lines = [line.strip() for line in full_stderr.splitlines() if line.strip() and not line.startswith("frame=")]
                raw_summary = " | ".join(err_lines[-3:]) if err_lines else f"Komut çıkış kodu: {ret_code}"
                clean_summary = sanitize_error_message(f"[{task_name}] {raw_summary}")
                raise RuntimeError(clean_summary)

            return stdout or ""

        except subprocess.TimeoutExpired as e:
            self.terminate_current_process()
            logger.error(f"[{task_name}] Komut zaman aşımına uğradı ({timeout}s)!")
            raise RuntimeError(f"[{task_name}] İşlem {timeout} saniye içinde tamamlanamadı (Zaman aşımı).") from e
        except FileNotFoundError as e:
            logger.error(f"[{task_name}] Yürütülebilir araç bulunamadı: {cmd[0]}")
            raise RuntimeError(f"Gerekli sistem aracı kurulu değil: {cmd[0]}") from e
        finally:
            self._current_process = None

    def get_audio_info(self, file_path: str) -> Dict[str, Any]:
        """ffprobe ile ses dosyasının süre ve stream geçerliliğini doğrular."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Ses dosyası bulunamadı: {file_path}")

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type,duration:format=duration,size",
            "-of", "json",
            file_path
        ]
        raw_output = self._run_command(cmd, "ffprobe_audio_info", timeout=30)
        try:
            data = json.loads(raw_output)
            streams = data.get("streams", [])
            if not streams or streams[0].get("codec_type") != "audio":
                raise ValueError("Dosyada geçerli bir ses akışı (audio stream) bulunamadı.")
            
            format_info = data.get("format", {})
            duration = float(format_info.get("duration", 0.0))
            if duration <= 0:
                raise ValueError(f"Geçersiz ses süresi: {duration}s")
            return {
                "duration": duration,
                "size_bytes": int(format_info.get("size", os.path.getsize(file_path)))
            }
        except Exception as e:
            logger.error(f"ffprobe ses doğrulama hatası ({file_path}): {e}")
            raise RuntimeError(f"Ses dosyası doğrulanamadı: {e}")

    def download_audio(self, youtube_url: str, task_dir: str, output_prefix: str) -> str:
        output_template = os.path.join(task_dir, f"{output_prefix}_raw.%(ext)s")
        expected_wav = os.path.join(task_dir, f"{output_prefix}_raw.wav")

        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "wav",
            "--audio-quality", "0",
            "--no-playlist",
            "-o", output_template,
            youtube_url
        ]
        
        logger.info(f"YouTube sesi indiriliyor: {youtube_url}")
        self._run_command(cmd, "yt-dlp_download", timeout=TIMEOUT_DOWNLOAD)

        if not os.path.exists(expected_wav) or os.path.getsize(expected_wav) == 0:
            wav_files = [os.path.join(task_dir, f) for f in os.listdir(task_dir) if f.endswith(".wav") and output_prefix in f]
            if wav_files:
                expected_wav = wav_files[0]
            else:
                raise FileNotFoundError(f"İndirilen ses dosyası bulunamadı: {expected_wav}")

        info = self.get_audio_info(expected_wav)
        logger.info(f"Ses başarıyla indirildi: {expected_wav} (Süre: {info['duration']:.2f}s)")
        return expected_wav

    def separate_stems(self, audio_path: str, task_dir: str) -> Tuple[str, str]:
        logger.info(f"Demucs ile stem ayrıştırma başlatılıyor: {audio_path}")
        
        cmd = [
            "demucs",
            "-n", "htdemucs",
            "--two-stems=vocals",
            audio_path,
            "-o", task_dir
        ]
        self._run_command(cmd, "demucs_separate", timeout=TIMEOUT_DEMUCS)

        track_name = os.path.splitext(os.path.basename(audio_path))[0]
        vocals_path = os.path.join(task_dir, "htdemucs", track_name, "vocals.wav")
        no_vocals_path = os.path.join(task_dir, "htdemucs", track_name, "no_vocals.wav")

        if not os.path.exists(vocals_path) or os.path.getsize(vocals_path) == 0:
            raise FileNotFoundError(f"Demucs vokal çıktısı oluşturulamadı: {vocals_path}")
        if not os.path.exists(no_vocals_path) or os.path.getsize(no_vocals_path) == 0:
            raise FileNotFoundError(f"Demucs enstrümantal çıktısı oluşturulamadı: {no_vocals_path}")

        logger.info(f"Stem ayrıştırma tamamlandı: Vokal ({os.path.getsize(vocals_path)} bytes), Enstrüman ({os.path.getsize(no_vocals_path)} bytes)")
        return vocals_path, no_vocals_path

    def convert_voice(self, vocals_path: str, model_name: str, pitch_shift: int, task_dir: str, output_prefix: str) -> str:
        model_path = os.path.join(self.models_dir, f"{model_name}.pth")
        index_path = os.path.join(self.models_dir, f"{model_name}.index")
        converted_vocal = os.path.join(task_dir, f"{output_prefix}_converted_vocal.wav")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"RVC Model dosyası (.pth) bulunamadı: {model_path}. "
                f"Lütfen 'models/' dizinine '{model_name}.pth' dosyasını ekleyin."
            )

        logger.info(f"RVC Ses Dönüşümü Başlatılıyor: Model={model_name}, Pitch={pitch_shift}")

        cmd = [
            "rvc", "infer",
            "--input", vocals_path,
            "--model", model_path,
            "--pitch", str(pitch_shift),
            "--method", "rmvpe",
            "--output", converted_vocal
        ]

        if os.path.exists(index_path):
            cmd.extend(["--index", index_path])
        else:
            logger.warning(f"RVC index dosyası bulunamadı ({index_path}), index olmadan çalıştırılıyor.")

        self._run_command(cmd, "rvc_infer", timeout=TIMEOUT_RVC)

        if not os.path.exists(converted_vocal) or os.path.getsize(converted_vocal) == 0:
            raise RuntimeError(f"RVC ses dönüşüm dosyası üretilemedi veya boş: {converted_vocal}")

        logger.info(f"RVC ses dönüşümü başarıyla tamamlandı: {converted_vocal}")
        return converted_vocal

    def mix_audio(self, converted_vocal: str, instrumental: str, task_dir: str, output_prefix: str) -> str:
        pre_mix_wav = os.path.join(task_dir, f"{output_prefix}_premix.wav")
        final_mp3 = os.path.join(task_dir, f"{output_prefix}_mixed.mp3")

        logger.info(f"Ses miksleme başlatılıyor (Adım 1: Ham Miks Birleştirme) -> {pre_mix_wav}")

        raw_mix_cmd = [
            "ffmpeg", "-y",
            "-i", converted_vocal,
            "-i", instrumental,
            "-filter_complex", "[0:a]volume=1.05[v];[1:a]volume=0.90[i];[v][i]amix=inputs=2:duration=longest:normalize=0[aout]",
            "-map", "[aout]",
            pre_mix_wav
        ]
        self._run_command(raw_mix_cmd, "ffmpeg_raw_mix", timeout=TIMEOUT_FFMPEG)

        # Pass 1 (Ölçüm)
        logger.info("EBU R128 Loudnorm Ölçümü yapılıyor (Pass 1)...")
        measure_cmd = [
            "ffmpeg", "-y",
            "-i", pre_mix_wav,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f", "null",
            "-"
        ]
        
        loudnorm_params = None
        try:
            res_proc = subprocess.run(measure_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            json_match = re.search(r"\{[\s\S]*?\"input_i\"[\s\S]*?\}", res_proc.stderr)
            if json_match:
                loudnorm_params = json.loads(json_match.group(0))
                logger.info(f"Loudnorm ölçüm değerleri: input_i={loudnorm_params.get('input_i')}, input_tp={loudnorm_params.get('input_tp')}")
        except Exception as e:
            logger.warning(f"Loudnorm Pass 1 ölçümünde uyarı, tek geçişe dönülüyor: {e}")

        # Pass 2 (Uygulama)
        if loudnorm_params and "input_i" in loudnorm_params:
            norm_filter = (
                f"loudnorm=I=-16:TP=-1.5:LRA=11:"
                f"measured_I={loudnorm_params.get('input_i')}:"
                f"measured_TP={loudnorm_params.get('input_tp')}:"
                f"measured_LRA={loudnorm_params.get('input_lra')}:"
                f"measured_thresh={loudnorm_params.get('input_thresh')}:"
                f"offset={loudnorm_params.get('target_offset')}:linear=true"
            )
        else:
            norm_filter = "loudnorm=I=-16:TP=-1.5:LRA=11"

        logger.info(f"Miks normalizasyonu uygulanıyor (Pass 2) -> {final_mp3}")
        final_cmd = [
            "ffmpeg", "-y",
            "-i", pre_mix_wav,
            "-af", norm_filter,
            "-b:a", "320k",
            final_mp3
        ]
        self._run_command(final_cmd, "ffmpeg_loudnorm_pass2", timeout=TIMEOUT_FFMPEG)

        if os.path.exists(pre_mix_wav):
            try:
                os.remove(pre_mix_wav)
            except Exception:
                pass

        info = self.get_audio_info(final_mp3)
        logger.info(f"Miks ses hazır: {final_mp3} (Süre: {info['duration']:.2f}s, Boyut: {info['size_bytes']} bytes)")
        return final_mp3
