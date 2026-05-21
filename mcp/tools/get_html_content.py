import json
from typing import Any

from bs4 import BeautifulSoup

from .page_pipeline import fetch_and_clean_page


def _extract_tables(soup: BeautifulSoup, max_tables: int = 10, max_rows_per_table: int = 200) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if cells and any(cells):
                rows.append(cells)
            if len(rows) >= max_rows_per_table:
                break
        if rows:
            tables.append(rows)
        if len(tables) >= max_tables:
            break
    return tables


def _focus_text(text: str, section_hint: str, head_chars: int = 2000, window_chars: int = 6000) -> str:
    if not section_hint:
        return text
    keywords = [section_hint, "销售机构", "代销", "直销", "机构名称", "销售商"]
    positions = []
    for keyword in keywords:
        if not keyword:
            continue
        start = 0
        while True:
            pos = text.find(keyword, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + len(keyword)
    if not positions:
        return text[: head_chars + window_chars]
    section_positions = sorted(set(positions))
    preferred = next((pos for pos in section_positions if pos > head_chars), section_positions[0])
    start = max(0, preferred - 500)
    head = text[:head_chars]
    focus = text[start : start + window_chars]
    if focus in head:
        return text[: head_chars + window_chars]
    return "{}\n...\n{}".format(head, focus)


def get_html_content(
    url: str,
    wait_seconds: float = 3.0,
    timeout: int = 20,
    section_hint: str = "",
    auto_paginate: bool = False,
    max_pages: int = 1,
    next_selector: str = "",
) -> str:
    """
    Fetch and clean a web page. This tool does not extract business fields.
    It returns cleaned HTML, visible text, and detected tables for the LLM to interpret.
    """
    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        timeout=timeout,
        section_hint=section_hint,
        auto_paginate=auto_paginate,
        max_pages=max_pages,
        next_selector=next_selector,
    )
    cleaned_html = page_payload.get("cleaned_html", "")
    soup = BeautifulSoup(cleaned_html, "lxml")
    full_text = soup.get_text("\n", strip=True)
    text = _focus_text(full_text, section_hint=section_hint)
    html_limit = 12000 if section_hint else 50000
    response: dict[str, Any] = {
        "url": page_payload.get("url", url),
        "title": page_payload.get("title"),
        # "cleaned_html": cleaned_html[:html_limit],
        "text": text[:15000],
        "tables": _extract_tables(soup),
        "network_json": page_payload.get("network_json", []),
        "evidence": {
            "source": "get_html_content",
            "browser_engine": page_payload.get("browser_engine"),
            "page_count": page_payload.get("page_count", 1),
            "pages": page_payload.get("pages", []),
            "resource_urls": (page_payload.get("resource_urls") or [])[-50:],
            "html_length": len(cleaned_html),
            "text_length": len(full_text),
            "focused_text_length": len(text),
        },
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
