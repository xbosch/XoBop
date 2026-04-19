from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_api_key: str
    owner_id: int
    model: str
    embedding_model: str
    db_file: str
    default_mode: str
    project_root: Path
    artifact_dir: Path
    allowed_write_root: Path
    read_root: Path
    allow_raw_shell: bool
    use_capability_facade_shell: bool
    use_shell_replay_controller: bool
    panic_unlock_code: str
    embedding_fallback: bool
    file_memory_root: Path
    long_memory_file: Path
    daily_memory_dir: Path
    self_mods_file: Path
    backup_dir: Path


def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def load_settings() -> Settings:
    load_dotenv()

    project_root = Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()
    allowed_write_root = Path(os.getenv("ALLOWED_WRITE_ROOT", str(project_root))).resolve()
    file_memory_root = Path(os.getenv("FILE_MEMORY_ROOT", str(allowed_write_root))).resolve()

    return Settings(
        telegram_token=_require_env("TELEGRAM_TOKEN"),
        openai_api_key=_require_env("OPENAI_API_KEY"),
        owner_id=int(_require_env("OWNER_ID")),
        model=os.getenv("MODEL", "gpt-4.1-mini"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        db_file=os.getenv("DB_FILE", "memory.db"),
        default_mode=os.getenv("DEFAULT_MODE", "strict").strip().lower(),
        project_root=project_root,
        artifact_dir=Path(os.getenv("ARTIFACT_DIR", str(project_root / "artifacts"))).resolve(),
        allowed_write_root=allowed_write_root,
        read_root=Path(os.getenv("READ_ROOT", str(allowed_write_root))).resolve(),
        allow_raw_shell=os.getenv("ALLOW_RAW_SHELL", "0").strip() == "1",
        use_capability_facade_shell=os.getenv("USE_CAPABILITY_FACADE_SHELL", "0").strip() == "1",
        use_shell_replay_controller=os.getenv("USE_SHELL_REPLAY_CONTROLLER", "0").strip() == "1",
        panic_unlock_code=os.getenv("PANIC_UNLOCK_CODE", "").strip(),
        embedding_fallback=os.getenv("EMBEDDING_FALLBACK", "1").strip() == "1",
        file_memory_root=file_memory_root,
        long_memory_file=Path(os.getenv("LONG_MEMORY_FILE", str(file_memory_root / "MEMORY.md"))).resolve(),
        daily_memory_dir=Path(os.getenv("DAILY_MEMORY_DIR", str(file_memory_root / "memory"))).resolve(),
        self_mods_file=Path(os.getenv("SELF_MODS_FILE", str(project_root / "config" / "self_mods.json"))).resolve(),
        backup_dir=Path(os.getenv("BACKUP_DIR", str(project_root / "workspace" / "backups"))).resolve(),
    )


settings = load_settings()
