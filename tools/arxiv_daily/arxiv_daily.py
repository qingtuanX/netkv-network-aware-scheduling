#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arXiv 大模型 / AI Infra 每日简报（零第三方依赖，只用标准库）。

它做四件事：
  1. 从 arXiv API 抓 cs.CL / cs.LG / cs.AI 当天新公告的论文；
  2. 先用关键词粗排，再把最像的 20 篇交给大模型做「分类 + 中文翻译 + 中文摘要」；
  3. 写成中文日报（Markdown + JSON）；
  4. 通过本机微信桥推一条手机能直接看的中文摘要（不放本地路径，只放 arXiv 链接）。

用法：
    python tools/arxiv_daily/arxiv_daily.py            # 正常跑一次
    python tools/arxiv_daily/arxiv_daily.py --no-state  # 看全量（不去重）
    python tools/arxiv_daily/arxiv_daily.py --dry-run   # 只抓取统计，不花钱不落盘
    python tools/arxiv_daily/arxiv_daily.py --no-push   # 只出文件不推微信

设计约束（都是踩坑换来的）：
- 零第三方依赖：定时任务要在没有虚拟环境时也能跑。
- 幂等：state/seen.json 记已读 id，一天跑两次不会重复报。
- 显式失败：抓取失败退出码 2；推送失败只记日志，不影响日报落盘。
- 时区：内部 UTC，展示固定 +08:00（Windows 的 Python 没有时区库，别用 zoneinfo）。
- 编码：文件一律 UTF-8；读配置/状态用 utf-8-sig（Windows 记事本会加 BOM）。
- 大模型只翻「粗排后的前 20 篇」，别拿 600 篇去烧 token。
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
STATE_PATH = HERE / "state" / "seen.json"
CURATED_STATE = HERE / "state" / "curated.json"
BRIEF_DIR = HERE / "briefs"
LOG_PATH = HERE / "logs" / "run.log"

API_URL = "https://export.arxiv.org/api/query"
S2_BULK = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
ARXIV_RSS = "https://rss.arxiv.org/rss/"
USER_AGENT = "arxiv-daily-digest/0.3 (personal research digest; contact: qiafe@local)"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
BEIJING = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log(msg: str, *, echo: bool = True) -> None:
    stamp = dt.datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if echo and sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 文本清洗

_CMD_WITH_ARG = re.compile(r"\\[A-Za-z]+\s*\{([^{}]*)\}")
_TEX_CMD = re.compile(r"\\[A-Za-z]+")
_MATH = re.compile(r"\$([^$]{1,160})\$")


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    s = s.replace("<br />", " ").replace("<br/>", " ")
    s = _CMD_WITH_ARG.sub(r"\1", s)
    s = _MATH.sub(r"\1", s)
    s = _TEX_CMD.sub("", s)
    s = s.replace("{", "").replace("}", "").replace("~", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"(\[])")


def excerpt(text: str, max_chars: int = 430, max_sentences: int = 2) -> str:
    if not text:
        return ""
    parts = _SENTENCE.split(text)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + " …"
    return out


# ---------------------------------------------------------------- HTTP

def http_get(url: str, *, timeout: int = 60, attempts: int = 4, backoff: float = 5.0) -> bytes:
    last_error: object = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (403, 429, 500, 502, 503, 504):
                wait = backoff * (2 ** i) + random.uniform(0, 1.5)
                log("HTTP %d，%.1fs 后重试（第 %d/%d 次）" % (exc.code, wait, i + 1, attempts))
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            wait = backoff * (2 ** i) + random.uniform(0, 1.5)
            log("网络错误 %s，%.1fs 后重试（第 %d/%d 次）" % (exc, wait, i + 1, attempts))
            time.sleep(wait)
    raise RuntimeError("抓取失败，已重试 %d 次：%s" % (attempts, last_error))


# ---------------------------------------------------------------- 抓取与解析

def parse_dt(text: str) -> dt.datetime | None:
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_entry(node: ET.Element) -> dict | None:
    def get(tag: str) -> str:
        el = node.find(tag, NS)
        return (el.text or "") if el is not None else ""

    raw_id = get("a:id").strip()
    if "/abs/" not in raw_id:
        return None
    short_id = raw_id.split("/abs/", 1)[1].strip()
    base_id = re.sub(r"v\d+$", "", short_id)
    title = clean_text(get("a:title"))
    abstract = clean_text(get("a:summary"))
    published = parse_dt(get("a:published"))
    if not base_id or not title or published is None:
        return None

    primary_el = node.find("arxiv:primary_category", NS)
    primary = primary_el.get("term") if primary_el is not None else ""
    categories = [c.get("term") for c in node.findall("a:category", NS) if c.get("term")]
    authors = [a.findtext("a:name", default="", namespaces=NS).strip()
               for a in node.findall("a:author", NS)]
    authors = [a for a in authors if a]

    return {
        "id": base_id,
        "version": short_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": published.isoformat(),
        "published_bj": published.astimezone(BEIJING).strftime("%m-%d %H:%M"),
        "primary": primary,
        "categories": categories,
        "comment": clean_text(get("arxiv:comment")),
        "journal_ref": clean_text(get("arxiv:journal_ref")),
        "abs_url": "https://arxiv.org/abs/" + base_id,
        "pdf_url": "https://arxiv.org/pdf/" + base_id,
        "source": "arxiv",
        "venue_name": "",
    }


