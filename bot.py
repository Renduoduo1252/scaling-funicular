#!/usr/bin/env python3
"""
Daily crawler for China AI startup financing and investment news.

The script uses public Google News RSS search feeds, keeps a local JSON
datastore, and deduplicates by canonical URL plus title fingerprint. It uses
only the Python standard library so it can run directly in GitHub Actions.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


DATA_FILE = Path("data.json")
MAX_ITEMS_PER_RUN = 80
REQUEST_TIMEOUT = 20
REQUEST_DELAY_RANGE = (1.5, 4.5)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]

QUERY_TERMS = [
    "中国 AI 初创公司 融资 投资",
    "中国 人工智能 初创 企业 融资",
    "AI 创业公司 获 投资 中国",
    "大模型 初创公司 融资 中国",
    "生成式 AI 公司 融资 中国",
    "中国 AI startup funding investment",
    "China AI startup raises funding",
]

POSITIVE_KEYWORDS = [
    "ai",
    "人工智能",
    "大模型",
    "生成式",
    "智能体",
    "agent",
    "初创",
    "创业",
    "融资",
    "投资",
    "领投",
    "跟投",
    "天使轮",
    "种子轮",
    "pre-a",
    "a轮",
    "b轮",
    "c轮",
    "完成",
    "获投",
]

INVESTMENT_KEYWORDS = [
    "融资",
    "投资",
    "领投",
    "跟投",
    "获投",
    "筹集",
    "raises",
    "raised",
    "funding",
    "investment",
    "financing",
]

CHINA_KEYWORDS = [
    "中国",
    "北京",
    "上海",
    "深圳",
    "杭州",
    "广州",
    "成都",
    "南京",
    "china",
    "chinese",
]

PRODUCT_PATTERNS = [
    ("大模型", ["大模型", "llm", "large language model"]),
    ("AI Agent", ["智能体", "agent", "ai agent"]),
    ("生成式AI应用", ["生成式", "aigc", "文生", "图像生成", "视频生成"]),
    ("AI基础设施", ["算力", "芯片", "gpu", "基础设施", "infra", "云"]),
    ("企业AI软件", ["企业", "办公", "协同", "crm", "saas", "软件"]),
    ("机器人", ["机器人", "具身智能", "embodied", "robot"]),
    ("自动驾驶", ["自动驾驶", "智能驾驶", "adas"]),
    ("AI医疗产品", ["医疗", "医药", "诊断", "药物"]),
    ("AI教育产品", ["教育", "学习", "教培"]),
]

INDUSTRY_PATTERNS = [
    ("人工智能", ["人工智能", "ai", "大模型", "生成式"]),
    ("企业服务", ["企业", "saas", "办公", "协同", "crm"]),
    ("半导体/算力", ["芯片", "算力", "gpu", "半导体"]),
    ("机器人/具身智能", ["机器人", "具身智能", "embodied", "robot"]),
    ("汽车/自动驾驶", ["汽车", "自动驾驶", "智能驾驶", "adas"]),
    ("医疗健康", ["医疗", "医药", "诊断", "药物", "健康"]),
    ("教育科技", ["教育", "学习", "教培"]),
    ("金融科技", ["金融", "投研", "风控", "保险"]),
    ("内容/媒体", ["内容", "营销", "视频", "图像", "传媒"]),
]


def main() -> int:
    existing_items = load_existing_items(DATA_FILE)
    seen_keys = build_seen_keys(existing_items)

    print(f"Loaded {len(existing_items)} existing records.")
    new_items: list[dict[str, Any]] = []

    for feed_url in build_feed_urls():
        try:
            feed_items = fetch_rss_items(feed_url)
        except Exception as exc:
            print(f"Fetch failed: {feed_url} ({exc})", file=sys.stderr)
            traceback.print_exc()
            continue

        for item in feed_items:
            normalized = normalize_item(item)
            if not normalized or not is_relevant(normalized):
                continue

            item_keys = make_dedupe_keys(normalized)
            if item_keys & seen_keys:
                continue

            normalized["id"] = stable_id(normalized)
            normalized["product"] = classify_text(
                normalized["title"] + " " + normalized["summary"],
                PRODUCT_PATTERNS,
                "AI相关产品",
            )
            normalized["industry"] = classify_text(
                normalized["title"] + " " + normalized["summary"],
                INDUSTRY_PATTERNS,
                "人工智能",
            )
            normalized["collected_at"] = utc_now_iso()

            new_items.append(normalized)
            seen_keys.update(item_keys)

            if len(new_items) >= MAX_ITEMS_PER_RUN:
                break

        if len(new_items) >= MAX_ITEMS_PER_RUN:
            break

        sleep_politely()

    all_items = sorted(new_items + existing_items, key=sort_key, reverse=True)
    save_items(DATA_FILE, all_items)

    print(f"Added {len(new_items)} new records. Total: {len(all_items)}.")
    return 0


def build_feed_urls() -> list[str]:
    urls = []
    for query in QUERY_TERMS:
        params = urllib.parse.urlencode(
            {
                "q": query,
                "hl": "zh-CN",
                "gl": "CN",
                "ceid": "CN:zh-Hans",
            }
        )
        urls.append(f"https://news.google.com/rss/search?{params}")
    return urls


def fetch_rss_items(url: str) -> list[dict[str, str]]:
    body = fetch_url(url)
    root = ElementTree.fromstring(body)
    items: list[dict[str, str]] = []

    for item in root.findall("./channel/item"):
        source = ""
        source_node = item.find("source")
        if source_node is not None and source_node.text:
            source = clean_text(source_node.text)

        items.append(
            {
                "title": get_xml_text(item, "title"),
                "link": get_xml_text(item, "link"),
                "published_at": get_xml_text(item, "pubDate"),
                "summary": get_xml_text(item, "description"),
                "source": source,
            }
        )

    return items


def fetch_url(url: str) -> bytes:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Cache-Control": "no-cache",
    }
    request = urllib.request.Request(url, headers=headers)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            backoff = (2**attempt) + random.uniform(0.5, 2.0)
            time.sleep(backoff)

    raise RuntimeError(f"request failed after retries: {last_error}")


def normalize_item(item: dict[str, str]) -> dict[str, Any] | None:
    title = clean_text(item.get("title", ""))
    link = canonicalize_url(item.get("link", ""))
    summary = clean_text(item.get("summary", ""))
    published_at = parse_datetime(item.get("published_at", ""))
    source = clean_text(item.get("source", ""))

    if not title or not link:
        return None

    return {
        "title": title,
        "link": link,
        "published_at": published_at,
        "summary": summary,
        "product": "",
        "industry": "",
        "source": source,
    }


def is_relevant(item: dict[str, Any]) -> bool:
    text = (item["title"] + " " + item["summary"]).lower()
    has_ai_context = any(keyword.lower() in text for keyword in POSITIVE_KEYWORDS)
    has_investment = any(keyword.lower() in text for keyword in INVESTMENT_KEYWORDS)
    has_china_context = any(keyword.lower() in text for keyword in CHINA_KEYWORDS)
    return has_ai_context and has_investment and has_china_context


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def canonicalize_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    parsed = urllib.parse.urlparse(url)

    if parsed.netloc == "news.google.com" and parsed.path.startswith("/rss/articles/"):
        query = urllib.parse.parse_qs(parsed.query)
        if "url" in query and query["url"]:
            url = query["url"][0]
            parsed = urllib.parse.urlparse(url)

    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    filtered_query = [
        (key, value)
        for key, value in query_pairs
        if not key.lower().startswith("utm_")
        and key.lower()
        not in {"from", "spm", "campaign", "source", "ref", "fbclid", "gclid"}
    ]

    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower() or "https",
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            urllib.parse.urlencode(filtered_query),
            "",
        )
    )


def parse_datetime(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return value


def load_existing_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        backup = path.with_suffix(".json.bak")
        try:
            backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except OSError:
            pass
        print(f"Could not parse {path}; old content backed up to {backup}: {exc}", file=sys.stderr)
        return []

    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return [item for item in raw["items"] if isinstance(item, dict)]
    return []


def save_items(path: Path, items: list[dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def build_seen_keys(items: list[dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    for item in items:
        seen.update(make_dedupe_keys(item))
    return seen


def make_dedupe_keys(item: dict[str, Any]) -> set[str]:
    keys = set()
    link = canonicalize_url(str(item.get("link", "")))
    title = normalize_for_key(str(item.get("title", "")))

    if link:
        keys.add(f"url:{link}")
    if title:
        keys.add(f"title:{title}")

    return keys


def stable_id(item: dict[str, Any]) -> str:
    link = canonicalize_url(str(item.get("link", "")))
    title = normalize_for_key(str(item.get("title", "")))
    return hashlib.sha256(f"{link}|{title}".encode("utf-8")).hexdigest()[:16]


def normalize_for_key(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value


def classify_text(
    text: str,
    patterns: list[tuple[str, list[str]]],
    fallback: str,
) -> str:
    lowered = text.lower()
    for label, keywords in patterns:
        if any(keyword.lower() in lowered for keyword in keywords):
            return label
    return fallback


def get_xml_text(node: ElementTree.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text


def sort_key(item: dict[str, Any]) -> str:
    published_at = str(item.get("published_at") or "")
    collected_at = str(item.get("collected_at") or "")
    return published_at or collected_at


def sleep_politely() -> None:
    time.sleep(random.uniform(*REQUEST_DELAY_RANGE))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
