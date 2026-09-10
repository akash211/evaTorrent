import os
import tempfile
import pytest
from pathlib import Path

# Force an isolated temporary directory for tests so tests never touch ~/.evatorrent
_test_data_dir = tempfile.mkdtemp(prefix="evatorrent_test_data_")
os.environ["EVA_DATA_DIR"] = _test_data_dir


@pytest.fixture(autouse=True)
def isolate_test_data_dir(tmp_path, monkeypatch):
    """Ensures each test gets an isolated data directory."""
    test_dir = tmp_path / "eva_isolated"
    test_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EVA_DATA_DIR", str(test_dir))
