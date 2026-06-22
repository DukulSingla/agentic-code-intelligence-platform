"""
Centralized configuration. All tunables come from environment variables
(with sane local-dev defaults) so the same image runs in docker-compose,
CI, or a real deployment without code changes.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to the project root (two levels up from this file), not the
# process's current working directory, so `uvicorn app.main:app` works
# identically whether it's launched from this directory or anywhere else --
# and so every path below (including database_url) stays consistent with
# every other one, rather than some being CWD-relative and others not.
BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCI_", extra="ignore")

    # --- Service ---
    service_name: str = "sarvam-code-intel"
    environment: str = "dev"
    log_level: str = "INFO"

    # --- Database ---
    # SQLite for local dev; schema is written dialect-agnostic so swapping
    # the URL for postgresql+asyncpg://... later needs no code changes.
    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DATA_DIR / 'sci.db'}"

    # --- Storage paths (bind-mounted volumes in docker-compose; see the
    # BASE_DIR comment above for why these all derive from one anchor) ---
    data_dir: Path = _DEFAULT_DATA_DIR
    repos_dir: Path = _DEFAULT_DATA_DIR / "repos"          # bare/main repo clones, one per workspace
    worktrees_dir: Path = _DEFAULT_DATA_DIR / "worktrees"  # per-task git worktrees
    journal_dir: Path = _DEFAULT_DATA_DIR / "journals"     # per-task JSONL journals

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