def fetch_window(cfg: dict, start: dt.datetime, end: dt.datetime, max_results: int):
    cats = [c.strip() for c in cfg.get("categories", []) if str(c).strip()]
    if not cats:
        raise ValueError("config.json 里 categories 为空")
    # 括号必须有：不加括号时 arXiv 会把 "A OR B AND 日期" 算成先 OR，日期条件几乎失效
    cat_expr = " OR ".join("cat:" + c for c in cats)
    query = "(%s) AND submittedDate:[%s TO %s]" % (
        cat_expr, start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"))
    log("查询：%s" % query)

    papers: list[dict] = []
    total = 0
    offset = 0
    page = min(1000, max_results)
    while True:
        params = {
            "search_query": query,
            "start": offset,
            "max_results": min(page, max_results - len(papers)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        if params["max_results"] <= 0:
            break
        url = API_URL + "?" + urllib.parse.urlencode(params)
        # API 一旦限流，等它重试很浪费；RSS 兜底更快，所以这里只重试 2 次
        root = ET.fromstring(http_get(url, attempts=2, backoff=8.0))
        if offset == 0:
            total_el = root.find("os:totalResults", NS)
            total = int(total_el.text) if total_el is not None and total_el.text else 0
            log("API 命中 %d 条（本次最多取 %d 条）" % (total, max_results))
        entries = root.findall("a:entry", NS)
        if not entries:
            break
        for node in entries:
            paper = parse_entry(node)
            if paper:
                papers.append(paper)
        offset += len(entries)
        if offset >= min(total, max_results) or offset >= max_results:
            break
        time.sleep(3.0)
    return papers, total


def parse_rss_item(node: ET.Element) -> dict | None:
    """rss.arxiv.org 的条目：description 里带 "arXiv:ID Announce Type: new  Abstract: ..."。"""
    import email.utils

    announce = node.findtext("{http://arxiv.org/schemas/atom}announce_type") or "new"
    if announce != "new":            # replace / cross 都不要：cross 会在别的分类里重复出现
        return None
    title = clean_text(node.findtext("title") or "")
    link = (node.findtext("link") or "").strip()
    desc = node.findtext("description") or ""
    match = re.search(r"arXiv:(\d{4}\.\d{4,5})(v\d+)?", desc) or re.search(r"abs/(\d{4}\.\d{4,5})", link)
    if not title or not match:
        return None
    base_id = match.group(1)
    abstract = ""
    if "Abstract:" in desc:
        abstract = clean_text(desc.split("Abstract:", 1)[1])
    cats = [c.text for c in node.findall("category") if c.text]
    creators = node.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
    authors = [a.strip() for a in creators.split(",") if a.strip()]
    published = None
    pub_raw = node.findtext("pubDate") or ""
    try:
        published = email.utils.parsedate_to_datetime(pub_raw)
    except (TypeError, ValueError):
        published = now_utc()
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    return {
        "id": base_id,
        "version": "",
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": published.isoformat(),
        "published_bj": published.astimezone(BEIJING).strftime("%m-%d %H:%M"),
        "primary": cats[0] if cats else "",
        "categories": cats,
        "comment": "",
        "journal_ref": "",
        "abs_url": "https://arxiv.org/abs/" + base_id,
        "pdf_url": "https://arxiv.org/pdf/" + base_id,
        "source": "arxiv",
        "venue_name": "",
    }


def fetch_rss(cfg: dict) -> list[dict]:
    """arXiv API 被限流时的兜底：每个分类拉一次当日公告 RSS。"""
    out: list[dict] = []
    for cat in cfg.get("categories") or []:
        url = ARXIV_RSS + cat
        try:
            raw = http_get(url, attempts=2, backoff=3.0)
            root = ET.fromstring(raw)
        except Exception as exc:
            log("RSS %s 失败：%s" % (cat, exc))
            continue
        n = 0
        for node in root.findall("./channel/item"):
            paper = parse_rss_item(node)
            if paper:
                out.append(paper)
                n += 1
        log("RSS %s：%d 条（只取 announce_type=new）" % (cat, n))
        time.sleep(3.0)
    return out


# ---------------------------------------------------------------- 期刊 / 会议（Semantic Scholar）

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def s2_bulk(venue: str, years: str, limit: int, timeout: int = 60, query: str = "*") -> dict:
    params = {
        "query": query,
        "year": years,
        "fields": "title,abstract,venue,year,publicationDate,externalIds,authors,url,citationCount",
    }
    if venue:
        params["venue"] = venue
    url = S2_BULK + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["data"] = (data.get("data") or [])[:limit]
    return data


def normalize_venue(item: dict, label: str) -> dict | None:
    ext = item.get("externalIds") or {}
    if ext.get("ArXiv"):
        return None                      # 有 arXiv 版本的，arXiv 源已经覆盖，别重复报
    title = clean_text(item.get("title") or "")
    if not title:
        return None
    doi = (ext.get("DOI") or "").strip()
    corpus = ext.get("CorpusId")
    pid = ("doi:" + doi.lower()) if doi else ("s2:" + str(corpus or title[:50]))
    published = item.get("publicationDate") or ""
    if not _ISO_DATE.match(published or ""):
        year = item.get("year") or 0
        published = "%04d-01-01" % int(year) if year else ""
    dt_obj = parse_dt(published + "T00:00:00Z") if published else None
    venue_name = item.get("venue") or label
    authors = [a.get("name", "").strip() for a in (item.get("authors") or [])]
    return {
        "id": pid,
        "version": "",
        "title": title,
        "abstract": clean_text(item.get("abstract") or ""),
        "authors": [a for a in authors if a],
        "published": dt_obj.isoformat() if dt_obj else "",
        "published_bj": dt_obj.astimezone(BEIJING).strftime("%m-%d") if dt_obj else "—",
        "primary": venue_name,
        "categories": [venue_name],
        "comment": "",
        "journal_ref": "",
        "abs_url": item.get("url") or ("https://doi.org/" + doi if doi else ""),
        "pdf_url": "",
        "source": "venue",
        "venue_name": venue_name,
        "venue_rank": "",
    }


def fetch_venues(cfg: dict) -> list[dict]:
    """按白名单逐会场抓最近发表，只保留 CCF B 及以上的会议/期刊。

    Semantic Scholar 免 key，但限流很紧：会场之间强制间隔，429 退避重试。
    """
    vc = cfg.get("venues") or {}
    if not vc.get("enabled"):
        return []
    entries = vc.get("list") or []
    if not entries:
        return []
    years = str(vc.get("years") or "2025-2026")
    per = int(vc.get("max_per_venue") or 20)
    gap = float(vc.get("request_gap_s") or 8.0)
    rank_order = {"A": 3, "B": 2, "C": 1}
    min_rank = rank_order.get(str(vc.get("min_ccf") or "B").upper(), 2)

    out: list[dict] = []
    for ent in entries:
        name = ent.get("s2") or ent.get("name")
        if not name:
            continue
        ccf = str(ent.get("ccf") or "").upper()
        if rank_order.get(ccf, 0) < min_rank:
            log("跳过 %s：CCF %s 低于门槛" % (ent.get("label") or name, ccf or "未定"))
            continue
        label = ent.get("label") or name
        data = None
        for attempt in range(2):
            try:
                data = s2_bulk(name, years, per)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = 6.0 * (attempt + 1)
                    log("Semantic Scholar 限流（%s），%.0fs 后重试" % (label, wait))
                    time.sleep(wait)
                    continue
                log("会场 %s 抓取失败：HTTP %d" % (label, exc.code))
                break
            except Exception as exc:
                log("会场 %s 抓取失败：%s" % (label, exc))
                break
        if data is None:
            continue
        got = 0
        sci = ent.get("sci") or ""
        rank_label = "CCF " + ccf + ((" · SCI " + sci) if sci else "")
        for item in (data.get("data") or []):
            paper = normalize_venue(item, label)
            if not paper:
                continue
            paper["venue_name"] = label
            paper["primary"] = label
            paper["venue_rank"] = rank_label
            out.append(paper)
            got += 1
        log("会场 %s（%s）：返回 %d 条，采用 %d 条"
            % (label, rank_label, len(data.get("data") or []), got))
        time.sleep(gap)
    log("期刊/会议合计 %d 条（只含 CCF %s 及以上）" % (out and len(out) or 0, vc.get("min_ccf") or "B"))
    return out


# ---------------------------------------------------------------- 精品文章（今年的顶会/顶刊）

def load_curated_seen() -> dict:
    if not CURATED_STATE.exists():
        return {}
    try:
        data = json.loads(CURATED_STATE.read_text(encoding="utf-8-sig"))
        ids = data.get("ids")
        return ids if isinstance(ids, dict) else {}
    except (OSError, ValueError):
        return {}


def save_curated_seen(ids: dict, keep_days: int = 400) -> None:
    cutoff = (now_utc() - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in ids.items() if isinstance(v, str) and v >= cutoff}
    CURATED_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURATED_STATE.with_name(CURATED_STATE.name + ".tmp")
    tmp.write_text(json.dumps({"updated": now_utc().isoformat(), "ids": kept},
                              ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, CURATED_STATE)


def _vkey(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def match_whitelist(venue_name: str, entries: list) -> dict | None:
    k = _vkey(venue_name)
    if not k:
        return None
    for ent in entries:
        for cand in (ent.get("label"), ent.get("s2")):
            ck = _vkey(cand)
            if ck and (ck == k or ck in k or k in ck):
                return ent
    return None


def fetch_curated(cfg: dict) -> list[dict]:
    """今年（默认 2026）的顶会/顶刊论文，按被引排序，只取没推过的。

    不受「最近 24 小时」限制：S2 的 bulk 查询一次拿 100 篇，再按白名单过滤。
    """
    cc = cfg.get("curated") or {}
    if not cc.get("enabled"):
        return []
    entries = (cfg.get("venues") or {}).get("list") or []
    if not entries:
        return []
    query = cc.get("query") or "LLM inference serving scheduling KV cache GPU"
    year = str(cc.get("year") or now_utc().year)
    pool = int(cc.get("pool") or 100)
    per_day = int(cc.get("per_day") or 2)
    min_cites = int(cc.get("min_citations") or 3)
    kw = [k.lower() for k in (cc.get("keywords") or [])]

    queries = cc.get("queries") or [query]
    items: list[dict] = []
    for q in queries:
        data = None
        for attempt in range(2):
            try:
                data = s2_bulk("", year, pool, query=q)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(12.0 * (attempt + 1))
                    continue
                log("精品文章查询失败（%s）：HTTP %d" % (q, exc.code))
                break
            except Exception as exc:
                log("精品文章查询失败（%s）：%s" % (q, exc))
                break
        if data:
            items.extend(data.get("data") or [])
            log("精品候选（%s）：%d 条" % (q, len(data.get("data") or [])))
        time.sleep(3.0)
    if not items:
        return []

    seen = load_curated_seen()
    picked: list[dict] = []
    seen_ids = set()
    for item in items:
        ent = match_whitelist(item.get("venue") or "", entries)
        if not ent:
            continue
        paper = normalize_venue(item, ent.get("label") or item.get("venue") or "")
        if not paper or paper["id"] in seen or paper["id"] in seen_ids:
            continue
        seen_ids.add(paper["id"])
        paper["venue_name"] = ent.get("label") or paper["venue_name"]
        paper["primary"] = paper["venue_name"]
        paper["venue_rank"] = "CCF " + str(ent.get("ccf") or "") + (
            (" · SCI " + str(ent.get("sci"))) if ent.get("sci") else "")
        paper["citations"] = int(item.get("citationCount") or 0)
        paper["curated"] = True
        if not paper.get("abstract"):
            continue                      # 没摘要的没法翻译，也会让模型说「内容缺失」
        if paper["citations"] < min_cites:
            continue                      # 精品文章要求至少被引几次，避免 0-1 引的凑数
        blob = (paper["title"] + " " + paper["abstract"]).lower()
        if kw and not any(k in blob for k in kw):
            continue                      # 主题不沾的直接排除
        picked.append(paper)
    picked.sort(key=lambda x: -int(x.get("citations") or 0))
    picked = picked[:per_day]
    if picked:
        stamp = now_utc().strftime("%Y-%m-%d")
        for paper in picked:
            seen[paper["id"]] = stamp
        save_curated_seen(seen)
        log("精品文章 %d 篇（今年，白名单会场，按被引排序）" % len(picked))
    return picked


# ---------------------------------------------------------------- 关键词粗排

def kw_pattern(term: str) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s\-_]+", term.strip()) if p]
    if not parts:
        return re.compile(r"(?!x)x")
    body = r"[\s\-_]+".join(parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:s|es)?(?![a-z0-9])", re.IGNORECASE)


class Scorer:
    def __init__(self, cfg: dict):
        self.research = [(t, float(w), kw_pattern(t))
                         for t, w in (cfg.get("research_keywords") or {}).items()]
        self.infra = [(t, kw_pattern(t)) for t in (cfg.get("infra_keywords") or [])]
        self.broad = [(t, kw_pattern(t)) for t in (cfg.get("broad_keywords") or [])]
        self.home_cats = set(cfg.get("categories") or [])

    def apply(self, paper: dict) -> dict:
        title = paper["title"].lower()
        body = paper["abstract"].lower()

        research_score = 0.0
        research_hits: list[str] = []
        for term, weight, pat in self.research:
            in_title = len(pat.findall(title))
            in_body = len(pat.findall(body))
            if in_title or in_body:
                research_score += weight * (3 * in_title + min(in_body, 3))
                research_hits.append(term)

        infra_score = 0.0
        infra_hits: list[str] = []
        for term, pat in self.infra:
            if pat.search(title) or pat.search(body):
                infra_score += 1.0
                infra_hits.append(term)

        broad_score = 0.0
        for _, pat in self.broad:
            if pat.search(title) or pat.search(body):
                broad_score += 1.0

        cat_bonus = 1.5 if paper.get("primary") in self.home_cats else 0.0
        paper["research_score"] = round(research_score, 2)
        paper["infra_score"] = round(infra_score, 2)
        paper["research_hits"] = research_hits[:4]
        paper["infra_hits"] = infra_hits[:4]
        paper["score"] = round(research_score * 1.0 + infra_score * 0.6 + broad_score * 0.8 + cat_bonus, 2)
        paper["excerpt"] = excerpt(paper["abstract"])
        paper["bucket"] = ""          # 由大模型或兜底规则填
        paper["rec"] = 0.0            # 阅读推荐指数 1-5，由大模型给
        paper["zh_title"] = ""
        paper["zh_summary"] = ""
        paper["why"] = ""
        return paper


# ---------------------------------------------------------------- 大模型：分类 + 翻译

def read_api_key(cfg: dict) -> str:
    """优先环境变量，其次本机 DSH 凭据文件里的 refs。绝不打印 key。"""
    env_name = (cfg.get("translate") or {}).get("api_key_env") or "DEEPSEEK_API_KEY"
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    path = (cfg.get("translate") or {}).get("credentials_file") or ""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    m = re.search(r"^\s*%s:\s*(\S+)\s*$" % re.escape(env_name), text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def llm_enrich(papers: list[dict], cfg: dict) -> dict[str, dict]:
    """一次请求：分类 + 中文标题 + 中文摘要 + 推荐理由。失败返回空 dict，调用方走兜底。"""
    tr = cfg.get("translate") or {}
    if not tr.get("enabled"):
        return {}
    key = read_api_key(cfg)
    if not key:
        log("没有可用的 API key，跳过翻译（仍会出英文标题的日报）")
        return {}
    base = (tr.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
    model = tr.get("model") or "deepseek-chat"
    max_chars = int(tr.get("max_abstract_chars") or 900)

    blocks = []
    for i, p in enumerate(papers, 1):
        blocks.append("[%d] 标题：%s\n主分类：%s\n摘要：%s" % (
            i, p["title"], p["primary"], p["abstract"][:max_chars]))
    prompt = (
        "你是 AI 领域的研究助理。下面是 %d 篇 arXiv 新论文。\n\n"
        "读者的研究方向：面向 AI 推理服务的网络感知异构算力调度"
        "（LLM 推理、KV 缓存、批处理、延迟、集群调度、网络带宽、异构算力）。\n"
        "读者的求职方向：AI Infra（推理引擎、GPU kernel 与算子优化、编译、量化压缩、"
        "分布式训练与并行、部署与性能工程）。\n\n"
        "请为每篇输出五项：\n"
        "1) bucket：\"research\"（与他的研究方向直接相关，例如调度、推理服务、KV cache、"
        "批处理、延迟/吞吐、集群与网络）、\"infra\"（属于 AI Infra 但更偏引擎/内核/编译/训练系统/部署）、"
        "\"other\"（都不沾）。\n"
        "2) zh_title：中文标题，准确简洁，专有名词保留英文原文。\n"
        "3) zh_summary：80-140 字中文摘要，说清「做了什么、怎么做的、关键结论」；"
        "摘要里没有的数字不要编，不要评价好坏。\n"
        "4) why：不超过 25 个字，说明和读者有什么关系（research 说与调度/网络/推理的关系，"
        "infra 说属于哪类基础设施）。\n"
        "5) score：阅读推荐指数，1 到 5 分，可以带一位小数，按下面的标准打分：\n"
        "   5 分＝直接命中他的研究方向或求职方向，而且摘要里能看到系统性方法或实验"
        "（例如新的调度策略、推理系统、KV 管理），今天不读会错过；\n"
        "   4 分＝明显相关，值得完整读一遍；\n"
        "   3 分＝沾边，扫一眼标题和摘要就够；\n"
        "   2 分＝关系弱，只是碰巧出现关键词；\n"
        "   1 分＝无关。\n"
        "   打分要克制：一天里 5 分最多给 1-2 篇，不要因为标题里有 LLM 就给高分。\n\n"
        "只输出 JSON 数组，元素形如 "
        "{\"i\":1,\"bucket\":\"research\",\"score\":4.2,\"zh_title\":\"…\","
        "\"zh_summary\":\"…\",\"why\":\"…\"}，不要输出任何其他文字。\n\n%s"
    ) % (len(papers), "\n\n".join(blocks))

    payload = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    t0 = time.time()
    items = None
    last_exc: object = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            match = re.search(r"\[.*\]", content, re.DOTALL)
            items = json.loads(match.group(0) if match else content)
            break
        except Exception as exc:          # 网络抖动 / 服务端断连，都重试
            last_exc = exc
            wait = 5 * (attempt + 1)
            log("大模型调用失败（第 %d/3 次）：%s；%ds 后重试" % (attempt + 1, exc, wait))
            time.sleep(wait)
    if items is None:
        log("大模型翻译/分类最终失败，本批退回英文摘要：%s" % last_exc)
        return {}

    out: dict[str, dict] = {}
    for item in items:
        try:
            idx = int(item.get("i", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(papers)):
            continue
        bucket = str(item.get("bucket", "")).strip().lower()
        if bucket not in ("research", "infra", "other"):
            bucket = "other"
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(1.0, min(5.0, score))
        out[papers[idx]["id"]] = {
            "bucket": bucket,
            "score": round(score, 1),
            "zh_title": str(item.get("zh_title", "")).strip(),
            "zh_summary": str(item.get("zh_summary", "")).strip(),
            "why": str(item.get("why", "")).strip(),
        }
    log("大模型处理 %d/%d 篇，用时 %.1fs" % (len(out), len(papers), time.time() - t0))
    return out


def classify_fallback(paper: dict, cfg: dict) -> str:
    """大模型不可用时的兜底分类：只靠关键词。"""
    threshold = float(cfg.get("research_threshold") or 8.0)
    if paper["research_score"] >= threshold:
        return "research"
    if paper["infra_score"] >= 2:
        return "infra"
    return "other"


def apply_enrichment(papers: list[dict], enrich: dict[str, dict], cfg: dict) -> None:
    for p in papers:
        info = enrich.get(p["id"])
        if info:
            p["bucket"] = info["bucket"]
            p["rec"] = float(info.get("score") or 0.0)
            p["zh_title"] = info["zh_title"]
            p["zh_summary"] = info["zh_summary"]
            p["why"] = info["why"]
        else:
            p["bucket"] = classify_fallback(p, cfg)
            # 没有翻译时，用英文标题 + 摘要前两句，至少保证信息量
            p["zh_title"] = ""
            p["zh_summary"] = ""
            p["why"] = ""
            p["rec"] = 0.0


def stars(rec: float) -> str:
    """4.2 -> ★★★★☆（四舍五入到整星，只用于显示）。"""
    full = int(float(rec) + 0.5) if rec else 0
    full = max(0, min(5, full))
    return "★" * full + "☆" * (5 - full)


# ---------------------------------------------------------------- 去重状态

def load_seen() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        ids = data.get("ids")
        return ids if isinstance(ids, dict) else {}
    except (OSError, ValueError):
        log("state/seen.json 损坏，按空状态处理（会多报一次全量）")
        return {}


def save_seen(seen: dict[str, str], keep_days: int = 21) -> None:
    cutoff = (now_utc() - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in seen.items() if isinstance(v, str) and v >= cutoff}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"updated": now_utc().isoformat(), "ids": kept},
                              ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 微信推送

def push_wechat(cfg: dict, text: str) -> bool:
    """写 outbox，由微信桥轮询发送。返回 0 只代表排队成功，真送达看 bridge.log。"""
    push = cfg.get("push") or {}
    if not push.get("enabled"):
        return False
    node = push.get("node") or "node"
    helper = push.get("script") or ""
    if not helper or not Path(helper).exists():
        log("微信推送跳过：找不到 wxpush.mjs（%s）" % helper)
        return False
    cmd = [node, helper, "--text", text]
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        log("微信推送失败：%s" % exc)
        return False
    if proc.returncode != 0:
        log("微信推送失败（exit %d）：%s %s" % (
            proc.returncode, (proc.stdout or "").strip()[:200], (proc.stderr or "").strip()[:200]))
        return False
    log("微信推送已入队：%s" % ((proc.stdout or "").strip()[:120] or "ok"))
    return True


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def pick_track(papers: list[dict], bucket: str, max_n: int, min_rec: float) -> list[dict]:
    """一条轨道里按推荐指数排序取前 max_n 篇；都不够分时退回分数最高的几篇（不推空消息）。"""
    items = [p for p in papers if p.get("bucket") == bucket]
    items.sort(key=lambda p: (-float(p.get("rec") or 0), -float(p.get("score") or 0),
                              p.get("published", "")))
    kept = [p for p in items if float(p.get("rec") or 0) >= min_rec]
    return (kept or items)[:max_n]


def render_push_entry(paper: dict, idx: int) -> list[str]:
    """一条论文 = 3 行：标题+评级 / 一句中文要点 / PDF 直链。

    不缩进、不用 "1." 编号：微信会把行首空格吞掉、把 "1." 当有序列表重排。
    """
    rec = float(paper.get("rec") or 0)
    title = clip(paper.get("zh_title") or paper["title"], 34)
    head = "%d｜%s" % (idx, title)
    if rec:
        head += " %s %.1f" % (stars(rec), rec)
    raw = paper.get("zh_summary") or paper.get("why") or ""
    summary = clip(raw, 62)
    for sep in ("。", "；"):          # 尽量不切在句子中间
        if sep in summary:
            cut = summary.rfind(sep)
            if 20 < cut < len(summary) - 1:
                summary = summary[:cut + 1]
                break
    lines = [head]
    if summary:
        lines.append("讲：" + summary)
    if paper.get("pdf_url"):
        lines.append("pdf：" + paper["pdf_url"])
    elif paper.get("abs_url"):
        lines.append("链接：" + paper["abs_url"])
    return lines


def build_push_messages(papers, cfg, date_str, window_start, window_end, curated=None) -> list[str]:
    if curated is None:
        curated = []
    """一天只推一条，硬上限 1450 字（桥按 1500 字切，切一次就是一条消息）。"""
    tracks = cfg.get("tracks") or {}
    a_name = (tracks.get("research") or {}).get("name", "你的方向")
    b_name = (tracks.get("infra") or {}).get("name", "AI Infra")
    a_max = int((tracks.get("research") or {}).get("max", 4))
    b_max = int((tracks.get("infra") or {}).get("max", 4))
    min_rec = float((cfg.get("scoring") or {}).get("min_recommend") or 3.5)

    curated_ids = {p.get("id") for p in curated} if curated else set()
    pool = [p for p in papers if p.get("id") not in curated_ids]
    group_a = pick_track(pool, "research", a_max, min_rec)
    group_b = pick_track(pool, "infra", b_max, min_rec)
    total_pick = len(group_a) + len(group_b) + len(curated)

    head = "arXiv 日报 %s（%s）｜新增 %d 篇｜推荐 %d 篇" % (
        date_str, WEEKDAYS[dt.date.fromisoformat(date_str).weekday()], len(papers), total_pick)

    chunks: list[str] = []
    if group_a:
        chunks.append(("section", "【%s】" % a_name))
        for i, paper in enumerate(group_a, 1):
            chunks.append(("entry", "\n".join(render_push_entry(paper, i))))
    if curated:
        chunks.append(("section", "【精品文章 · 今年顶会/顶刊】"))
        for i, paper in enumerate(curated, len(group_a) + 1):
            block = render_push_entry(paper, i)
            block.append("来源：%s（%s）被引 %d" % (
                paper.get("venue_name") or "?", paper.get("venue_rank") or "顶会/顶刊",
                int(paper.get("citations") or 0)))
            chunks.append(("entry", "\n".join(block)))
        offset = len(group_a) + len(curated)
    else:
        offset = len(group_a)
    if group_b:
        chunks.append(("section", "【%s】" % b_name))
        for i, paper in enumerate(group_b, offset + 1):
            chunks.append(("entry", "\n".join(render_push_entry(paper, i))))

    limit = 1380          # 桥按 1500 字切，留足余量，保证一条发完
    body = head
    shown = 0
    for kind, text in chunks:
        add = "\n\n" + text          # 每一条之间空一行
        if len(body) + len(add) > limit:
            break
        body += add
        if kind == "entry":
            shown += 1
    if shown < total_pick:
        tail = "（共筛出 %d 篇，这里放不下 %d 篇，完整版在电脑 briefs/ 目录）" % (
            total_pick, total_pick - shown)
        if len(body) + len(tail) + 2 <= limit:
            body += "\n\n" + tail
    return [body]
    return ["\n".join(out)]


# ---------------------------------------------------------------- 输出

def chinese_date(d: dt.datetime) -> str:
    return "%d 年 %d 月 %d 日 %s" % (d.year, d.month, d.day, WEEKDAYS[d.weekday()])


def render_paper(paper: dict, idx: int) -> list[str]:
    title_cn = paper.get("zh_title") or ""
    lines = ["### %d. %s" % (idx, title_cn or paper["title"]), ""]
    if title_cn:
        lines.append("英文原名：%s" % paper["title"])
        lines.append("")
    if paper.get("source") == "venue":
        rank = paper.get("venue_rank") or ""
        meta = ["期刊/会议：%s%s" % (paper.get("venue_name") or "?",
                                     ("（%s）" % rank) if rank else ""), paper["published_bj"]]
    else:
        meta = ["`%s`" % (paper["primary"] or "?"), paper["published_bj"] + " 北京"]
    others = [c for c in paper["categories"] if c != paper["primary"]]
    if others:
        meta.append("cross-list: " + ", ".join(others[:4]))
    lines.append(" · ".join(meta))
    lines.append("")

    authors = paper["authors"]
    if authors:
        shown = ", ".join(authors[:3])
        if len(authors) > 3:
            shown += " 等（共 %d 人）" % len(authors)
    else:
        shown = "未提供"
    lines.append("作者：%s" % shown)
    lines.append("")
    rec = float(paper.get("rec") or 0)
    if rec:
        lines.append("**阅读推荐指数**：%s %.1f / 5" % (stars(rec), rec))
        lines.append("")
    link_bits = []
    if paper.get("abs_url"):
        link_bits.append("[详情页](%s)" % paper["abs_url"])
    if paper.get("pdf_url"):
        link_bits.append("[PDF](%s)" % paper["pdf_url"])
    link_bits.append("编号: %s" % paper["id"])
    lines.append(" · ".join(link_bits))
    lines.append("")
    if paper.get("zh_summary"):
        lines.append("**中文摘要**：%s" % paper["zh_summary"])
        lines.append("")
    if paper.get("why"):
        lines.append("**为什么推给你**：%s" % paper["why"])
        lines.append("")
    hits = paper.get("research_hits") or paper.get("infra_hits")
    if hits:
        lines.append("命中关键词：%s" % "、".join(hits[:4]))
        lines.append("")
    if not paper.get("zh_summary"):
        lines.append("英文摘要摘录：%s" % paper["excerpt"])
        lines.append("")
    return lines


def render_brief(papers: list[dict], cfg: dict, window_start: dt.datetime, window_end: dt.datetime,
                 total_in_window: int, skipped_seen: int, baseline: bool, translated: bool,
                 curated: list = None) -> str:
    curated = curated or []
    now_bj = now_utc().astimezone(BEIJING)
    tracks = cfg.get("tracks") or {}
    a_name = (tracks.get("research") or {}).get("name", "你的方向")
    b_name = (tracks.get("infra") or {}).get("name", "AI Infra")
    a_max = int((tracks.get("research") or {}).get("max", 4))
    b_max = int((tracks.get("infra") or {}).get("max", 4))
    min_rec = float((cfg.get("scoring") or {}).get("min_recommend") or 3.5)

    curated_ids = {p.get("id") for p in curated} if curated else set()
    pool = [p for p in papers if p.get("id") not in curated_ids]
    group_a = pick_track(pool, "research", a_max, min_rec)
    group_b = pick_track(pool, "infra", b_max, min_rec)
    shown_ids = {p["id"] for p in group_a + group_b}
    others = [p for p in papers if p["id"] not in shown_ids][:8]

    out = ["# arXiv 日报 · %s" % chinese_date(now_bj), ""]
    scope = " / ".join(cfg.get("categories", []))
    vcfg = cfg.get("venues") or {}
    if vcfg.get("enabled") and vcfg.get("names"):
        scope += "｜期刊会议：" + "、".join(vcfg.get("names")[:6])
    out.append("> 抓取范围：%s" % scope)
    out.append("> 时间窗：%s → %s（北京时间）" % (
        window_start.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M"),
        window_end.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")))
    out.append("> 本期新增 **%d** 篇（窗口内共 %d 篇，已跳过看过的 %d 篇）｜生成于 %s" % (
        len(papers), total_in_window, skipped_seen, now_bj.strftime("%Y-%m-%d %H:%M")))
    if not translated:
        out.append("> 注意：这次没有调用大模型（无 key 或调用失败），标题和摘要是英文原文。")
    if baseline:
        out.append("> 说明：第一次运行，先把窗口内论文记为已读作为基线，明天起只报新增。")
    out.append("")

    if papers:
        counts = {"research": sum(1 for p in papers if p.get("bucket") == "research"),
                  "infra": sum(1 for p in papers if p.get("bucket") == "infra"),
                  "other": sum(1 for p in papers if p.get("bucket") == "other")}
        out.append("## 今日速览")
        out.append("")
        out.append("- 你的方向（调度 / 推理服务 / KV 缓存）：%d 篇" % counts["research"])
        out.append("- AI Infra（引擎 / GPU / 编译 / 部署）：%d 篇" % counts["infra"])
        out.append("- 其他：%d 篇" % counts["other"])
        out.append("")
        out.append("- **推荐指数怎么读**：1-5 分，模型按「和你的研究/求职方向有多相关」+"
                   "「摘要里有没有系统方法或实验」打分。5 分一天最多 1-2 篇；"
                   "3 分左右＝扫一眼标题就够；不到 3.5 分的不进微信推荐。")
        out.append("")

    out.append("## 一、%s（%d 篇）" % (a_name, len(group_a)))
    out.append("")
    if group_a:
        for i, p in enumerate(group_a, 1):
            out.extend(render_paper(p, i))
    else:
        out.append("今天没有和你的研究方向直接相关的论文。")
        out.append("")

    out.append("## 二、%s（%d 篇）" % (b_name, len(group_b)))
    out.append("")
    if group_b:
        for i, p in enumerate(group_b, len(group_a) + 1):
            out.extend(render_paper(p, i))
    else:
        out.append("今天没有明显属于 AI Infra 的论文。")
        out.append("")

    if curated:
        out.append("## 三、精品文章（今年 · 顶会/顶刊，按被引排序）")
        out.append("")
        for i, paper in enumerate(curated, 1):
            out.extend(render_paper(paper, i))
            out.append("入选理由：今年发表在 %s（%s），被引 %d 次。"
                       % (paper.get("venue_name") or "?", paper.get("venue_rank") or "顶会/顶刊",
                          int(paper.get("citations") or 0)))
            out.append("")

    if others:
        out.append("## 四、其他（仅列标题，没进推荐）")
        out.append("")
        for p in others:
            out.append("- %s｜`%s`｜%s" % (
                p.get("zh_title") or p["title"], p["primary"], p["abs_url"]))
        out.append("")

    out.append("---")
    out.append("")
    out.append("## 怎么用")
    out.append("")
    out.append("1. 先看第一节：和你的论文直接相关的，点 PDF，重点看它和哪些基线比、在什么负载下。")
    out.append("2. 第二节是求职向：AI Infra 的东西不一定马上能引用，但面试常问，扫一遍标题有印象就行。")
    out.append("3. 手机上只看微信那条就够；这份文件是完整版，回电脑再看。")
    out.append("")
    return "\n".join(out)


def render_empty_brief(cfg: dict, window_start: dt.datetime, window_end: dt.datetime,
                       total_in_window: int) -> str:
    now_bj = now_utc().astimezone(BEIJING)
    return "\n".join([
        "# arXiv 日报 · %s" % chinese_date(now_bj),
        "",
        "> 抓取范围：%s" % " / ".join(cfg.get("categories", [])),
        "> 时间窗：%s → %s（北京时间）" % (
            window_start.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M"),
            window_end.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")),
        "> 本期无新增论文（时间窗内共 %d 篇，全部是已经报过的）。" % total_in_window,
        "",
        "## 为什么可能是空的",
        "",
        "- arXiv 只在美东时间周日到周四晚 20:00 公告新一批；周五、周六晚上不公告，",
        "  所以周六、周日早上（北京时间）的日报大概率是空的，这是正常现象。",
        "- 美国假期、arXiv 维护日也会停更。",
        "- 连续几天都空就看 `logs/run.log`；也可以手动跑 `--no-state` 看窗口内到底抓到多少篇。",
        "",
    ])


def merge_previous(json_path: Path, papers: list[dict]) -> list[dict]:
    if not json_path.exists():
        return papers
    try:
        old = json.loads(json_path.read_text(encoding="utf-8-sig")).get("papers", [])
    except (OSError, ValueError):
        return papers
    have = {p["id"] for p in papers}
    for p in old:
        if p.get("id") and p["id"] not in have:
            papers.append(p)
    papers.sort(key=lambda p: (-p.get("score", 0.0), p.get("published", "")))
    return papers


# ---------------------------------------------------------------- 主流程

def run(args: argparse.Namespace) -> int:
    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("配置文件读不了或不是合法 JSON：%s（%s）" % (args.config, exc))

    window_hours = args.window_hours if args.window_hours is not None else float(cfg.get("window_hours", 30))
    max_results = args.max_results if args.max_results is not None else int(cfg.get("max_results", 1200))
    out_dir = Path(args.out_dir) if args.out_dir else BRIEF_DIR

    end = now_utc()
    start = end - dt.timedelta(hours=window_hours)
    log("开始：窗口 %s → %s（UTC）" % (start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")))

    try:
        papers, total = fetch_window(cfg, start, end, max_results)
    except Exception as exc:
        # arXiv 的 API 会周期性限流（429），RSS 走的是另一套服务，通常还能用
        log("arXiv API 取不到（%s），改用 RSS 当日公告兜底" % exc)
        papers = fetch_rss(cfg)
        total = len(papers)
        if not papers:
            raise
    if not papers:
        # API 有时会「200 但 0 条」（限流期间见过），这时也用 RSS 兜底
        log("API 返回 0 条，改用 RSS 当日公告兜底")
        papers = fetch_rss(cfg)
        total = len(papers)
    log("arXiv 解析出 %d 篇" % len(papers))

    curated: list[dict] = []
    if not args.no_venues:
        venue_papers = fetch_venues(cfg)
        papers = papers + venue_papers

    if not args.no_curated:
        curated = fetch_curated(cfg)
        papers = papers + curated

    unique: dict[str, dict] = {}
    for p in papers:
        unique.setdefault(p["id"], p)
    papers = list(unique.values())

    # 标题级去重：同一篇既有 DOI 记录又有 arXiv 记录时，留 arXiv 那条（有 PDF）
    by_title: dict[str, dict] = {}
    for p in papers:
        key = re.sub(r"[^a-z0-9]", "", p["title"].lower())[:120]
        if not key:
            continue
        cur = by_title.get(key)
        if cur is None or (cur.get("source") == "venue" and p.get("source") == "arxiv"):
            by_title[key] = p
    papers = list(by_title.values())

    seen = {} if args.no_state else load_seen()
    baseline = not STATE_PATH.exists()
    fresh = [p for p in papers if p["id"] not in seen]
    skipped = len(papers) - len(fresh)
    log("新增 %d 篇，跳过看过的 %d 篇" % (len(fresh), skipped))

    if args.dry_run:
        log("dry-run：不落盘、不调大模型")
        for p in fresh[:5]:
            log("  sample: %s | %s" % (p["id"], p["title"][:70]))
        return 0

    scorer = Scorer(cfg)
    scored = sorted((scorer.apply(p) for p in fresh), key=lambda p: (-p["score"], p["published"]))
    listed = scored[: int(cfg.get("max_list", 200))]

    date_str = end.astimezone(BEIJING).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / (date_str + ".json")
    md_path = out_dir / (date_str + ".md")

    if not listed:
        if md_path.exists() and not args.force:
            log("本期无新增，当天日报已存在，保持原文件不动（--force 可覆盖成空日报）")
            return 0
        md_path.write_text(render_empty_brief(cfg, start, end, total), encoding="utf-8")
        log("本期无新增 → 写了空日报：%s" % md_path)
        if not args.no_push and (cfg.get("push") or {}).get("send_empty"):
            push_wechat(cfg, "arXiv 日报 %s：本期没有新增论文。周末、美国假期 arXiv 不公告，属正常。"
                        % date_str)
        return 0

    # 只把粗排最像的 N 篇交给大模型（默认 20 篇），控制花费
    max_cand = int((cfg.get("translate") or {}).get("max_papers") or 20)
    candidates = sorted(listed, key=lambda p: -(p["score"]))[:max_cand]
    curated_ids = {p["id"] for p in curated}
    for paper in listed:
        if paper["id"] in curated_ids and paper not in candidates:
            candidates.append(paper)
    enrich = llm_enrich(candidates, cfg)
    apply_enrichment(listed, enrich, cfg)
    translated = bool(enrich)

    # 分轨后再排一次：轨道内按推荐指数降序，其次才看关键词分
    listed.sort(key=lambda p: (
        0 if p["bucket"] == "research" else (1 if p["bucket"] == "infra" else 2),
        -float(p.get("rec") or 0), -float(p.get("score") or 0), p["published"]))

    listed = merge_previous(json_path, listed)

    payload = {
        "date": date_str,
        "generated_at": now_utc().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_in_window": total,
        "new_count": len(listed),
        "translated": translated,
        "categories": cfg.get("categories", []),
        "papers": listed,
    }
    tmp_json = json_path.with_name(json_path.name + ".tmp")
    tmp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp_json, json_path)

    md = render_brief(listed, cfg, start, end, total, skipped, baseline, translated, curated)
    tmp_md = md_path.with_name(md_path.name + ".tmp")
    tmp_md.write_text(md, encoding="utf-8")
    os.replace(tmp_md, md_path)

    if not args.no_push:
        if translated:
            for msg in build_push_messages(listed, cfg, date_str, start, end, curated):
                push_wechat(cfg, msg)
                time.sleep(2.0)      # 桥 5 秒轮询一次，两条之间留间隔，避免顺序乱
        else:
            # 翻译失败时不推英文长文（手机上没法看），只推一句说明。
            push_wechat(cfg, "arXiv 日报 %s：抓到 %d 篇，但翻译接口这次没通，"
                             "英文版已存在电脑，稍后会自动重试。" % (date_str, len(listed)))

    if not args.no_state and translated:
        for p in scored:
            seen.setdefault(p["id"], date_str)
        save_seen(seen)
    elif not translated:
        # 不更新台账，让 09:10 那次（或下次手动跑）拿同一批重试翻译
        log("翻译失败：本次不更新去重台账，下一次运行会重试同一批")

    log("完成：新增 %d 篇 → %s" % (len(listed), md_path))
    if args.open:
        try:
            os.startfile(str(md_path))       # noqa: S606
        except Exception as exc:
            log("打开日报失败：%s" % exc)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 arXiv 大模型 / AI Infra 每日简报")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--window-hours", type=float, default=None, help="往回看多少小时（默认取配置）")
    parser.add_argument("--max-results", type=int, default=None, help="最多抓多少条（默认取配置）")
    parser.add_argument("--out-dir", default=None, help="日报输出目录")
    parser.add_argument("--no-state", action="store_true", help="不去重、不写状态（看全量）")
    parser.add_argument("--force", action="store_true", help="没有新增也生成日报")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和统计，不落盘不调模型")
    parser.add_argument("--no-push", action="store_true", help="不推微信")
    parser.add_argument("--no-venues", action="store_true", help="跳过期刊/会议，只抓 arXiv")
    parser.add_argument("--no-curated", action="store_true", help="跳过精品文章")
    parser.add_argument("--open", action="store_true", help="生成后打开日报")
    args = parser.parse_args(argv)

    try:
        return run(args)
    except Exception as exc:
        import traceback
        log("运行失败：%s" % exc)
        log(traceback.format_exc())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
