from pathlib import Path

from app import config


def test_poll_interval_defaults_to_10_seconds():
    assert config.POLL_INTERVAL_SECONDS == 10


def test_compose_poll_interval_is_10_seconds():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert 'POLL_INTERVAL_SECONDS: "10"' in compose
