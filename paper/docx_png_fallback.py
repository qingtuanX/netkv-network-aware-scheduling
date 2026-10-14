# -*- coding: utf-8 -*-
"""docx 后处理：给每个内嵌 SVG 补一份 PNG 回退，并做结构校验。

为什么：pandoc 把 SVG 嵌入 docx 时（本机没有 rsvg-convert）不给 PNG 回退，
主 ``<a:blip>`` 只有扩展里的 ``asvg:svgBlip``。Word 2016+/365 能显示，
但旧版 Word / WPS / Google Docs 可能显示空白。本脚本把 paper/figures 下
同名 PNG 塞进包里，写进主 ``<a:blip r:embed=...>``（标准做法）。

SVG↔原图的对应关系：pandoc 会把媒体文件改名成 ``rIdN.svg``，
所以用**文件内容 sha256** 反查 stem（不依赖命名）。

用法（在 paper/ 下）：  python docx_png_fallback.py
"""
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

PAPER = Path(__file__).resolve().parent
FIGDIR = PAPER / "figures"
DOCX = PAPER / "论文_Word版_20261012.docx"


def main() -> int:
    svg_by_hash = {hashlib.sha256(f.read_bytes()).hexdigest(): f.stem
                   for f in FIGDIR.glob("*.svg")}

    zin = zipfile.ZipFile(DOCX)
    entries = {e.filename: zin.read(e.filename) for e in zin.infolist()}
    zin.close()

    doc = entries["word/document.xml"].decode("utf-8")
    rels = entries["word/_rels/document.xml.rels"].decode("utf-8")
    ctypes = entries["[Content_Types].xml"].decode("utf-8")

    rid2target = dict(re.findall(r'Id="(rId\d+)"[^>]*Target="(media/[^"]+)"', rels))

    plan = []
    for m in re.finditer(r'asvg:svgBlip[^>]*r:embed="(rId\d+)"', doc):
        rid = m.group(1)
        target = rid2target.get(rid)
        if target and target.endswith(".svg"):
            stem = svg_by_hash.get(hashlib.sha256(entries[f"word/{target}"]).hexdigest())
            if stem:
                plan.append((rid, stem))
    print(f"识别到 {len(plan)} 张 SVG")

    add_rels = []
    for i, (svg_rid, stem) in enumerate(plan, 1):
        png = FIGDIR / f"{stem}.png"
        if not png.exists():
            print(f"  [跳过] 缺 {png.name}")
            continue
        png_rid = f"rIdPng{i}"
        entries[f"word/media/{stem}.png"] = png.read_bytes()
        add_rels.append(
            '<Relationship Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/image" '
            f'Id="{png_rid}" Target="media/{stem}.png" />')
        pat = re.compile(
            r'(<a:blip)(><a:extLst>(?:(?!</a:blip>).)*?r:embed="' + svg_rid + r'")',
            re.DOTALL)
        doc, n = pat.subn(rf'\1 r:embed="{png_rid}"\2', doc)
        if n != 1:                     # 已经是处理过的文件时 n=0，提示而不崩
            print(f"  [!] {stem}: 替换 {n} 处（可能已处理过），跳过")
            continue
        print(f"  + {stem}.png 作为 {svg_rid} 的回退")

    if '<Default Extension="png"' not in ctypes:
        ctypes = ctypes.replace(
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml" />',
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
            '<Default Extension="png" ContentType="image/png" />')

    rels = rels.replace("</Relationships>", "".join(add_rels) + "</Relationships>")
    entries["word/document.xml"] = doc.encode("utf-8")
    entries["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    entries["[Content_Types].xml"] = ctypes.encode("utf-8")

    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)
    print(f"重打包完成: {DOCX} ({DOCX.stat().st_size:,} B)")

    # 校验：XML 良构 + 媒体清单
    z = zipfile.ZipFile(DOCX)
    ET.fromstring(z.read("word/document.xml"))
    ET.fromstring(z.read("word/_rels/document.xml.rels"))
    ET.fromstring(z.read("[Content_Types].xml"))
    media = sorted(n for n in z.namelist() if n.startswith("word/media/"))
    z.close()
    print(f"XML 良构 ✓；媒体 {len(media)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
