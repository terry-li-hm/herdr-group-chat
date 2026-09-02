import pytest


@pytest.fixture(autouse=True)
def _isolated_group_chat_settings(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Keep every assay away from the operator's real settings.toml."""
    missing = tmp_path_factory.mktemp("settings") / "absent.toml"
    monkeypatch.setenv("HERDR_GROUP_CHAT_SETTINGS", str(missing))
