#!/usr/bin/env bash
# 编译 Elsevier 版（elsarticle）。用法：bash build_elsevier.sh
# 与 build.sh 同为三轮（pdflatex → bibtex → pdflatex ×2），原因见 build.sh 顶部注释。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PDFLATEX="${PDFLATEX:-pdflatex}"
BIBTEX="${BIBTEX:-bibtex}"
MAIN=main_elsevier

echo "== 1/3 pdflatex =="
"$PDFLATEX" -interaction=nonstopmode -halt-on-error "$MAIN.tex" >/dev/null || {
    echo "第 1 轮失败，看 ${MAIN}.log 的最后一个 '!' 行："; grep -n -A 5 '^!' "$MAIN.log" | tail -30; exit 1; }
echo "== 2/3 bibtex =="
"$BIBTEX" "$MAIN" >/dev/null || { echo "bibtex 失败，看 ${MAIN}.blg："; tail -20 "$MAIN.blg"; exit 1; }
echo "== 3/3 pdflatex ×2 =="
"$PDFLATEX" -interaction=nonstopmode -halt-on-error "$MAIN.tex" >/dev/null
"$PDFLATEX" -interaction=nonstopmode -halt-on-error "$MAIN.tex" >/dev/null
echo
echo "完成：$(pwd)/${MAIN}.pdf"
echo "--- 未定义引用（应为 0）---"; grep -c "LaTeX Warning: Reference.*undefined" "$MAIN.log" || true
echo "--- 未定义引用文献（应为 0）---"; grep -c "LaTeX Warning: Citation.*undefined" "$MAIN.log" || true
echo "--- 超出版心的行数（越少越好）---"; grep -c "Overfull \\hbox" "$MAIN.log" || true
