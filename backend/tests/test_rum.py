"""Cấu hình Datadog RUM trả cho web qua /api/meta."""
import dataclasses

from app import api


def test_rum_off_without_keys(monkeypatch):
    monkeypatch.setattr(api, "settings", dataclasses.replace(api.settings, rum_application_id="", rum_client_token=""))
    assert api.rum_config() is None


def test_rum_config_when_set(monkeypatch):
    s = dataclasses.replace(api.settings, rum_application_id="app-1", rum_client_token="pub-x",
                            dd_site="US5.datadoghq.com ", rum_session_sample=150, rum_replay_sample=20)
    monkeypatch.setattr(api, "settings", s)
    cfg = api.rum_config()
    assert cfg["applicationId"] == "app-1" and cfg["site"] == "us5.datadoghq.com"
    assert cfg["service"] == "agriguard-web" and cfg["sessionSampleRate"] == 100 and cfg["sessionReplaySampleRate"] == 20
