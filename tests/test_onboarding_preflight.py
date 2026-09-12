from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).parents[1] / 'skills/travel-video-pipeline/scripts/check_environment.py'
spec = importlib.util.spec_from_file_location('onboarding_preflight', SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def test_binary_that_exists_but_fails_is_not_available(monkeypatch):
    monkeypatch.setattr(preflight.shutil, 'which', lambda _: '/broken/tool')
    monkeypatch.setattr(preflight.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'missing executable'))
    assert preflight.command_info('uv')['available'] is False


def test_explicit_missing_project_does_not_fall_back_to_current_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(SCRIPT.parents[3])
    assert preflight.discover_project(str(tmp_path / 'missing')) is None


def test_malformed_asset_report_is_not_ready(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(command, 0, '[]', ''))
    result = preflight.apple_asset_info(Path('/worker'), ['ko-KR'])
    assert result['ready'] is False
    assert 'error' in result['locales']['ko-KR']


def test_assets_inspection_never_installs_and_checks_each_locale(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        status = 'installed' if command[-1] == 'ko-KR' else 'supported'
        return subprocess.CompletedProcess(command, 0, json.dumps({'schema_version': 'apple-speech-assets/v1', 'status_after': status}), '')
    monkeypatch.setattr(preflight.subprocess, 'run', run)
    result = preflight.apple_asset_info(Path('/worker'), ['ko-KR', 'en-US'])
    assert result['ready'] is False
    assert len(commands) == 2
    assert all(command[1] == 'assets' and '--install' not in command for command in commands)
    assert result['locales']['en-US']['status_after'] == 'supported'


def test_all_requested_assets_must_be_installed(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(command, 0,
        json.dumps({'schema_version': 'apple-speech-assets/v1', 'status_after': 'installed'}), ''))
    assert preflight.apple_asset_info(Path('/worker'), ['ko-KR', 'en-US'])['ready'] is True
    assert preflight.apple_asset_info(Path('/worker'), [])['ready'] is False


def test_old_worker_or_timeout_cannot_report_ready(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, 'run', lambda command, **kw: subprocess.CompletedProcess(command, 1, '', 'Unknown command: assets'))
    assert preflight.apple_asset_info(Path('/worker'), ['ko-KR'])['ready'] is False
    def timeout(command, **kw):
        raise subprocess.TimeoutExpired(command, 30)
    monkeypatch.setattr(preflight.subprocess, 'run', timeout)
    result = preflight.apple_asset_info(Path('/worker'), ['ko-KR'])
    assert result['ready'] is False
    assert 'error' in result['locales']['ko-KR']
