"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Software Mentor"
    app_env: str = "development"
    log_level: str = "INFO"

    # AI provider selection: "mock" | "watsonx"
    ai_provider: str = "mock"

    # IBM watsonx.ai credentials (required when ai_provider="watsonx")
    watsonx_api_key: str = ""
    watsonx_project_id: str = ""
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"

    # Session storage path for cloned repositories
    session_storage_path: str = "./sessions"

    # Phase 6: SQLite file backing workflow runs and workflow events
    database_path: str = "./mentor_workflow.db"

    # Phase 6: SSE stream tuning (seconds)
    sse_poll_interval_seconds: float = 0.2
    sse_heartbeat_seconds: float = 15.0
    sse_timeout_seconds: float = 60.0

    # Phase 11: bounded, controlled execution of a repository's existing tests.
    #
    # These tune *how much* the controlled runner is allowed to do; they cannot
    # widen what it is allowed to run, which is a constant command in
    # ``app.services.test_executor``.  A zero or negative timeout makes the
    # executor refuse to start rather than run unbounded.
    verification_timeout_seconds: float = 120.0
    #: Ceiling on how many test files one invocation may name.  Bounds the
    #: argument vector and, with the timeout, the wall clock.
    verification_max_targets: int = 10


settings = Settings()
