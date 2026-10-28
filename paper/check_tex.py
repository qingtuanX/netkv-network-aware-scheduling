"""对 `paper/` 做静态自检（本机没装 LaTeX 编译器，所以用脚本先抓低级错误）。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/check_tex.py

检查五件事：
  1. 每个 `\\cite{...}` 的键都在 `refs.bib` 里有对应条目（避免 `[?]`）；
  2. 每个 `\\ref{...}` / `\\eqref{...}` 都有匹配的 `\\label{...}`；
  3. 每个文件里 `\\begin{env}` / `\\end{env}` 配对且顺序正确；
  4. 每个 `\\includegraphics{...}` 在 `paper/figures/` 里找得到（pdf 或 png）；
  5. 明显的括号/数学环境不平衡（`$` 计数、`{}` 计数）。

**为什么值得单独做**：LaTeX 的很多低级错误（少一个 `}`、标签拼错、图不存在）
在有编译器时也要看日志才知道；没有编译器时，这个脚本是唯一的自动化防线。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

CITE_RE = re.compile(r"\\cite[a-z]*\{([^}]*)\}")
REF_RE = re.compile(r"\\(?:ref|eqref)\{([^}]*)\}")
LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
BE_RE = re.compile(r"\\(begin|end)\{([^}]*)\}")
INC_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}")


def bib_keys(text: str) -> set[str]:
    return set(re.findall(r"@\w+\{([^,]+),", text))


def strip_comments(text: str) -> str:
    """去掉整行注释（`%` 到行尾），但**跳过转义的 `\\%`**。

    【为什么必须处理 `\\%`】正文里到处是 `\\%`（百分号字面量）。如果把 `\\%` 当成注释起点，
    会把该行后半段（往往包含配对的 `$`）一起吃掉，于是"数学公式 `$` 计数为奇数"这种
    假警告就会出现——本项目第一次跑就踩了这个坑。
    """
    out = []
    for line in text.splitlines():
        cut = None
        i = 0
        while i < len(line):
            if line[i] == "%":
                if i > 0 and line[i - 1] == "\\":
                    i += 1          # 转义百分号，继续
                    continue
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def main() -> int:
    main_tex = ROOT / "main_elsevier.tex"
    refs = ROOT / "refs.bib"
    files = [main_tex] + sorted((ROOT / "sections").glob("*.tex"))
    bib = strip_comments(refs.read_text(encoding="utf-8"))
    known = bib_keys(bib)

    errors: list[str] = []
    warnings: list[str] = []

    # ---- 1. 引用键 ----
    cited: set[str] = set()
    for f in files:
        for m in CITE_RE.finditer(strip_comments(f.read_text(encoding="utf-8"))):
            for k in m.group(1).split(","):
                k = k.strip()
                if k:
                    cited.add(k)
    missing = sorted(cited - known)
    unused = sorted(known - cited)
    if missing:
        errors.append(f"引用了 refs.bib 里不存在的键：{missing}")
    if unused:
        warnings.append(f"refs.bib 里有 {len(unused)} 个条目未被引用：{unused}")

    # ---- 2/3/5 逐文件 ----
    labels: dict[str, str] = {}
    for f in files:
        raw = f.read_text(encoding="utf-8")
        txt = strip_comments(raw)
        # 环境配对（用栈，能抓出顺序错误）
        stack: list[str] = []
        for m in BE_RE.finditer(txt):
            kind, env = m.group(1), m.group(2)
            if kind == "begin":
                stack.append(env)
            else:
                if not stack:
                    errors.append(f"{f.name}: 多了一个 \\end{{{env}}}")
                elif stack[-1] != env:
                    errors.append(
                        f"{f.name}: 环境交叉——期望 \\end{{{stack[-1]}}} 却遇到 \\end{{{env}}}")
                    stack.pop()
                else:
                    stack.pop()
        if stack:
            errors.append(f"{f.name}: 这些环境没有 \\end：{stack}")
        # 标签
        for m in LABEL_RE.finditer(txt):
            lab = m.group(1)
            if lab in labels:
                errors.append(f"{f.name}: 标签 {lab} 重复（已在 {labels[lab]} 里）")
            labels[lab] = f.name
        # 数学环境里的 $ 计数（display math 用 \[ \] 或 equation，不在此列）
        n_dollar = txt.count("$") - 2 * len(re.findall(r"\$\$", txt))
        if n_dollar % 2:
            warnings.append(f"{f.name}: 行内数学的 $ 出现奇数次（{n_dollar}），可能有未闭合")
        # 花括号
        if txt.count("{") != txt.count("}"):
            warnings.append(f"{f.name}: 花括号数量不等 "
                            f"（{{ = {txt.count('{')}, }} = {txt.count('}')}）")
        # 图片文件
        for m in INC_RE.finditer(txt):
            name = m.group(1)
            cands = [ROOT / "figures" / f"{name}{ext}" for ext in ("", ".pdf", ".png")]
            if not any(c.exists() for c in cands):
                errors.append(f"{f.name}: 找不到图片 {name}（找过 "
                              f"{[str(c.relative_to(ROOT)) for c in cands]}）")

    # ---- 2. 交叉引用 ----
    for f in files:
        txt = strip_comments(f.read_text(encoding="utf-8"))
        for m in REF_RE.finditer(txt):
            for lab in m.group(1).split(","):
                lab = lab.strip()
                if lab and lab not in labels:
                    errors.append(f"{f.name}: \\ref{{{lab}}} 没有对应的 \\label")

    print(f"检查文件：{len(files)} 个（main_elsevier.tex + sections/*.tex）")
    print(f"引用键：正文用了 {len(cited)} 个，refs.bib 里有 {len(known)} 个")
    print(f"标签：{len(labels)} 个")
    for w in warnings:
        print(f"  [警告] {w}")
    if errors:
        print(f"\n发现 {len(errors)} 个错误：")
        for e in errors:
            print(f"  [错误] {e}")
        return 1
    print("\n通过：引用键、交叉引用、环境配对、图片文件、括号计数均未发现问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
