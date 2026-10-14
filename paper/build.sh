#!/usr/bin/env bash
# 编译论文（在 paper/ 目录下执行：bash build.sh）
#
# 为什么要三轮：第 1 轮生成 .aux/.bbl 输入，bibtex 读 .aux 产出 .bbl，
# 第 2–3 轮把参考文献与交叉引用的编号回填（第 2 轮后页码/图号才稳定）。
# 只跑一轮的话，\ref 会显示 "??"，参考文献不出现——这是最常见的"编译成功但看着不对"。
#
# -halt-on-error：遇到第一个错误就停，便于定位；不要去掉，否则会在错误上继续跑出满屏垃圾。
# 输出：main.pdf；日志：main.log（排错看它，或看 main.blg 查 bibtex 的问题）。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PDFLATEX="${PDFLATEX:-pdflatex}"
BIBTEX="${BIBTEX:-bibtex}"

echo "== 1/3 pdflatex =="
"$PDFLATEX" -interaction=nonstopmode -halt-on-error main.tex >/dev/null || {
    echo "第 1 轮失败，看 main.log 的最后一个 '!' 行："; grep -n -A 5 '^!' main.log | tail -30; exit 1; }

echo "== 2/3 bibtex =="
"$BIBTEX" main >/dev/null || { echo "bibtex 失败，看 main.blg："; tail -20 main.blg; exit 1; }

echo "== 3/3 pdflatex ×2 =="
"$PDFLATEX" -interaction=nonstopmode -halt-on-error main.tex >/dev/null
"$PDFLATEX" -interaction=nonstopmode -halt-on-error main.tex >/dev/null

echo
echo "完成：$(pwd)/main.pdf"
# 把值得注意的告警挑出来（overfull 表示表格/行超出版心，投稿前应处理）
echo "--- 未定义引用（应为 0）---"
grep -c "LaTeX Warning: Reference.*undefined" main.log || true
echo "--- 未定义引用文献（应为 0）---"
grep -c "LaTeX Warning: Citation.*undefined" main.log || true
echo "--- 超出版心的行数（越少越好，>10pt 值得处理）---"
grep -c "Overfull \\\\hbox" main.log || true
