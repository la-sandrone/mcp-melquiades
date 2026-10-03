#!/usr/bin/env bash
# 文件名: tools/export_to_public.sh
# 摘要: 白名单导出 + 泄露闸门 + 可选脱敏。从自用工作目录导出到公开发布目录。
#       **不是 cp -r**：白名单之外的东西永远不会被带出去，
#       而且每次导出都是一次泄露检测（闸门命中即拒绝，不静默放行）。
#
# 与 DSH 上游有关的一处特别提示：
#   本项目会随 DSH 升级而改。改完代码 **先跑 `pytest`**，再跑本脚本导出。
#   本脚本的闸门只保证"发布件里没有凭据/个人路径"，不保证"代码仍适配新版 DSH"
#   —— 后者由 tests/ 的黄金样本负责。
#
# 依赖: 标准库 + 可选 gitleaks（未装则走降级 grep）

set -uo pipefail

# WARNING: 本脚本**自己也在导出版里**，所以它不能含任何机器相关路径——
#          否则就把作者机器的目录布局一起发布出去了。
#          SRC 默认取脚本自身所在仓库根；DST 必须显式给，不给就报错。
SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DST="${2:-${MELQUIADES_PUBLIC_DST:-}}"

if [ -z "$DST" ]; then
  printf '%s\n' "用法: $0 [源目录] <导出版目录>"
  printf '%s\n' "  或设 MELQUIADES_PUBLIC_DST=<导出版目录>"
  printf '%s\n' "  （导出版目录**故意不给默认值**——避免把本机布局写进发布件）"
  exit 2
fi

# ── 白名单：只有这些进得去 ────────────────────────────────────────────
# 根文件。`.gitattributes` 必须在这里，否则每次导出都会把它漏掉——
# 而它恰恰是防"clone 后 .sh 变 CRLF"的那道保险，漏了等于没有。
ALLOW_FILES=(
  README.md LICENSE .gitignore .gitattributes pyproject.toml
  config.example.yaml
  DESIGN.md IMPLEMENTATION.md NOTES.md
)
# 整目录拷贝。
ALLOW_DIRS=(
  melquiades tests
)
# tools/ 只挑**通用**件。一次性的定位/回放/修正脚本（replay/**、fix_archive_v*、
# apply_prompt_v6 等）硬编码了个人语料路径，一律不出。
ALLOW_TOOLS=(
  tools/export_to_public.sh
  tools/scan_paths.py
  tools/backfill_compaction_v2.py
  tools/verify_archive_v2.py
)

# 绝不能进发布件的（命中即 FAIL）——凭据与本地运行时配置。
FORBIDDEN_FILES=(
  config.yaml
  .env
)

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
fail=0
note() { printf '%s\n' "$*"; }
ok()   { printf "%s  ok  %s%s\n" "$GRN" "$*" "$RST"; }
warn() { printf "%s warn %s%s\n" "$YEL" "$*" "$RST"; }
bad()  { printf "%s FAIL %s%s\n" "$RED" "$*" "$RST"; fail=1; }

# ── 0. 前置 ───────────────────────────────────────────────────────────
[ -d "$SRC" ] || { bad "源目录不存在: $SRC"; exit 1; }
mkdir -p "$DST"
note "src: $SRC"
note "dst: $DST"
note ""

