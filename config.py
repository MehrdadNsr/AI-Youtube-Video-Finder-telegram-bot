from pydantic import SecretStr, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application configuration loaded from environment variables."""

    TELEGRAM_BOT_TOKEN: SecretStr = Field(
        ..., description="Telegram Bot Token from BotFather"
    )
    GEMINI_API_KEY: SecretStr = Field(
        ..., description="Google Gemini API Key"
    )
    GROQ_API_KEY: SecretStr = Field(
        ..., description="Groq Cloud API Key for fast Whisper ASR"
    )

    GEMINI_MODEL: str = Field(
        default="gemini-3.8-flash", description="Primary heavy Gemini model name"
    )
    WHISPER_MODEL: str = Field(
        default="whisper-large-v3-turbo", description="Groq Whisper model name"
    )
    AUDIO_CLIP_DURATION_SEC: int = Field(
        default=40,
        ge=15,
        le=90,
        description="Number of seconds from the beginning of the video to extract for ASR",
    )
    CONFIDENCE_THRESHOLD: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score (0.0 to 1.0) to accept a match",
    )

    # Admin & Analytics settings
    ADMIN_ID: int = Field(
        ...,
        description="Telegram numeric user ID of the bot owner for /stats access (required)",
    )
    DEFAULT_LANG: str = Field(
        default="en",
        pattern="^(fa|en)$",
        description="Default UI language ('fa' or 'en')",
    )
    ANALYTICS_DB_PATH: str = Field(
        default="data/analytics.db",
        description="File path for the SQLite usage analytics database",
    )
    ANALYTICS_SALT: SecretStr = Field(
        ...,
        description="Secret salt for HMAC-SHA256 anonymization of user IDs (required)",
    )

    # LLM Router models & budgets
    GEMINI_MODEL_FAST: str = Field(
        default="gemini-3.5-flash-lite",
        description="Primary fast Gemini model for low-latency tasks",
    )
    GEMINI_MODEL_FALLBACKS: str = Field(
        default="gemini-3.1-flash-lite",
        description="Comma-separated list of fast fallback Gemini models",
    )
    GEMINI_MODEL_SLOW_BACKUP: str = Field(
        default="gemini-3.5-flash",
        description="Backup Gemini model with higher timeout when fast models fail",
    )
    LLM_TOTAL_BUDGET_SEC: int = Field(
        default=45,
        ge=10,
        le=180,
        description="Total time budget in seconds for a standard routed LLM request",
    )
    LLM_TIMEOUT_FAST_SEC: float = Field(
        default=8.0,
        ge=2.0,
        le=60.0,
        description="Per-attempt timeout in seconds for fast lite Gemini models on short calls",
    )
    LLM_TIMEOUT_SLOW_BACKUP_SEC: float = Field(
        default=20.0,
        ge=5.0,
        le=90.0,
        description="Per-attempt timeout in seconds for the slow backup Gemini model on short calls",
    )
    LLM_TIMEOUT_BATCH_SEC: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description="Per-attempt timeout in seconds for fast lite Gemini models on subtitle batches",
    )
    LLM_TIMEOUT_BATCH_SLOW_SEC: float = Field(
        default=45.0,
        ge=10.0,
        le=120.0,
        description="Per-attempt timeout in seconds for the slow backup Gemini model on subtitle batches",
    )
    LLM_TIMEOUT_HEAVY_SEC: float = Field(
        default=60.0,
        ge=10.0,
        le=180.0,
        description="Per-attempt timeout in seconds for the primary heavy Gemini model on summaries",
    )
    LLM_TIMEOUT_OPENROUTER_SEC: float = Field(
        default=20.0,
        ge=5.0,
        le=120.0,
        description="Per-attempt timeout in seconds for OpenRouter models on short calls (doubled to 40s for batches)",
    )

    # OpenRouter settings
    OPENROUTER_API_KEY: SecretStr | None = Field(
        default=None,
        description="Optional OpenRouter API key used only when all Gemini models fail",
    )
    OPENROUTER_MODELS: str = Field(
        default="inclusionai/ling-3.2-flash:free,particle-ai/particle-1:free",
        description="Comma-separated list of free OpenRouter fallback models",
    )
    OPENROUTER_USE_FREE_ROUTER: bool = Field(
        default=True,
        description="Whether to append openrouter/free at the end of the OpenRouter chain",
    )

    # YouTube Transcript Proxy, Supadata & Gemini-URL Fallback settings
    YT_PROXY_HTTP: str | None = Field(
        default=None,
        description="Optional HTTP proxy URL for youtube-transcript-api and yt-dlp",
    )
    YT_PROXY_HTTPS: str | None = Field(
        default=None,
        description="Optional HTTPS proxy URL for youtube-transcript-api",
    )
    YT_BLOCK_COOLDOWN_SEC: int = Field(
        default=900,
        ge=60,
        description="Cooldown in seconds to skip direct youtube-transcript-api after an IP block",
    )
    SUPADATA_API_KEY: SecretStr | None = Field(
        default=None,
        description="Optional Supadata API key for native transcript fallback",
    )
    SUPADATA_MONTHLY_LIMIT: int = Field(
        default=90,
        ge=1,
        description="Maximum Supadata native transcript credits to consume per UTC month",
    )
    GEMINI_URL_TRANSCRIBE_ENABLED: bool = Field(
        default=True,
        description="Whether to fall back to Gemini URL video transcription when caption tiers fail",
    )
    GEMINI_URL_DAILY_LIMIT_SEC: int = Field(
        default=25200,
        ge=60,
        description="Maximum processed YouTube video seconds per UTC day via Gemini URL input (default 7h)",
    )
    GEMINI_URL_WINDOW_SEC: int = Field(
        default=300,
        ge=60,
        le=900,
        description="Window length in seconds when splitting long videos for Gemini URL transcription",
    )
    GEMINI_URL_PARALLEL_WINDOWS: int = Field(
        default=4,
        ge=1,
        le=8,
        description="Maximum number of video windows transcribed in parallel via Gemini URL input",
    )

    # Resource Safety & Job Concurrency settings
    WORK_DIR: str = Field(
        default="/tmp/vsf_work",
        description="Base temporary directory for job working files",
    )
    MAX_CONCURRENT_VIDEO_JOBS: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Maximum concurrent yt-dlp download + ffmpeg mux jobs",
    )
    MAX_CONCURRENT_GEMINI_URL_JOBS: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Maximum concurrent Gemini-URL video transcription/matching jobs",
    )
    MAX_CONCURRENT_TRANSLATION_JOBS: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum concurrent subtitle translation/summary jobs",
    )
    QUEUE_MAX_LENGTH: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of waiting jobs in any FIFO resource queue",
    )
    USER_HEAVY_JOBS_PER_HOUR: int = Field(
        default=6,
        ge=1,
        le=100,
        description="Maximum heavy jobs allowed per user per rolling hour (ADMIN_ID exempt)",
    )
    SOFTSUB_MAX_DURATION_SEC: int = Field(
        default=1800,
        ge=60,
        description="Maximum video duration in seconds allowed for softsub video muxing (default 30m)",
    )
    SOFTSUB_MAX_FILESIZE_MB: int = Field(
        default=49,
        ge=5,
        le=2000,
        description="Maximum video file size in MB for Telegram softsub uploads",
    )
    MIN_FREE_DISK_MB: int = Field(
        default=1500,
        ge=100,
        description="Minimum free disk space in MB required to start a download job",
    )

    # Direct Streaming Links Server & Media settings
    LINK_PUBLIC_BASE_URL: str | None = Field(
        default=None,
        description="Public base URL for direct streaming links (empty disables the feature)",
    )
    LINK_BIND_HOST: str = Field(
        default="0.0.0.0",
        description="Bind host for the aiohttp streaming link server",
    )
    LINK_BIND_PORT: int = Field(
        default=8081,
        ge=1,
        le=65535,
        description="Bind port for the aiohttp streaming link server",
    )
    LINK_DIR: str = Field(
        default="/var/tmp/vsf_links",
        description="Directory where direct streaming files are stored (outside WORK_DIR)",
    )
    LINK_TTL_SEC: int = Field(
        default=21600,
        ge=300,
        le=604800,
        description="Expiry time in seconds for generated streaming links (default 6 hours)",
    )
    LINK_MAX_TOTAL_MB: int = Field(
        default=4000,
        ge=100,
        le=500000,
        description="Maximum total disk space in MB for LINK_DIR before evicting oldest files",
    )
    LINK_MAX_FILESIZE_MB: int = Field(
        default=700,
        ge=10,
        le=5000,
        description="Maximum single file size in MB for direct streaming link downloads",
    )
    LINK_MAX_DURATION_SEC: int = Field(
        default=7200,
        ge=60,
        le=43200,
        description="Maximum video duration in seconds allowed for direct streaming links (default 2 hours)",
    )
    LINK_TRANSLATION_TIMEOUT_SEC: int = Field(
        default=240,
        ge=30,
        le=900,
        description="Timeout in seconds for subtitle translation during link_sub generation",
    )
    LINK_SIGNING_SECRET: SecretStr | None = Field(
        default=None,
        description="Optional secret for HMAC signing of streaming tokens; falls back to ANALYTICS_SALT",
    )
    LINK_MAX_CONCURRENT_DOWNLOADS: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Maximum simultaneous HTTP file downloads served by LinkServer",
    )
    LINK_RATE_LIMIT_PER_MIN: int = Field(
        default=60,
        ge=5,
        le=1000,
        description="Maximum HTTP requests per minute per client IP on LinkServer",
    )

    @field_validator("ADMIN_ID", mode="before")
    @classmethod
    def _validate_admin_id(cls, value: object) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                "ADMIN_ID is required in .env (set it to your numeric Telegram user ID)."
            )
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise ValueError(
                "ADMIN_ID must be a valid integer Telegram user ID."
            ) from exc

    @field_validator("ANALYTICS_SALT", mode="before")
    @classmethod
    def _validate_analytics_salt(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                "ANALYTICS_SALT is required in .env for privacy-preserving user hashing."
            )
        return value

    @field_validator(
        "OPENROUTER_API_KEY",
        "SUPADATA_API_KEY",
        "YT_PROXY_HTTP",
        "YT_PROXY_HTTPS",
        "LINK_PUBLIC_BASE_URL",
        "LINK_SIGNING_SECRET",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return stripped
        return value

    @field_validator("LINK_PUBLIC_BASE_URL", mode="after")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.rstrip("/")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
