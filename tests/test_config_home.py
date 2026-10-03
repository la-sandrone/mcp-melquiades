"""
文件名: tests/test_config_home.py
摘要: DSH_HOME 解析契约 —— "引真相源、不抄结果"。
      home 名由 <harness>/DSH_VERSION 派生；写死版本号的那一版曾导致收割静默归零。
"""

from __future__ import annotations

import pathlib

from melquiades.config import Config, _derive_home_from_version, resolve_dsh_home


def _make_harness(root: pathlib.Path, version: str = "v0.1.7-rc.2") -> pathlib.Path:
    h = root / "harness"
    (h / ("dsh-domus-" + version.lstrip("v"))).mkdir(parents=True)
    (h / "DSH_VERSION").write_text(f"DSH_VERSION={version}\n", encoding="utf-8")
    return h


def test_derive_home_from_version(tmp_path: pathlib.Path):
    h = _make_harness(tmp_path)
    assert _derive_home_from_version(str(h)) == str(h / "dsh-domus-0.1.7-rc.2")


def test_derive_returns_none_when_version_file_absent(tmp_path: pathlib.Path):
    assert _derive_home_from_version(str(tmp_path)) is None


def test_derive_returns_none_when_target_dir_absent(tmp_path: pathlib.Path):
    # 有 DSH_VERSION 但对应 home 目录不存在 ⇒ 返回 None（**绝不返回不存在的路径**）。
    (tmp_path / "DSH_VERSION").write_text("DSH_VERSION=v9.9.9\n", encoding="utf-8")
    assert _derive_home_from_version(str(tmp_path)) is None


def test_resolve_override_wins(tmp_path: pathlib.Path):
    h = _make_harness(tmp_path)
    assert resolve_dsh_home("/explicit/home", str(h)) == "/explicit/home"


def test_resolve_derives_from_version_before_env(tmp_path: pathlib.Path, monkeypatch):
    h = _make_harness(tmp_path)
    monkeypatch.setenv("DSH_HOME", "/from/env/ignored")
    assert resolve_dsh_home(None, str(h)) == str(h / "dsh-domus-0.1.7-rc.2")


def test_resolve_never_returns_empty(tmp_path: pathlib.Path):
    assert resolve_dsh_home(None, str(tmp_path / "nope")) != ""


def test_sessions_path_uses_derived_home(tmp_path: pathlib.Path):
    h = _make_harness(tmp_path)
    cfg = Config()
    cfg.dsh.harness_dir = str(h)
    assert cfg.dsh_home == str(h / "dsh-domus-0.1.7-rc.2")
    assert cfg.sessions_path == h / "dsh-domus-0.1.7-rc.2" / "sessions"


def test_rerank_tables_rejects_unknown_table():
    import pytest

    from melquiades.config import _validate_rerank_tables

    class _S:
        rerank_tables = {"draftpad": True, "typo_table": True}
        rerank_doc_chars = {"draftpad": 400}

    with pytest.raises(ValueError):
        _validate_rerank_tables(_S())
