"""论文体检：把"投稿前该检查的机械项"一次跑完。

用法（在仓库根执行）：

    sim/.venv/Scripts/python.exe paper/audit_pdf.py

检查项：
  1. 摘要词数（IEEE 期刊通常限 150–250 词；超了要压缩）；
  2. 正文词数；
  3. 图/表数量与在正文里被引用的次数（图没被引用是硬伤）；
  4. 未填的占位符（作者、单位、邮箱、TODO…）；
  5. `refs.bib` 的完整度（缺作者 / 缺页码 / 缺 DOI）；
  6. 编译告警（overfull 超出版心 / underfull 间距过松 / 字体缺失）；
  7. 正文里是否残留中文（英文投稿不能有）。

**为什么要有这个脚本**：这些项逐条手查很容易漏，而每一条都是审稿人或编辑会直接指出的。
跑一次 3 秒，比返修一轮便宜得多。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

# 注意：下面所有正则都用原始字符串（r"..."）。曾经在 shell heredoc 里写 LaTeX 正则，
# 反斜杠被 shell 吃掉导致脚本报 "bad escape" —— 所以这个脚本单独成文件。
CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def strip_comments(s: str) -> str:
    """去掉 `%` 到行尾的注释，但**跳过转义的 `\\%`**。

    【踩过的坑】直接把 `%` 当注释起点，会把 `70\\%` 这类字面百分号后面的整行吃掉——
    实测摘要词数因此从 400+ 变成 "1 词"，而那种错误报告比不报告更糟。
    """
    out = []
    for line in s.splitlines():
        cut, i = None, 0
        while i < len(line):
            if line[i] == "%":
                if i and line[i - 1] == "\\":
                    i += 1
                    continue
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def strip_tex(s: str) -> str:
    """粗略去掉 LaTeX 命令，只留可读文字（够用来数词数）。"""
    s = strip_comments(s)
    s = re.sub(r"\\begin\{(equation|align|tabular|table|figure)\*?\}.*?\\end\{\1\*?\}",
               " ", s, flags=re.S)
    s = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", s)
    s = re.sub(r"[{}$&\\~^_]", " ", s)
    return s


def main() -> int:
    main_tex = ROOT / "main.tex"
    tex = main_tex.read_text(encoding="utf-8")
    secs = sorted((ROOT / "sections").glob("*.tex"))
    body = "\n".join(p.read_text(encoding="utf-8") for p in secs)
    allt = strip_comments(tex) + strip_comments(body)

    # 必须先在**去注释之后**的文本里找 abstract：main.tex 的头部注释里也写了
    # `\begin{abstract}...\end{abstract}`（解释 elsarticle 替换用），不去注释会先命中注释，
    # 结果把摘要算成 "1 词"。踩过一次。
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
                  strip_comments(tex), re.S)
    ab_words = len(strip_tex(m.group(1)).split()) if m else 0
    over = "   ⚠ 超过 250 词，IEEE 期刊一般要求 150–250" if ab_words > 250 else "   ✓"
    print(f"1) 摘要：{ab_words} 词{over}")
    print(f"2) 正文（十节，含表格内容）：约 {len(strip_tex(body).split())} 词")

    n_fig = len(re.findall(r"\\begin\{figure\*?\}", allt))
    n_fig_ref = len(re.findall(r"\\ref\{fig:", allt))
    n_tab = len(re.findall(r"\\begin\{table\*?\}", allt))
    n_tab_ref = len(re.findall(r"\\ref\{tab:", allt))
    flag = "   ⚠ 有图/表未被引用" if (n_fig > n_fig_ref or n_tab > n_tab_ref) else "   ✓"
    print(f"3) 图 {n_fig} 个（引用 {n_fig_ref} 次）；表 {n_tab} 个（引用 {n_tab_ref} 次）{flag}")

    ph = []
    for pat, desc in ((r"\[Author Name\]", "作者"), (r"\[Affiliation\]", "单位"),
                      (r"\[email@example\.org\]", "邮箱"), (r"\[City\]", "城市"),
                      (r"\[Country\]", "国家"), (r"TODO", "TODO"), (r"XXX", "XXX")):
        n = len(re.findall(pat, allt))
        if n:
            ph.append(f"{desc}×{n}")
    print(f"4) 未填占位符：{'、'.join(ph) if ph else '无'}" + ("   ⚠ 投稿前必须替换" if ph else "   ✓"))

    bib = (ROOT / "refs.bib").read_text(encoding="utf-8")
    ents = re.findall(r"@\w+\{([^,]+),(.*?)\n\}", bib, re.S)
    no_auth = [k for k, b in ents if "author" not in b.lower()]
    no_page = [k for k, b in ents if "pages" not in b.lower()]
    no_doi = [k for k, b in ents if "doi" not in b.lower()]
    print(f"5) 文献 {len(ents)} 条：缺作者 {len(no_auth)}、缺页码 {len(no_page)}、缺 DOI {len(no_doi)}")
    if no_auth:
        print(f"   缺作者（投稿前必须补）：{', '.join(no_auth)}")

    log = ROOT / "main.log"
    if log.exists():
        t = log.read_text(encoding="utf-8", errors="replace")
        ov = len(re.findall(r"Overfull \\hbox", t))
        un = len(re.findall(r"Underfull \\hbox", t))
        fw = len(re.findall(r"Font Warning", t))
        und = len(re.findall(r"LaTeX Warning: (?:Reference|Citation).*undefined", t))
        print(f"6) 编译告警：overfull {ov} 处、underfull {un} 处、字体 {fw} 条、未定义引用 {und} 处")
    else:
        print("6) 找不到 main.log（先跑 bash build.sh）")

    cjk = CJK.findall(strip_comments(body)) + CJK.findall(strip_comments(tex))
    print(f"7) 正文残留中文：{len(cjk)} 个字符" + ("   ⚠ 英文投稿不能有" if cjk else "   ✓"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
