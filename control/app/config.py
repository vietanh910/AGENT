"""Cấu hình tập trung cho Control Node PC1 đọc từ biến môi trường hoặc .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlConfig(BaseSettings):
    """Cấu hình ứng dụng Control Node PC1."""

    model_config = SettingsConfigDict(
        env_file=(".env", "control/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    CONTROL_PUBLIC_URL: str = "http://127.0.0.1:8000"

    DATABASE_URL: str = (
        "mysql+pymysql://ai_user:ai_password_change_me@127.0.0.1:3307/ai_team?charset=utf8mb4"
    )
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_API_KEY: str = "sk-placeholder"
    LLM_BASE_URL: str = "https://api.openai.com/v1"

    LINEAR_API_KEY: str = "lin_api_placeholder"
    LINEAR_TEAM_ID: str = "team_placeholder"
    LINEAR_DRY_RUN: bool = True
    LINEAR_WEBHOOK_SECRET: str = ""

    GITHUB_TOKEN: str = "ghp_placeholder"
    GITHUB_REPOSITORY: str = "vietanh910/AGENT"
    GITHUB_WEBHOOK_SECRET: str = ""

    ADMIN_TOKEN: str = "admin_secret_bearer_token_change_me"
    WORKERS_FILE: str = "control/workers.yaml"

    HEARTBEAT_TIMEOUT_SECONDS: int = 45
    APPROVAL_MODE: bool = True
    MAX_TASKS_PER_RUN: int = 60
    MAX_FIX_LOOPS: int = 3
    MAX_PARALLEL_TASKS: int = 2
    RUN_TIMEOUT_MINUTES: int = 240
    ALLOW_PROD_DEPLOY: bool = False
    MAX_UPLOAD_MB: int = 50
    MAX_CONCURRENT_RUNS: int = 3
    MAX_RUN_RETRIES: int = 15
    LOG_LEVEL: str = "INFO"
    PROJECT_COORDINATOR_ENABLED: bool = True
    PROJECT_POLL_SECONDS: float = 2.0
    PM_AUTO_RECOVERY: bool = True
    PM_RECOVERY_RETRIES: int = 2
    PM_RECOVERY_DELAY_SECONDS: float = 5.0
    PM_RECOVERY_MAX_DELAY_SECONDS: float = 60.0
    PM_STALL_SECONDS: float = 60.0

    # DevOps Staging Configuration
    STAGING_PORT_START: int = 8100
    STAGING_PORT_END: int = 8900
    MAX_STAGING_STACKS: int = 5
    REGISTRY_URL: str = ""
    STAGING_DEPLOY_DIR: str = "deploy/staging"


settings = ControlConfig()
