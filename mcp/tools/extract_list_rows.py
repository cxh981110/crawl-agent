import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from agent.config import build_schema
from agent.models import FieldRule
from agent.normalize import coerce_value, normalize_text

from .page_pipeline import fetch_and_clean_page


def _infer_field_kind(field_name: str, rule: FieldRule) -> str:
    joined = " ".join([field_name] + list(rule.labels or []))
    n = normalize_text(joined)

    if any(token in n for token in ["url", "link", "href", "\u94fe\u63a5", "\u7f51\u5740"]):
        return "url"
    if any(token in n for token in ["date", "time", "\u65e5\u671f", "\u65f6\u95f4", "\u53d1\u5e03"]):
        return "date"
    if any(token in n for token in ["title", "name", "\u6807\u9898", "\u540d\u79f0", "\u516c\u544a\u540d"]):
        return "title"
    return "generic"


def _extract_date(text: str, custom_regex: str = "") -> Optional[str]:
    patterns = []
    if custom_regex:
        patterns.append(custom_regex)
    patterns.extend(
        [
            r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}",
            r"\d{4}\u5e74\d{1,2}\u6708\d{1,2}\u65e5",
        ]
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(0)
        normalized = (
            raw.replace("\u5e74", "-")
            .replace("\u6708", "-")
            .replace("\u65e5", "")
            .replace("/", "-")
            .replace(".", "-")
        )
        parts = normalized.split("-")
        if len(parts) == 3:
            try:
                y = int(parts[0])
                m = int(parts[1])
                d = int(parts[2])
                return "{:04d}-{:02d}-{:02d}".format(y, m, d)
            except Exception:
                return raw
        return raw
    return None


def _extract_by_label_value(text: str, labels: List[str]) -> Optional[str]:
    for label in labels:
        if not label:
            continue
        escaped = re.escape(label)
        match = re.search(rf"{escaped}\s*[:\uFF1A]\s*(.+)", text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).splitlines()[0].strip()
            if value:
                return value
    return None


def _find_candidate_nodes(soup: BeautifulSoup, list_selector: str, item_selector: str) -> List[Any]:
    if item_selector:
        return list(soup.select(item_selector))

    if list_selector:
        containers = soup.select(list_selector)
        nodes: List[Any] = []
        for container in containers:
            children = container.select("li, tr, article, div, p")
            if children:
                nodes.extend(children)
            else:
                nodes.append(container)
        return nodes

    return list(soup.select("li, tr, article, div, p"))


def _extract_row_from_node(
    node: Any,
    schema: Dict[str, FieldRule],
    base_url: str,
    date_regex: str,
) -> Dict[str, Any]:
    anchors = node.select("a[href]")
    first_anchor = anchors[0] if anchors else None
    href = first_anchor.get("href", "").strip() if first_anchor else ""
    anchor_text = first_anchor.get_text(" ", strip=True) if first_anchor else ""
    full_text = node.get_text(" ", strip=True)

    row: Dict[str, Any] = {}
    for field_name, rule in schema.items():
        kind = _infer_field_kind(field_name, rule)
        value: Any = None

        if kind == "url" and href:
            value = urljoin(base_url, href)
        elif kind == "title" and anchor_text:
            value = anchor_text
        elif kind == "date":
            value = _extract_date(full_text, date_regex)
        else:
            value = _extract_by_label_value(full_text, rule.labels)

        if value in (None, "", []):
            # Fallbacks for generic fields frequently seen in announcement lists.
            if kind == "title" and full_text:
                value = full_text[:120]
            elif kind == "url" and href:
                value = urljoin(base_url, href)
            elif kind == "date":
                value = _extract_date(full_text, date_regex)

        row[field_name] = coerce_value(value, rule.field_type)

    return row


def extract_list_rows(
    url: str,
    row_schema_json: str,
    list_selector: str = "",
    item_selector: str = "",
    date_regex: str = "",
    max_items: int = 100,
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
    auto_paginate: bool = True,
    max_pages: int = 20,
    next_selector: str = "",
) -> str:
    raw_schema = json.loads(row_schema_json)
    schema = build_schema(raw_schema)

    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        actions_json=actions_json,
        timeout=timeout,
        auto_paginate=auto_paginate,
        max_pages=max_pages,
        next_selector=next_selector,
    )
    base_url = page_payload.get("url", url)
    soup = BeautifulSoup(page_payload.get("cleaned_html", ""), "lxml")

    candidates = _find_candidate_nodes(soup, list_selector=list_selector, item_selector=item_selector)
    rows: List[Dict[str, Any]] = []
    seen = set()

    for node in candidates:
        if len(rows) >= max_items:
            break
        if not node.select("a[href]"):
            continue

        row = _extract_row_from_node(node=node, schema=schema, base_url=base_url, date_regex=date_regex)
        non_empty = sum(1 for value in row.values() if value not in (None, "", []))
        if non_empty == 0:
            continue

        key = tuple(str(row.get(field, "")) for field in schema.keys())
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    missing_fields = []
    for field_name in schema.keys():
        if not any(item.get(field_name) not in (None, "", []) for item in rows):
            missing_fields.append(field_name)

    response = {
        "url": base_url,
        "title": page_payload.get("title"),
        "data": rows,
        "missing_fields": missing_fields,
        "evidence": {
            "candidate_count": len(candidates),
            "row_count": len(rows),
            "page_count": page_payload.get("page_count", 1),
            "pages": page_payload.get("pages", []),
            "list_selector": list_selector,
            "item_selector": item_selector,
        },
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
