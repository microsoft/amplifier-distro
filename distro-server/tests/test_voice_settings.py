"""Tests for VoiceSettings.assistant_name field and env export."""

import os

from amplifier_distro.distro_settings import (
    DistroSettings,
    VoiceSettings,
    export_to_env,
)


def test_voice_settings_has_assistant_name_field():
    """VoiceSettings().assistant_name defaults to 'Amplifier'."""
    vs = VoiceSettings()
    assert vs.assistant_name == "Amplifier"


def test_assistant_name_exported_to_env(monkeypatch):
    """export_to_env sets AMPLIFIER_VOICE_ASSISTANT_NAME to the default 'Amplifier'."""
    monkeypatch.delenv("AMPLIFIER_VOICE_ASSISTANT_NAME", raising=False)
    settings = DistroSettings()
    exported = export_to_env(settings)
    assert "AMPLIFIER_VOICE_ASSISTANT_NAME" in exported
    assert os.environ["AMPLIFIER_VOICE_ASSISTANT_NAME"] == "Amplifier"


def test_custom_assistant_name_exported(monkeypatch):
    """export_to_env exports a custom assistant name correctly."""
    monkeypatch.delenv("AMPLIFIER_VOICE_ASSISTANT_NAME", raising=False)
    settings = DistroSettings()
    settings.voice.assistant_name = "Jarvis"
    exported = export_to_env(settings)
    assert "AMPLIFIER_VOICE_ASSISTANT_NAME" in exported
    assert os.environ["AMPLIFIER_VOICE_ASSISTANT_NAME"] == "Jarvis"


def test_existing_env_var_not_overwritten(monkeypatch):
    """Pre-set AMPLIFIER_VOICE_ASSISTANT_NAME is not overwritten by export_to_env."""
    monkeypatch.setenv("AMPLIFIER_VOICE_ASSISTANT_NAME", "Cortana")
    settings = DistroSettings()
    export_to_env(settings)
    assert os.environ["AMPLIFIER_VOICE_ASSISTANT_NAME"] == "Cortana"
