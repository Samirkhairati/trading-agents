"""Where persistent state lives. STATE_DIR (a Docker volume on the server) keeps it identical across deployments."""
import os
from pathlib import Path

def state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR") or Path(__file__).parent / "data")
