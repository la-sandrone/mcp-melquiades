#!/usr/bin/env python3
"""文件名: tools/scan_paths.py
摘要: **Python 感知**的机器路径扫描器，供 export_to_public.sh 调用。
     与 grep 的关键差别：它先剥掉注释与 docstring，只看**可执行代码**里的字符串。

     为什么必须这样：docstring 里为了说明问题而写的示例路径
     （"Windows 上中文用户名极常见，如 C:\\Users\\<名字>\\…"）不是泄露，
     而 grep 分不出来——too crude 的闸门会被误报淹没，最后被人关掉。

     判据（只看可执行代码里的字符串字面量）：
       · WSL 挂载路径      /mnt/<drive>/…
       · POSIX 家目录      /home/<非占位>/…   （/home/u/ 这类单字符占位放行）
       · Windows 用户路径  C:\\Users\\…  /  /Users/…
       · 其他盘符绝对路径  X:\\…
     放行：含 example / placeholder / <user> / xxx 的占位形式。
依赖: 标准库（ast）
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PATTERNS = [
    re.compile(r"/mnt/[a-z]/"),
    re.compile(r"/home/(?!u/|user/|username/|<)[A-Za-z][\w.-]*/"),
    re.compile(r"[A-Za-z]:\\\\?Users\\\\?"),
    re.compile(r"/Users/[A-Za-z]"),
    re.compile(r"[A-Za-z]:\\\\?[A-Za-z]"),
]
# 明显的占位形式一律放行——级 C 判据：文件名与内容双重佐证
ALLOW = re.compile(r"example|placeholder|<[a-z]+>|xxx|/home/u/|C:\\\\?Users\\\\?<", re.I)


# 逐行放行标注。**故意不做"tests/ 一律放行"** —— 那是撒网式豁免，
# 会让测试目录成为任意泄露的藏身处。这里要求逐行显式标注：可审计、有痕迹。
ALLOW_MARKER = "scan-paths-allow"


def code_strings(path: Path) -> list[tuple[int, str]]:
    """取出**可执行代码里的字符串字面量**，剥掉注释与 docstring。

    用 ast 而不是 grep —— 这正是本工具存在的理由。
    返回 (行号, 值)；带 ALLOW_MARKER 标注的行（或其上一行）已剔除。
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except Exception:
        return []

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    src_lines = src.splitlines()
    marked: set[int] = set()
    for i, ln in enumerate(src_lines, 1):
        if ALLOW_MARKER in ln:
            # 标在本行 → 放行本行；标在上一行 → 放行下一行
            marked.add(i)
            marked.add(i + 1)

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or node.lineno in marked:
                continue
            out.append((node.lineno, node.value))
    return out


def scan(root: Path) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for f in sorted(root.rglob("*")):
        if f.suffix not in (".py", ".sh", ".json", ".toml", ".yml", ".yaml"):
            continue
        if "__pycache__" in f.parts or ".bak" in f.name or ".git" in f.parts:
            continue
        for lineno, s in (code_strings(f) if f.suffix == ".py"
                          else [(i, ln) for i, ln in
                                enumerate(f.read_text(encoding="utf-8",
                                                     errors="replace").splitlines(), 1)
                                if not ln.lstrip().startswith("#")]):
            if ALLOW.search(s):
                continue
            for rx in PATTERNS:
                m = rx.search(s)
                if m:
                    hits.append((str(f.relative_to(root)), lineno, m.group(0)))
                    break
    return hits


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    hits = scan(root)
    if not hits:
        print("  ok    无机器绝对路径（可执行代码内；注释与 docstring 已豁免）")
        return 0
    print("  FAIL  发现机器绝对路径（可执行代码内）：")
    for f, ln, pat in hits[:25]:
        print(f"      {f}:{ln}  匹配 {pat!r}")
    if len(hits) > 25:
        print(f"      …另有 {len(hits) - 25} 处")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
