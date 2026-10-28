# -*- coding: utf-8 -*-
"""LaTeX（elsarticle）→ Word（docx）转换：pandoc + citeproc + SVG 图。

用法（在本目录下）：
    & ../../sim/.venv/Scripts/python.exe tex2docx.py        # 转换（含 staging 预处理）
    & ../../sim/.venv/Scripts/python.exe docx_png_fallback.py  # 给 SVG 补 PNG 回退（后处理）

产物：``论文_Word版_<日期>.docx``（阅读/传阅版；投稿仍以 LaTeX/PDF 为准）。

为什么需要 staging 预处理（每条都是实测踩坑，pandoc 3.9）：
  1. \\includegraphics 不带扩展名 + \\graphicspath —— pandoc 不认 graphicspath，
     且要钉死用 SVG，必须把路径改写成 ``figures/NAME.svg``；
  2. \\SI{n}{\\unit} —— siunitx pandoc 只认一部分，``\\kilo\\byte`` 直接解析失败，
     统一替换成纯文本（KB/MB/s/ms/µs/Mbps）；
  3. \\multicolumn{N}{@{}l}{...} —— 带 ``@{}`` 的列式会让**整张表被静默丢弃**，
     剥成 ``{l}``；
  4. \\resizebox{...}{!}{% \\begin{tabular}...\\end{tabular}% } —— 包一层表格
     pandoc 看不见，拆壳；
  5. table*（跨栏浮动）—— **caption 会丢**，降级成 table（Word 单栏无影响）。
  转换跑在 paper/word_build/ 的副本上，仓库源文件不动。

图片说明：docx 内嵌 SVG（Word 2016+ 原生显示、无损缩放），并（由
docx_png_fallback.py）补一份 PNG 回退：旧版 Word / WPS / Google Docs 显示 PNG。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

PAPER = Path(__file__).resolve().parent          # paper/
WB = PAPER / "word_build"
ROOT = PAPER.parent
PANDOC = ROOT / "sim/.venv/Lib/site-packages/pypandoc/files/pandoc.exe"
OUT = PAPER / "submission" / "ComputerNetworks_2026-10-22" / "word" / "ComputerNetworks_manuscript_2026-10-22.docx"

UNITS = {
    "\\second": "s", "\\milli\\second": "ms", "\\micro\\second": "µs",
    "\\kilo\\byte": "KB", "\\mega\\byte": "MB", "\\giga\\byte": "GB",
    "\\byte": "B", "\\mega\\bit\\per\\second": "Mbps",
    "\\giga\\bit\\per\\second": "Gbps", "\\percent": "%", "": "",
}


def patch_tex(t: str) -> str:
    """staging 预处理（规则与原因见模块 docstring）。"""
    t = re.sub(r"(\\includegraphics(?:\[[^\]]*\])?)\{([^}.]+)\}",
               lambda m: f"{m.group(1)}{{figures/{m.group(2)}.svg}}", t)
    t = re.sub(r"\\SI\{([^}]*)\}\{([^}]*)\}",
               lambda m: f"{m.group(1)} {UNITS.get(m.group(2), m.group(2))}".strip(), t)
    t = re.sub(r"\\SIrange\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}",
               lambda m: f"{m.group(1)}--{m.group(2)} {UNITS.get(m.group(3), m.group(3))}".strip(), t)
    t = re.sub(r"\\si\{([^}]*)\}",
               lambda m: UNITS.get(m.group(1), m.group(1)), t)
    t = re.sub(r"\\num\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\multicolumn\{(\d+)\}\{@\{\}([lcr])\}",
               r"\\multicolumn{\1}{\2}", t)
    t = re.sub(r"\\resizebox\{[^}]*\}\{!\}\{%\s*", "", t)
    t = re.sub(r"(\\end\{tabular\})\s*%\s*\}", r"\1", t)
    t = t.replace(r"\begin{table*}", r"\begin{table}").replace(
        r"\end{table*}", r"\end{table}")
    return t


def main() -> int:
    if not PANDOC.exists():
        print(f"[错] pandoc 不存在：{PANDOC}（pip install pypandoc-binary）")
        return 2

    (WB / "sections").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PAPER / "main_elsevier.tex", WB / "main_word.tex")
    # pandoc/citeproc 不会自动加 "References" 标题；在副本里补一个，源文件不动。
    mw = (WB / "main_word.tex").read_text(encoding="utf-8")
    if "\\section*{References}" not in mw:
        mw = mw.replace("\\bibliographystyle{elsarticle-num}",
                        "\\section*{References}\n\\bibliographystyle{elsarticle-num}")
        (WB / "main_word.tex").write_text(mw, encoding="utf-8")

    # staging + 残留自检（有残留就停，防静默丢内容）
    checks = {"SI": r"\\SI\{", "multicolumn@": r"\\multicolumn\{\d+\}\{@",
              "resizebox": r"\\resizebox", "table*": r"table\*"}
    leftover = {k: 0 for k in checks}
    for f in sorted((PAPER / "sections").glob("*.tex")):
        t = patch_tex(f.read_text(encoding="utf-8"))
        for k, pat in checks.items():
            leftover[k] += len(re.findall(pat, t))
        (WB / "sections" / f.name).write_text(t, encoding="utf-8")
    print("staging 完成；残留:", leftover)
    if any(leftover.values()):
        print("[错] staging 有残留，禁止继续")
        return 2

    # 图的 SVG 副本（resource-path 首选 word_build/figures）
    figdst = WB / "figures"
    figdst.mkdir(exist_ok=True)
    for svg in (PAPER / "figures").glob("*.svg"):
        shutil.copy2(svg, figdst / svg.name)
    if not list(figdst.glob("*.svg")):
        print("[错] paper/figures 下没有 .svg；先跑 sim/analysis/make_paper_figures.py 的 SVG 版")

    args = [
        str(PANDOC), "main_word.tex",
        "-f", "latex", "-t", "docx",
        "-o", str(OUT),
        "--resource-path=.;..;../figures",
        "--citeproc", "--bibliography=../refs.bib", "--csl=../tools/elsevier-vancouver.csl",
        "--number-sections",
        "--metadata", "lang=en",
    ]
    r = subprocess.run(args, cwd=WB, capture_output=True, text=True, encoding="utf-8")
    print("pandoc exit:", r.returncode)
    warn = [l for l in r.stderr.splitlines()
            if l.strip() and "rsvg-convert" not in l and "Could not convert image" not in l]
    for l in warn[:15]:
        print("  ", l[:150])
    if OUT.exists():
        print(f"输出: {OUT}  ({OUT.stat().st_size:,} B)")
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
