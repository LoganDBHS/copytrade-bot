from pathlib import Path

import pytest

from settings import Config, load_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(ROOT / "config.example.yaml")


@pytest.fixture(scope="session")
def bravo(cfg):
    return cfg.alerters["bravo"]
