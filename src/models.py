import re
from enum import Enum
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorCategory(str, Enum):
    TRANSIENT = "transient"   # Ağ kesintisi, geçici API kotası, geçici ffmpeg bellek sorunu
    PERMANENT = "permanent"   # Model eksik, URL geçersiz, telif/hak onayı yok, bozuk dosya


# Dizin geçişi (directory traversal ../) saldırılarını engelleyen güvenli isim deseni
SAFE_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class CoverTask(BaseModel):
    id: str = Field(..., description="Benzersiz güvenli görev kimliği (örn. 2026-08-12-baris-manco-ornek)")
    date: str = Field(..., description="YYYY-MM-DD formatında işlem tarihi (Europe/Istanbul bazlı)")
    youtube_url: str = Field(..., description="İndirilecek kaynak YouTube video linki")
    model_name: str = Field(..., description="models/ dizinindeki model adı (.pth ve .index hariç)")
    pitch_shift: int = Field(default=0, description="Yarı ton cinsinden ton kaydırma (-12, 0, 12 vb.)")
    video_title: str = Field(..., description="YouTube video başlığı")
    video_description: str = Field(default="CoverCraft otomasyonu ile üretilmiştir.", description="YouTube video açıklaması")
    tags: List[str] = Field(default_factory=lambda: ["AICover", "CoverCraft", "Music"], description="YouTube etiketleri")
    
    # Yayın ve Hak Güvenliği
    rights_confirmed: bool = Field(default=False, description="Kullanım ve yayın hakkının doğrulandığına dair açık onay")
    synthetic_declaration: bool = Field(default=True, description="İçeriğin yapay zeka tarafından üretildiğine dair beyan")

    # Durum Makinesi ve Zaman Damgaları (Teknik alanlar UTC saklanır)
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Görev işlenme durumu")
    attempts: int = Field(default=0, description="Yapılan deneme sayısı")
    max_attempts: int = Field(default=3, description="Maksimum yeniden deneme hakkı")
    
    started_at: Optional[datetime] = Field(default=None, description="Mevcut denemenin başladığı UTC zamanı")
    processed_at: Optional[datetime] = Field(default=None, description="Tamamlanma veya son hata UTC zamanı")
    next_retry_at: Optional[datetime] = Field(default=None, description="Tekrar denemenin yapılacağı en erken UTC zamanı")
    
    error_category: Optional[ErrorCategory] = Field(default=None, description="Son hatanın kategorisi (transient / permanent)")
    last_error: Optional[str] = Field(default=None, description="Son alınan hatanın temizlenmiş kısa özeti")
    youtube_video_id: Optional[str] = Field(default=None, description="Yüklenen YouTube videosunun ID'si")

    @field_validator("id", "model_name")
    @classmethod
    def validate_safe_identifiers(cls, v: str) -> str:
        if not SAFE_IDENTIFIER_REGEX.match(v):
            raise ValueError(
                f"Değer ('{v}') güvenli karakter deseniyle uyuşmuyor. "
                "Sadece harf, rakam, alt çizgi ve tire kullanılabilir (En fazla 128 karakter, dizin geçişi engellenmiştir)."
            )
        return v

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Tarih YYYY-MM-DD formatında olmalıdır (örn. 2026-08-12).")
        return v

    @field_validator("youtube_url")
    @classmethod
    def validate_youtube_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("Geçerli bir HTTP(S) URL girilmelidir.")
        return v

    def is_retryable(self, now_utc: Optional[datetime] = None) -> bool:
        """Görevin şu an yeniden denenmeye uygun olup olmadığını kontrol eder."""
        if self.status != TaskStatus.FAILED:
            return False
        if self.error_category == ErrorCategory.PERMANENT:
            return False
        if self.attempts >= self.max_attempts:
            return False
        if self.next_retry_at is not None:
            current_time = now_utc or datetime.now(timezone.utc)
            # Timezone aware kontrolü
            if self.next_retry_at.tzinfo is None:
                compare_retry = self.next_retry_at.replace(tzinfo=timezone.utc)
            else:
                compare_retry = self.next_retry_at
            if current_time < compare_retry:
                return False
        return True
