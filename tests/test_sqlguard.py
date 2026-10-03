"""
文件名: tests/test_sqlguard.py
摘要: SQL 片段门禁（condition AST 白名单）的单元测试（纯逻辑）。
"""

from __future__ import annotations

import pytest

from melquiades.sqlguard import (
    SqlGuardError,
    guard_condition,
    guard_limit,
    guard_select_cols,
    guard_table,
)


def test_guard_table_rejects_non_whitelisted():
    with pytest.raises(SqlGuardError):
        guard_table("draftpad; DROP DATABASE x")
    with pytest.raises(SqlGuardError):
        guard_table("nope")


def test_guard_select_cols_rejects_arbitrary():
    with pytest.raises(SqlGuardError):
        guard_select_cols("id, (SELECT password FROM mysql.user)")


def test_guard_condition_empty_is_allowed():
    assert guard_condition("") == ""
    assert guard_condition("   ") == ""


def test_guard_condition_accepts_simple_comparison():
    expr = "created_at > '2026-01-01'"
    assert guard_condition(expr) == expr


def test_guard_condition_rejects_stacked_statement():
    with pytest.raises(SqlGuardError):
        guard_condition("1=1; DROP TABLE draftpad")


def test_guard_condition_rejects_comment():
    with pytest.raises(SqlGuardError):
        guard_condition("1=1 /* sneaky */")


def test_guard_condition_rejects_subquery():
    with pytest.raises(SqlGuardError):
        guard_condition("id IN (SELECT id FROM draftpad)")


def test_guard_condition_rejects_paren_escape():
    # 提前关闭外层包装括号 ⇒ 让 OR 逃逸 AND 约束。
    with pytest.raises(SqlGuardError):
        guard_condition("1=1) OR (1=1")


def test_guard_condition_allows_literal_percent():
    # 真实学术文本里的 `%` 出现在**字符串字面量**里是合法的，不该被字符黑名单误杀。
    expr = "content LIKE '%置信%'"
    assert guard_condition(expr) == expr


def test_guard_limit_clamps_and_rejects_non_int():
    # guard_limit 的语义是**夹紧**（clamp），不是拒绝 —— 防的是"意外大 limit 拖库"。
    assert guard_limit(5) == 5
    assert guard_limit(1) == 1
    assert guard_limit(10 ** 9) == 500
    assert guard_limit(0) == 1
    with pytest.raises(SqlGuardError):
        guard_limit("not-an-int")
