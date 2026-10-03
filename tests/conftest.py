"""
文件名: tests/conftest.py
摘要: pytest 公共夹具 —— 把仓库根挂上 sys.path；
      提供「按 JSONL 合成多帧 zstd 会话日志」的构造器（复刻 DSH 真实目录布局）。
依赖: zstandard, pytest

★ 为什么不提交二进制 fixture：
  DSH 会话日志是**个人对话内容**，进仓即泄露。这里改为提交一份**合成**的
  JSONL 事件样本（dsh_session_golden.jsonl），测试时现场把它编成多帧 zstd、
  摆成 DSH 的目录布局。这样做的两个好处：
    ① 零泄露（内容是我们自己编的）；
    ② 仍是**多帧、帧边界对齐行边界**的真实形态 —— 读游标的边界行为真的被测到了。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_JSONL = pathlib.Path(__file__).parent / "fixtures" / "dsh_session_golden.jsonl"

# 固定的合成会话标识（44 字符契约 = "session-" + uuid4 的 36 字符）
SESSION_UUID = "abcdef01-2345-6789-abcd-ef0123456789"
SESSION_ID = "session-" + SESSION_UUID

# fixture 里那条 compaction 的 id（36 字符，满足 v2 的 char(36) 契约）
COMPACTION_ID = "11111111-2222-3333-4444-555555555555"

DSH_VERSION = "v0.1.7-rc.2"
HOME_NAME = "dsh-domus-" + DSH_VERSION.lstrip("v")


def _read_fixture_lines() -> list:
    """读取合成样本的**原始行**（未解析）。"""
    text = FIXTURE_JSONL.read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip()]


def _write_multiframe_zstd(path: pathlib.Path, lines: list) -> int:
    """把每行写成一个**独立 zstd 帧**，返回文件字节数。

    这是 DSH 会话日志的真实形态：多帧 zstd 追加流，且**帧边界对齐行边界**。
    逐行一帧是最严格的形态（读者必须在每个帧边界上都能正确切分）。
    """
    import zstandard as zstd

    cctx = zstd.ZstdCompressor()
    blob = b"".join(cctx.compress((ln + "\n").encode("utf-8")) for ln in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return len(blob)


# ── 夹具 ────────────────────────────────────────────────────────────

@pytest.fixture
def ids() -> dict:
    """合成样本的固定标识，供断言引用（避免在测试里重复字面量）。"""
    return {
        "uuid": SESSION_UUID,
        "session_id": SESSION_ID,
        "compaction_id": COMPACTION_ID,
        "dsh_version": DSH_VERSION,
        "home_name": HOME_NAME,
    }


@pytest.fixture
def fixture_lines() -> list:
    return _read_fixture_lines()


@pytest.fixture
def fixture_events() -> list:
    return [json.loads(ln) for ln in _read_fixture_lines()]


@pytest.fixture
def write_session():
    """返回 (path, lines) → bytes 的合成器。"""
    return _write_multiframe_zstd


@pytest.fixture
def harness(tmp_path: pathlib.Path) -> pathlib.Path:
    """搭一个假的 DSH harness 目录：DSH_VERSION 文件 + <home>/sessions/<会话>。

    返回 harness 根目录。会话目录形如
      <harness>/<HOME_NAME>/sessions/--proj--/session-<uuid>/
    """
    h = tmp_path / "harness"
    (h / HOME_NAME / "sessions" / "--proj--" / f"session-{SESSION_UUID}").mkdir(
        parents=True, exist_ok=True
    )
    (h / "DSH_VERSION").write_text(f"DSH_VERSION={DSH_VERSION}\n", encoding="utf-8")
    return h


@pytest.fixture
def session_dir(harness: pathlib.Path) -> pathlib.Path:
    return harness / HOME_NAME / "sessions" / "--proj--" / f"session-{SESSION_UUID}"
