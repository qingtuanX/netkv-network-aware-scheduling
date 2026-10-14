"""抽取 文献/ 下所有 PDF 的正文文本，缓存到 sim/data/processed/pdf_text/。

用途：为「中英对照全文翻译」提供可 grep 的纯文本源，避免反复解析 PDF。
已存在的缓存默认跳过；用 --force 强制重抽。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent          # 论文/
LIT = ROOT / "文献"
OUT = ROOT / "sim" / "data" / "processed" / "pdf_text"
SKIP_DIRS = {"05-中文", "07-数据集与trace", "译文"}

# pypdf 偶尔抽到 NUL 等控制字符，会让下游 grep 把文件判成 binary，统一清掉。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL.sub("", text)


def extract(pdf: Path) -> str:
    reader = PdfReader(str(pdf))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            t = f"\n[[page {i} extract failed: {e}]]\n"
        parts.append(f"\n===== PAGE {i} =====\n{t}")
    return sanitize("".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(
        p for p in LIT.rglob("*.pdf")
        if not any(part in SKIP_DIRS for part in p.parts)
    )
    print(f"found {len(pdfs)} pdfs")
    ok = skip = fail = 0
    for pdf in pdfs:
        dst = OUT / (pdf.stem + ".txt")
        if dst.exists() and not args.force:
            skip += 1
            continue
        try:
            text = extract(pdf)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {pdf.name}: {e}")
            fail += 1
            continue
        if len(text) < 2000:
            print(f"WARN {pdf.name}: only {len(text)} chars")
        dst.write_text(text, encoding="utf-8")
        print(f"ok   {pdf.name} -> {len(text)} chars")
        ok += 1
    print(f"\ndone: ok={ok} skip={skip} fail={fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
