# -*- coding: utf-8 -*-
"""
抽取「交易有关」课程语料 -> 纯文本 + 去重 + 溯源索引。

用途:
  把 PDF / DOCX 课程资料批量抽成 UTF-8 文本(放到 knowledge/_raw/, 不进 git),
  并生成 corpus_index.csv 作为溯源与去重的总账, 供后续蒸馏知识卡片使用。

可重复运行(幂等): 重跑会覆盖 _raw/ 与 corpus_index.csv。

依赖: pymupdf (PDF 文字抽取); DOCX 用标准库 zipfile 解析, 无需 python-docx。
"""
from __future__ import annotations
import csv
import glob
import hashlib
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_DIR = os.path.dirname(HERE)          # .../knowledge
RAW_DIR = os.path.join(KNOWLEDGE_DIR, "_raw")  # gitignored
INDEX_CSV = os.path.join(KNOWLEDGE_DIR, "corpus_index.csv")

# 高价值切片的主题打标关键词(命中文件名/相对路径即归入该主题, 可多归)
PRIORITY_THEMES = {
    "情绪周期": ["情绪周期", "情绪", "题材节奏", "退潮", "冰点", "涨停潮", "接力"],
    "实用指标": ["实用指标", "指标", "涨跌家数", "持续性", "见顶分歧", "情绪强弱"],
    "启动标准": ["启动", "破局", "板块启动", "共振板块"],
    "强势股模式": ["强势", "龙头", "大长腿", "趋势", "主升", "低吸", "反包", "弱转强", "新王"],
    "仓位风控": ["仓位", "风报比", "赔率", "概率", "账户管理", "回撤", "满仓"],
}


DEFAULT_TRADE_DIR = r"D:\BaiduNetdiskDownload\personal\交易有关"


def find_trade_dir() -> str:
    d = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRADE_DIR
    if not os.path.isdir(d):
        raise SystemExit(f"交易资料目录不存在: {d}")
    return d


def extract_pdf(path: str) -> str:
    import fitz
    doc = fitz.open(path)
    parts = [doc.load_page(i).get_text() for i in range(doc.page_count)]
    doc.close()
    return "\n".join(parts)


def extract_docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t")
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n[ \t]*\n+", "\n", text).strip()


def norm_title(name: str) -> str:
    """归一化标题用于去重: 去扩展名/前导序号/watermark/日期括号。"""
    t = os.path.splitext(name)[0]
    t = re.sub(r"^\s*\d+[\.、]\s*", "", t)          # 去 "08." / "1、"
    t = re.sub(r"_?wm$|_watermark$|_\d+_wm$", "", t, flags=re.I)
    t = re.sub(r"[（(]\s*20\d{2}[-.\d]*\s*[)）]", "", t)  # 去 (2019-04-13)
    return re.sub(r"\s+", "", t).strip()


def first_date(text: str, name: str) -> str:
    for s in (name, text[:400]):
        m = re.search(r"(20\d{2})[-.\/年]?\s?(\d{1,2})?[-.\/月]?\s?(\d{1,2})?", s)
        if m and m.group(1):
            y, mo, d = m.group(1), m.group(2) or "", m.group(3) or ""
            return f"{y}-{mo.zfill(2) if mo else '00'}-{d.zfill(2) if d else '00'}"
    return ""


def themes_for(rel_path: str) -> str:
    hits = [th for th, kws in PRIORITY_THEMES.items() if any(k in rel_path for k in kws)]
    return "|".join(hits)


def main() -> None:
    trade_dir = find_trade_dir()
    if os.path.isdir(RAW_DIR):
        import shutil
        shutil.rmtree(RAW_DIR)
    os.makedirs(RAW_DIR, exist_ok=True)
    targets = []
    for ext in ("*.pdf", "*.docx"):
        targets += glob.glob(os.path.join(trade_dir, "**", ext), recursive=True)
    targets = sorted(set(targets))

    rows = []
    seen_hash: dict[str, str] = {}  # content_hash -> first doc_id
    for i, path in enumerate(targets, 1):
        rel = os.path.relpath(path, trade_dir)
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        doc_id = f"D{i:04d}"
        try:
            text = extract_pdf(path) if ext == ".pdf" else extract_docx(path)
        except Exception as e:  # 老 .doc 二进制 / 损坏文件
            rows.append([doc_id, name, ext, rel, "", 0, "", "", f"EXTRACT_ERR:{type(e).__name__}", themes_for(rel)])
            continue
        chars = len(text)
        chash = hashlib.md5((norm_title(name) + "|" + text[:1500]).encode("utf-8")).hexdigest()[:12]
        dup_of = seen_hash.get(chash, "")
        if not dup_of:
            seen_hash[chash] = doc_id
            safe = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(name)[0])[:60]
            out_path = os.path.join(RAW_DIR, f"{doc_id}_{safe}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"# {name}\n# source: {rel}\n\n{text}")
        rows.append([doc_id, name, ext, rel, first_date(text, name), chars,
                     norm_title(name), dup_of, "OK" if not dup_of else "DUP", themes_for(rel)])

    with open(INDEX_CSV, "w", encoding="utf-8-sig", newline="") as f:
        wcsv = csv.writer(f)
        wcsv.writerow(["doc_id", "filename", "ext", "rel_path", "date", "chars",
                       "norm_title", "dup_of", "status", "priority_themes"])
        wcsv.writerows(rows)

    uniq = sum(1 for r in rows if r[8] == "OK")
    dup = sum(1 for r in rows if r[8] == "DUP")
    err = sum(1 for r in rows if str(r[8]).startswith("EXTRACT_ERR"))
    print(f"trade_dir : {trade_dir}")
    print(f"docs total: {len(rows)}  unique: {uniq}  dup: {dup}  err: {err}")
    print(f"raw text  : {RAW_DIR}")
    print(f"index csv : {INDEX_CSV}")
    by_theme = {}
    for r in rows:
        if r[8] != "OK" or not r[9]:
            continue
        for th in r[9].split("|"):
            by_theme[th] = by_theme.get(th, 0) + 1
    print("priority unique docs by theme:")
    for th, c in sorted(by_theme.items(), key=lambda x: -x[1]):
        print(f"  {th:8s} {c}")


if __name__ == "__main__":
    main()
