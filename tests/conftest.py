import os
from pathlib import Path
import sys

import pytest
from pydantic_ai import models

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DEEPSEEK_API_KEY"] = "test-key"
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    import permissions
    import session
    import subagents

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(session, "STORAGE_ROOT", tmp_path / "projects")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(permissions, "state", permissions.PermissionState())
    monkeypatch.setattr(subagents, "PENDING_APPROVALS", [])
    monkeypatch.setattr(subagents, "_TYPES", dict(subagents._TYPES))
