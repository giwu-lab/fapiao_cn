"""Application configuration.

All values can be overridden via environment variables with prefix ``FC_``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FC_",
        case_sensitive=False,
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    host: str = "0.0.0.0"
    port: int = 8091
    title: str = "fapiao_cn"
    version: str = "1.0.0"

    # pdfplumber does not render PDF to images; keep for future OCR fallback.
    max_file_mb: int = 32

    # Batch extraction limits
    max_batch_files: int = 50          # max files per batch request
    max_batch_total_mb: int = 500      # max total size per batch request
    max_batch_single_mb: int = 10      # max size for a single file in batch

    # Performance
    cache_size: int = 128              # LRU parse-result cache; 0 disables
    batch_workers: int = 4             # /extract/batch thread-pool size

    # Optional 国家税务总局 verification proxy (FC_VERIFY_*)
    verify_enabled: bool = False
    verify_endpoint: str = ""
    verify_api_key: str = ""
    verify_timeout: int = 15


settings = Settings()
