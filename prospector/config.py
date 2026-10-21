import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CACHE_DIR = ROOT / "cache"
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
CLAUDE_MODEL = "claude-opus-5-5"


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


@lru_cache
def zones() -> dict:
    return yaml.safe_load((ROOT / "config" / "zones.yaml").read_text(encoding="utf-8"))["zones"]


@lru_cache
def _categories_file() -> dict:
    return yaml.safe_load((ROOT / "config" / "categories.yaml").read_text(encoding="utf-8"))


def categories() -> dict:
    return _categories_file()["categories"]


def general_keywords() -> list[str]:
    return _categories_file()["general_site_keywords"]


@lru_cache
def scoring() -> dict:
    return yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))
