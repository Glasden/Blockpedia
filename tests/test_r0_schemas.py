from pathlib import Path

from tools.validate_r0 import validate_repository


def test_r0_schemas() -> None:
    validate_repository(Path(__file__).resolve().parents[1])
