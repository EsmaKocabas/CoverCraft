import os
import time
import json
import socket
import logging
from typing import List, Optional
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from src.utils import sanitize_error_message

logger = logging.getLogger("CoverCraft.YouTubeUploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRIABLE_STATUS_CODES = [500, 502, 503, 504, 429]


class YouTubeUploader:
    def __init__(
        self,
        client_secret_path: str = "./config/client_secret.json",
        token_path: str = "./config/token.json"
    ):
        self.client_secret_path = os.path.abspath(client_secret_path)
        self.token_path = os.path.abspath(token_path)
        self._service = None

    def _set_secure_permissions(self, file_path: str) -> None:
        """POSIX sistemlerde token dosyasını yalnızca sahibi okuyabilecek/yazabilecek şekilde (0o600) ayarlar."""
        if os.name != "nt" and os.path.exists(file_path):
            try:
                os.chmod(file_path, 0o600)
            except Exception as e:
                logger.warning(f"Dosya izinleri ayarlanırken uyarı: {e}")

    def _get_service(self, allow_browser: bool = False):
        if self._service is not None:
            return self._service

        creds = None

        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
                logger.info("Kayıtlı YouTube OAuth JSON token'ı başarıyla yüklendi.")
            except Exception as e:
                logger.warning(f"Kayıtlı JSON token okunurken hata oluştu: {e}")

        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("YouTube OAuth token süresi dolmuş, yenileniyor...")
                creds.refresh(Request())
                with open(self.token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(creds.to_json())
                self._set_secure_permissions(self.token_path)
                logger.info("Yenilenen JSON token başarıyla kaydedildi.")
            except Exception as e:
                logger.error(f"Token yenileme başarısız: {e}")
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(self.client_secret_path):
                raise FileNotFoundError(
                    f"Google Cloud Client Secret dosyası bulunamadı: {self.client_secret_path}. "
                    f"Lütfen Google Cloud Console'dan indirdiğiniz 'client_secret.json' dosyasını 'config/' dizinine yerleştirin."
                )

            if not allow_browser:
                raise RuntimeError(
                    f"Geçerli bir YouTube yetki token'ı ({self.token_path}) bulunamadı!\n"
                    f"Docker veya headless ortamda çalıştırmadan önce yerel bilgisayarınızda şu komutu çalıştırarak yetkilendirme yapın:\n"
                    f"  python src/main.py --auth"
                )

            logger.info("Tarayıcı üzerinden YouTube OAuth yetkilendirmesi başlatılıyor...")
            flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            self._set_secure_permissions(self.token_path)
            logger.info(f"Yeni JSON OAuth token'ı başarıyla kaydedildi: {self.token_path}")

        self._service = build("youtube", "v3", credentials=creds)
        return self._service

    def authorize_interactive(self) -> bool:
        try:
            self._get_service(allow_browser=True)
            logger.info("YouTube yetkilendirmesi başarıyla tamamlandı.")
            return True
        except Exception as e:
            logger.error(f"Yetkilendirme sırasında hata oluştu: {e}")
            raise

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "10",
        privacy_status: str = "public",
        max_retries: int = 5
    ) -> str:
        """
        Belirtilen MP4 videosunu YouTube kanalına yükler.
        429 ve 5xx hatalarında üstel gecikmeyle (exponential backoff) yeniden dener.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Yüklenecek video dosyası bulunamadı: {video_path}")

        service = self._get_service(allow_browser=False)

        if tags is None:
            tags = ["AICover", "CoverCraft", "Music"]

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False
            }
        }

        media = MediaFileUpload(
            video_path,
            chunksize=1024 * 1024 * 4,
            resumable=True,
            mimetype="video/mp4"
        )

        logger.info(f"YouTube'a video yükleme başlatılıyor: '{title}' ({os.path.getsize(video_path)} bytes)")
        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )

        response = None
        retry_count = 0

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info(f"YouTube Yükleme İlerlemesi: %{progress}")
                retry_count = 0  # Başarılı chunk sonrası retry sıfırla
            except HttpError as e:
                if e.resp.status in RETRIABLE_STATUS_CODES and retry_count < max_retries:
                    retry_count += 1
                    sleep_secs = 2 ** retry_count
                    logger.warning(f"YouTube API geçici hatası ({e.resp.status}). {sleep_secs}s sonra tekrar deneniyor (Deneme {retry_count}/{max_retries})...")
                    time.sleep(sleep_secs)
                else:
                    clean_err = sanitize_error_message(f"YouTube HTTP Hatası ({e.resp.status}): {e}")
                    raise RuntimeError(clean_err) from e
            except (socket.error, ConnectionError, TimeoutError) as e:
                if retry_count < max_retries:
                    retry_count += 1
                    sleep_secs = 2 ** retry_count
                    logger.warning(f"Ağ bağlantı hatası: {e}. {sleep_secs}s sonra tekrar deneniyor ({retry_count}/{max_retries})...")
                    time.sleep(sleep_secs)
                else:
                    raise RuntimeError(f"YouTube yüklemesi ağ hatası nedeniyle başarısız oldu: {e}") from e

        video_id = response.get("id")
        if not video_id:
            raise RuntimeError(f"YouTube yanıtında video ID bulunamadı: {response}")

        logger.info(f"Video başarıyla yüklendi! YouTube Video ID: {video_id} (URL: https://youtu.be/{video_id})")
        return video_id