# ── 1. 白名单拷贝 ─────────────────────────────────────────────────────
note "[1] 白名单导出"
rm -rf "${DST:?}"/* 2>/dev/null || true
for f in "${ALLOW_FILES[@]}"; do
  if [ -f "$SRC/$f" ]; then cp -p "$SRC/$f" "$DST/$f"; ok "$f"; else warn "缺 $f"; fi
done
for d in "${ALLOW_DIRS[@]}"; do
  if [ -d "$SRC/$d" ]; then
    # 排除规则**只此一处**。用 tar 管道而不是 rsync：rsync 未必装，
    # 而"降级路径放松了主路径的保证"不是降级、是漏洞。tar 是标准件，一处排除表。
    if tar -C "$SRC" -cf - \
         --exclude='__pycache__' --exclude='*.pyc' --exclude='*.bak*' \
         --exclude='.env' --exclude='.cache' --exclude='*.log' --exclude='.DS_Store' \
         --exclude='_generated' \
         "$d" | tar -C "$DST" -xf -; then
      ok "$d/"
    else
      bad "导出 $d/ 失败（tar 管道退出码非 0）"
    fi
  else
    warn "缺 $d/"
  fi
done
for f in "${ALLOW_TOOLS[@]}"; do
  if [ -f "$SRC/$f" ]; then mkdir -p "$DST/$(dirname "$f")"; cp -p "$SRC/$f" "$DST/$f"; ok "$f"
  else warn "缺 $f"; fi
done
note ""

# ── 2. 脱敏（可选，作用于导出目录）───────────────────────────────────
# 规则文件 `.publish-redactions`（**不发布**）是 sed -E 脚本，一行一条。
# 只对文本文件生效；二进制一律跳过。
#
# 为什么脱敏**只作用于导出目录**：主仓保留真实路径，开发/调试不受影响；
# 发布件才需要占位符。这样两边都不将就。
# ★ 脱敏必须**覆盖**闸门（redaction ⊇ gate）：闸门在第 [3] 段扫的正是脱敏后的
#   导出目录——若规则漏了，闸门会当场抓出来。这就是"脱敏是否做全"的自检。
note "[2] 脱敏（.publish-redactions）"
REDACT_FILE="$SRC/.publish-redactions"
if [ -f "$REDACT_FILE" ]; then
  n_applied=0
  while IFS= read -r f; do
    sed -i -E -f "$REDACT_FILE" "$f" && n_applied=$((n_applied + 1))
  done < <(find "$DST" -name '.git' -prune -o -type f \
             \( -name '*.md' -o -name '*.py' -o -name '*.sh' -o -name '*.yaml' \
                -o -name '*.yml' -o -name '*.toml' -o -name '*.txt' -o -name '*.json' \) \
             -print)
  ok "脱敏已作用于 $n_applied 个文本文件（规则 $(grep -cvE '^\s*(#|$)' "$REDACT_FILE" || echo 0) 条）"
else
  warn "无 .publish-redactions ⇒ 跳过脱敏（若文档里有个人路径，闸门会抓）"
fi
note ""

# ── 3. 闸门：凭据 ────────────────────────────────────────────────────
# 扫描目标是 **$DST（导出件）**，不是 $SRC —— 两条路（gitleaks / grep）必须一致。
#   为什么不扫 $SRC：源树里**必然**有 config.yaml（它就该待在那儿）。拿 gitleaks
#   扫源树 ⇒ 每次都在真实口令上报 ⇒ 闸门**永远无法通过** ⇒ 最后被人关掉。
#   发布件的定义就是 $DST；扫它才等价于"将要发布的东西里没有凭据"。
# **所有扫 $DST 的闸门一律排除 .git**：.git 不是发布内容，但它的内容随本地操作
#   变化（remote/credential/reflog），不排除则同一棵树换个 remote 就能从"过"变"不过"。
note "[3] 凭据闸门"

# 泄露报告里**绝不出现密钥明文**——只留前 6 字符。
redact() { sed -E 's/(AKIA[0-9A-Z]{4})[0-9A-Z]{12}/\1…REDACTED/g;
                   s/(ghp_[A-Za-z0-9]{4})[A-Za-z0-9]{16,}/\1…REDACTED/g;
                   s/(github_pat_[A-Za-z0-9]{4})[A-Za-z0-9_]{18,}/\1…REDACTED/g;
                   s/(sk-[A-Za-z0-9]{4})[A-Za-z0-9]{16,}/\1…REDACTED/g;
                   s/(xox[bap]-[A-Za-z0-9-]{4})[A-Za-z0-9-]{6,}/\1…REDACTED/g;'; }

PAT='AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,}|sk-[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|AZURE_STORAGE_[A-Za-z0-9]+=|-----BEGIN (RSA|OPENSSH|EC|PGP) '

if command -v gitleaks >/dev/null 2>&1; then
  if gitleaks detect --source "$DST" --no-git \
        --report-format json --report-path /tmp/.melq-gitleaks.json --redact -v 2>/tmp/.melq-gl.err; then
    ok "  凭据扫描（gitleaks --no-git 快照，扫描导出件）"
  else
    if grep -q "leaks found" /tmp/.melq-gl.err 2>/dev/null; then
      bad "  凭据扫描发现泄露——见 /tmp/.melq-gitleaks.json（含打码值）"
    else
      warn "  gitleaks 退出码非 0 但 stderr 无 'leaks found' ⇒ 可能是配置错误，请人工确认"
      sed -n '1,5p' /tmp/.melq-gl.err | sed 's/^/      /'
    fi
  fi
  rm -f /tmp/.melq-gitleaks.json /tmp/.melq-gl.err
else
  warn "  gitleaks 未安装 → 降级 grep（**只覆盖当前树，覆盖不了已删除的历史**）"
fi

HITS=$(grep -rInE --exclude-dir=.git "$PAT" "$DST" 2>/dev/null | redact || true)
if [ -n "$HITS" ]; then
  bad "  凭据模式命中："
  printf '%s\n' "$HITS" | sed 's/^/      /'
else
  ok "  凭据模式扫描（当前树）"
fi

# ── 已知机密闸门 ─────────────────────────────────────────────────────
# `.publish-secrets`（**不发布**）一行一条**字面**机密，逐条在导出件里 grep -F。
# 为什么单独一道：本项目最敏感的机密是**数据库口令**——它不是 AKIA/sk- 那类
# 有固定格式的令牌，gitleaks 与 PAT 都抓不到。没有这道闸门，脱敏就是**唯一**的护栏
# （而"唯一的护栏"一旦漏一条就无声通过）。这里把"已知机密不得出现"变成可测断言。
# ★ 本闸门**只覆盖你登记过的机密**；新机密请及时登记到 .publish-secrets。
SECRETS_FILE="$SRC/.publish-secrets"
if [ -f "$SECRETS_FILE" ]; then
  n_sec=0; hit_sec=0
  while IFS= read -r sec; do
    case "$sec" in ''|\#*) continue ;; esac
    n_sec=$((n_sec + 1))
    found=$(grep -rIFn --exclude-dir=.git -- "$sec" "$DST" 2>/dev/null || true)
    if [ -n "$found" ]; then
      bad "  已知机密出现在导出件里（登记于 .publish-secrets 第 $n_sec 条）："
      # 只报 `文件:行号`，**不回显机密明文**（报告本身就是泄露面）。
      printf '%s\n' "$found" | cut -d: -f1,2 | sed 's/^/      /'
      hit_sec=1
    fi
  done < "$SECRETS_FILE"
  [ "$hit_sec" -eq 0 ] && ok "  已知机密（$n_sec 条）均未出现在导出件"
else
  warn "  无 .publish-secrets ⇒ 跳过已知机密闸门（口令类机密将只靠脱敏）"
fi
note ""

# ── 4. 闸门：绝对路径与个人身份 ──────────────────────────────────────
note "[4] 绝对路径 / 个人身份闸门"

PY=""
for c in "$HOME/ai-services/bin/python" python3 python; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done

# Python 感知扫描：剥掉注释与 docstring，只看可执行代码里的字符串。
# grep 分不出"示例路径"与"真路径"，误报会把闸门淹没、最后被人关掉。
if [ -n "$PY" ] && [ -f "$DST/tools/scan_paths.py" ]; then
  if "$PY" "$DST/tools/scan_paths.py" "$DST"; then :; else bad "  机器绝对路径（见上）"; fi
else
  warn "  scan_paths.py 不可用，退回 grep（误报会较多）"
  P_HIT=$(grep -rInE --exclude-dir=.git '/mnt/[a-z]/|/home/[A-Za-z]|C:\\\\Users' "$DST" 2>/dev/null \
          | grep -vE '^\S+:\s*#' | grep -vE 'github\.com|docs\.|https?://' || true)
  if [ -n "$P_HIT" ]; then
    bad "  发现机器绝对路径："; printf '%s\n' "$P_HIT" | head -20 | sed 's/^/      /'
  else
    ok "  无机器绝对路径"
  fi
fi

# 文档里的 Windows 盘符绝对路径（含本项目自己的 E:\... 示例）——脱敏后不该再有个人盘符。
# 注意 `\\{1,2}`：JSON 串里是双反斜杠、散文里是单反斜杠，两种都要抓。
# ★ 定向自指豁免 `tools/scan_paths.py`：它**必须写出这些模式**才能扫别人，
#   否则就是"测谎仪自己过不了测谎"。豁免**只针对这一个文件、这一道闸门**，
#   并且每次都打印出来——不做无提示的撒网式豁免（那会让发布件里出现免检孤本）。
W_HIT=$(grep -rInoE --exclude-dir=.git --exclude='scan_paths.py' \
        '[A-Za-z]:\\{1,2}(Users|DataServices|公开发布项目)' "$DST" 2>/dev/null || true)
[ -f "$DST/tools/scan_paths.py" ] && note "    （tools/scan_paths.py 已定向豁免本道闸门）"
if [ -n "$W_HIT" ]; then
  bad "  Windows 个人盘符路径："; printf '%s\n' "$W_HIT" | head -20 | sed 's/^/      /'
else
  ok "  无 Windows 个人盘符路径"
fi

# 真实邮箱：占位符放行（文件名或值含 example/xxx/placeholder）。
# 另豁免 **scp 风格的 SSH remote**（`git@github.com` 是"用户名@主机"，不是邮箱）。
E_HIT=$(grep -rInoE --exclude-dir=.git '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$DST" 2>/dev/null \
        | grep -viE 'example\.(com|org)|@example|xxx|placeholder|you@|@your|noreply|git@(ssh\.)?(github|gitlab|bitbucket)\.(com|org)' || true)
if [ -n "$E_HIT" ]; then
  bad "  发现疑似真实邮箱（若非占位符，请改为 example.com）："
  printf '%s\n' "$E_HIT" | sed 's/^/      /'
else
  ok "  无真实邮箱（占位符已放行）"
fi
note ""

# ── 5. 闸门：必备件与纯净度 ──────────────────────────────────────────
note "[5] 必备件 / 纯净度闸门"

# 不该出现的（凭据/本地配置）。
for f in "${FORBIDDEN_FILES[@]}"; do
  if [ -e "$DST/$f" ]; then bad "  导出版里出现 $f（绝不能发布）！"; else ok "  无 $f"; fi
done

# 必须出现的。
REQUIRED=(config.example.yaml LICENSE pyproject.toml README.md .gitattributes)
for f in "${REQUIRED[@]}"; do
  if [ -f "$DST/$f" ]; then ok "  必备件 $f"; else bad "  缺必备件 $f"; fi
done

# 导出件纯净度：白名单之外的东西一律不该在（结果侧的结构性检查）。
NOISE=$(find "$DST" -name '.git' -prune -o \
          \( -name '__pycache__' -o -name '*.pyc' -o -name '*.bak*' \
             -o -name '*.log' -o -name '.DS_Store' -o -name '.cache' \
             -o -name '_generated' \) \
          -print 2>/dev/null || true)
if [ -n "$NOISE" ]; then
  bad "  导出件里混入噪声（白名单之外，不该出现）："
  printf '%s\n' "$NOISE" | sed 's/^/      /'
else
  ok "  导出件纯净（无 __pycache__ / 字节码 / 备份 / 日志）"
fi

# tools/ 下只能有白名单里那几个（防"整目录拷贝"把个人脚本带出去）。
if [ -d "$DST/tools" ]; then
  EXTRA=""
  while IFS= read -r tf; do
    rel="${tf#"$DST"/}"
    keep=0
    for allow in "${ALLOW_TOOLS[@]}"; do [ "$rel" = "$allow" ] && keep=1; done
    [ "$keep" -eq 0 ] && EXTRA="$EXTRA$rel"$'\n'
  done < <(find "$DST/tools" -type f)
  if [ -n "$EXTRA" ]; then
    bad "  tools/ 里有白名单之外的文件："; printf '%s' "$EXTRA" | sed 's/^/      /'
  else
    ok "  tools/ 仅含白名单文件"
  fi
fi
note ""

# ── 6. 语法自检 ──────────────────────────────────────────────────────
note "[6] 导出件语法自检"
if [ -n "$PY" ]; then
  while IFS= read -r f; do
    "$PY" -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f" \
      || bad "语法错误: $f"
  done < <(find "$DST" -name '.git' -prune -o -name '*.py' \
             -not -path '*/__pycache__/*' -print)
  ok "  Python 语法（$PY）"

  # shell 脚本语法
  while IFS= read -r f; do
    bash -n "$f" || bad "shell 语法错误: $f"
  done < <(find "$DST" -name '.git' -prune -o -name '*.sh' -print)
  ok "  shell 语法"
else
  warn "  无 python，跳过"
fi
note ""

# ── 7. 汇总 ──────────────────────────────────────────────────────────
# 清单与体积都必须排除 .git（导出后用户会 git init；重跑时它会把清单灌满）。
note "[7] 导出清单"
( cd "$DST" && find . -name '.git' -prune -o \
    -type f -not -path '*/__pycache__/*' -print | sort | sed 's/^/  /' )
note ""
SIZE=$(du -sh --exclude=.git "$DST" 2>/dev/null | cut -f1)
note "  体积 $SIZE（不含 .git）"
note ""
if [ "$fail" -eq 0 ]; then
  printf "%s导出通过。%s\n" "$GRN" "$RST"
  if [ -d "$DST/.git" ]; then
    note "下一步：cd $DST && git add . && git status   # 仓库已存在；人工复核后再提交"
  else
    note "下一步：cd $DST && git init -b main && git add . && git status   # 人工复核后再提交"
  fi
else
  printf "%s导出被闸门拒绝——**未通过，不要提交**。%s\n" "$RED" "$RST"
fi
exit "$fail"
