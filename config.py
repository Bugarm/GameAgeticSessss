import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class Config:
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024 + 65536
    SECRET_KEY = os.environ.get("GAMELEARN_SECRET_KEY", "gamelearn-local-development")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'instance' / 'gamelearn.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    GIT_POLL_INTERVAL_SECONDS = 3
