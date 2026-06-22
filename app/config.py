"""
Centralized configuration. All tunables come from environment variables
(with sane local-dev defaults) so the same image runs in docker-compose,
CI, or a real deployment without code changes.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCI_", extra="ignore")

    # --- Service ---
    service_name: str = "sarvam-code-intel"
    environment: str = "dev"
    log_level: str = "INFO"

    # --- Database ---
    # SQLite for local dev; swap the URL for postgresql+asyncpg://... in prod.
    # Schema is written to be dialect-agnostic (no SQLite-only types).
    database_url: str = "sqlite+aiosqlite:///./data/sci.db"

    # --- Storage paths (all bind-mounted volumes in docker-compose) ---
    data_dir: Path = BASE_DIR / "data"
    repos_dir: Path = data_dir / "repos"              # bare/main repo clones, one per workspace
    worktrees_dir: Path = data_dir / "worktrees"      # per-task git worktrees
    journal_dir: Path = data_dir / "journals"         # per-task JSONL journals

    # --- Auth ---
    # API keys are stored hashed (passlib/bcrypt). Plaintext keys are only ever
    # shown once, at creation time, via the bootstrap script.
    api_key_header: str = "Authorization"

    # --- Rate limiting (per user, simple token bucket) ---
    rate_limit_requests_per_minute: int = 60

    # --- LLM ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-6"

    # --- Default budgets (used if a task omits a field) ---
    default_max_tokens: int = 250_000
    default_max_usd: float = 0.75
    default_max_wall_seconds: int = 600

    # --- Sandbox (Phase 4) ---
    sandbox_image: str = "python:3.11-slim"
    sandbox_mem_limit: str = "512m"
    sandbox_cpu_quota: int = 50_000  # 50% of one CPU (cpu_period defaults to 100_000)
    sandbox_timeout_seconds: int = 120

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.repos_dir, self.worktrees_dir, self.journal_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()