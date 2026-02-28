from pathlib import Path

from dotenv import load_dotenv


def load_project_env(base_dir: Path | None = None) -> Path:
    project_dir = base_dir or Path(__file__).resolve().parent
    env_path = project_dir / ".env"
    load_dotenv(env_path, override=False)
    return env_path
