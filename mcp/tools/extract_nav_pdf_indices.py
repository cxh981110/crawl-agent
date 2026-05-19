import io
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
import urllib3
from bs4 import BeautifulSoup

from .page_pipeline import fetch_and_clean_page


urllib3.disable_warnings()


DEFAULT_BANK_NAME = "北银理财"
DEFAULT_PLATFORM_CODE = "8001_BYLC"


def _require_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pdfplumber is required for nav PDF extraction. Install dependencies with: pip install -r requirements.txt"
        ) from exc


def _http_get(url: str, timeout: int = 20) -> requests.Response:
    url = _safe_url(url)
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, timeout=timeout, headers=headers)
        response.raise_for_status()
        return response
    except requests.exceptions.SSLError:
        response = requests.get(url, timeout=timeout, headers=headers, verify=False)
        response.raise_for_status()
        return response


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%()")
    query = quote(parts.query, safe="=&%")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _normalize_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None

    match = re.search(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})\s*日?", text)
    if not match:
        match = re.search(r"(\d{4})[.](\d{1,2})[.](\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    return "{:04d}-{:02d}-{:02d}".format(year, month, day)


def _clean_number(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "—"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return match.group(0) if match else None


def _clean_product_code(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    match = re.search(r"(?=[A-Z0-9]*\d)[A-Z0-9]{6,20}", text, flags=re.IGNORECASE)
    return match.group(0).upper() if match else None


def _product_code_from_url(url: str) -> Optional[str]:
    parts = urlsplit(url)
    query_pairs = [part.split("=", 1) for part in parts.query.split("&") if "=" in part]
    for key, value in query_pairs:
        if key in {"prodTradeCode", "productCode", "product_code", "code"}:
            return _clean_product_code(value)
    return _clean_product_code(url)


def _cell_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _find_pdf_links(list_url: str, max_pdfs: int, timeout: int) -> List[Dict[str, str]]:
    if ".pdf" in list_url.lower():
        return [{"title": "", "url": list_url}]

    response = _http_get(list_url, timeout=timeout)
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "lxml")

    links: List[Dict[str, str]] = []
    seen = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        full_url = urljoin(list_url, href)
        title = anchor.get_text(" ", strip=True)
        if ".pdf" not in full_url.lower():
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append({"title": title, "url": full_url})
        if len(links) >= max_pdfs:
            break
    return links


def _header_index(headers: List[str], keywords: Tuple[str, ...]) -> Optional[int]:
    for index, header in enumerate(headers):
        if any(keyword in header for keyword in keywords):
            return index
    return None


def _extract_nav_date_from_text(text: str) -> Optional[str]:
    patterns = [
        r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日).{0,20}净值",
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}).{0,20}净值",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.DOTALL)
        if match:
            return _normalize_date(match.group(1))
    return None


def _extract_indices_from_table(
    table: List[List[Any]],
    platform_code: str,
    default_nav_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    indices: List[Dict[str, Any]] = []
    current_date: Optional[str] = default_nav_date
    header_map: Dict[str, Optional[int]] = {}

    for row in table:
        cells = [_cell_text(cell) for cell in (row or [])]
        if not any(cells):
            continue

        joined = "".join(cells)
        if "产品" in joined and ("代码" in joined or "销售代码" in joined):
            header_map = {
                "date": _header_index(cells, ("日期", "估值日", "净值日期")),
                "productCode": _header_index(cells, ("产品销售代码", "销售代码", "产品代码", "产品编码")),
                "nav": _header_index(cells, ("单位净值", "份额净值")),
                "accNav": _header_index(cells, ("累计净值",)),
                "tenThousandRevenue": _header_index(cells, ("万份收益", "每万份收益", "万份产品收益")),
                "sevenDayAnnualizedYield": _header_index(cells, ("七日年化", "7日年化")),
            }
            continue

        product_index = header_map.get("productCode")
        if product_index is None or product_index >= len(cells):
            continue

        product_code = _clean_product_code(cells[product_index])
        if not product_code:
            continue

        date_index = header_map.get("date")
        if date_index is not None and date_index < len(cells):
            current_date = _normalize_date(cells[date_index]) or current_date
        if not current_date:
            date_from_row = _normalize_date(joined)
            current_date = date_from_row or current_date
        if not current_date:
            continue

        item: Dict[str, Any] = {
            "platformCode": platform_code,
            "productCode": product_code,
            "navDate": current_date,
        }

        nav_index = header_map.get("nav")
        acc_nav_index = header_map.get("accNav")
        revenue_index = header_map.get("tenThousandRevenue")
        yield_index = header_map.get("sevenDayAnnualizedYield")

        is_yield_row = revenue_index is not None or yield_index is not None
        if is_yield_row:
            if revenue_index is not None and revenue_index < len(cells):
                item["tenThousandRevenue"] = _clean_number(cells[revenue_index])
            if yield_index is not None and yield_index < len(cells):
                item["sevenDayAnnualizedYield"] = _clean_number(cells[yield_index])
        else:
            if nav_index is not None and nav_index < len(cells):
                item["nav"] = _clean_number(cells[nav_index])
            if acc_nav_index is not None and acc_nav_index < len(cells):
                item["accNav"] = _clean_number(cells[acc_nav_index])

        item = {key: value for key, value in item.items() if value not in (None, "", [])}
        if any(key in item for key in ("nav", "accNav", "tenThousandRevenue", "sevenDayAnnualizedYield")):
            indices.append(item)

    return indices


def _extract_indices_from_text(text: str, platform_code: str) -> List[Dict[str, Any]]:
    indices: List[Dict[str, Any]] = []
    current_date: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        current_date = _normalize_date(line) or current_date
        product_code = _clean_product_code(line)
        if not product_code or not current_date:
            continue

        numbers = re.findall(r"-?\d+\.\d+", line)
        if len(numbers) >= 2 and ("万份" in text or "七日" in text or "年化" in text):
            indices.append(
                {
                    "platformCode": platform_code,
                    "productCode": product_code,
                    "navDate": current_date,
                    "tenThousandRevenue": numbers[0],
                    "sevenDayAnnualizedYield": numbers[1],
                }
            )
        elif len(numbers) >= 4 and "净值" in text:
            indices.append(
                {
                    "platformCode": platform_code,
                    "productCode": product_code,
                    "navDate": current_date,
                    "nav": numbers[0],
                    "accNav": numbers[1],
                }
            )
    return indices


def _dedupe_indices(indices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in indices:
        key = tuple((field, item.get(field)) for field in sorted(item.keys()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _extract_html_table_rows(soup: BeautifulSoup) -> List[List[str]]:
    rows: List[List[str]] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if cells and any(cell for cell in cells):
                rows.append(cells)
    return rows


def _extract_indices_from_html(
    url: str,
    platform_code: str,
    wait_seconds: float,
    timeout: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        timeout=timeout,
        auto_paginate=False,
    )
    soup = BeautifulSoup(page_payload.get("cleaned_html", ""), "lxml")
    rows = _extract_html_table_rows(soup)
    product_code = _product_code_from_url(url)
    indices: List[Dict[str, Any]] = []
    current_headers: List[str] = []

    for row in rows:
        normalized = [_cell_text(cell) for cell in row]
        joined = "".join(normalized)
        if "净值日期" in joined and any(token in joined for token in ("单位净值", "累计净值", "万份收益")):
            current_headers = normalized
            continue

        if not current_headers or not product_code:
            continue

        date_index = _header_index(current_headers, ("净值日期", "日期"))
        if date_index is None or date_index >= len(row):
            continue
        nav_date = _normalize_date(row[date_index])
        if not nav_date:
            continue

        item: Dict[str, Any] = {
            "platformCode": platform_code,
            "productCode": product_code,
            "navDate": nav_date,
        }
        nav_index = _header_index(current_headers, ("单位净值", "份额净值"))
        acc_nav_index = _header_index(current_headers, ("累计净值", "份额累计净值"))
        revenue_index = _header_index(current_headers, ("万份收益", "每万份收益", "万份产品收益"))
        yield_index = _header_index(current_headers, ("七日", "近七日收益率", "七日年化"))

        if revenue_index is not None or yield_index is not None:
            if revenue_index is not None and revenue_index < len(row):
                item["tenThousandRevenue"] = _clean_number(row[revenue_index])
            if yield_index is not None and yield_index < len(row):
                item["sevenDayAnnualizedYield"] = _clean_number(row[yield_index])
        else:
            if nav_index is not None and nav_index < len(row):
                item["nav"] = _clean_number(row[nav_index])
            if acc_nav_index is not None and acc_nav_index < len(row):
                item["accNav"] = _clean_number(row[acc_nav_index])

        item = {key: value for key, value in item.items() if value not in (None, "", [])}
        if any(key in item for key in ("nav", "accNav", "tenThousandRevenue", "sevenDayAnnualizedYield")):
            indices.append(item)

    return _dedupe_indices(indices), {
        "source": "html_page",
        "title": page_payload.get("title"),
        "table_row_count": len(rows),
        "product_code": product_code,
    }


def _parse_pdf(pdf_url: str, platform_code: str, timeout: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    import pdfplumber

    response = _http_get(pdf_url, timeout=timeout)
    content = response.content
    if not content.startswith(b"%PDF"):
        return [], {"pdf_url": pdf_url, "reason": "Downloaded content is not a PDF."}

    all_indices: List[Dict[str, Any]] = []
    page_count = 0
    table_count = 0
    text_parts: List[str] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
        full_text = "\n".join(text_parts)
        default_nav_date = _extract_nav_date_from_text(full_text)
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                table_count += 1
                all_indices.extend(
                    _extract_indices_from_table(
                        table,
                        platform_code=platform_code,
                        default_nav_date=default_nav_date,
                    )
                )

    if not all_indices:
        all_indices.extend(_extract_indices_from_text("\n".join(text_parts), platform_code=platform_code))

    return _dedupe_indices(all_indices), {
        "pdf_url": pdf_url,
        "page_count": page_count,
        "table_count": table_count,
        "index_count": len(all_indices),
    }


def extract_nav_pdf_indices(
    url: str,
    bank_name: str = DEFAULT_BANK_NAME,
    platform_code: str = DEFAULT_PLATFORM_CODE,
    max_pdfs: int = 20,
    timeout: int = 20,
    wait_seconds: float = 8.0,
) -> str:
    bank_name = bank_name or DEFAULT_BANK_NAME
    platform_code = platform_code or DEFAULT_PLATFORM_CODE

    if ".pdf" not in url.lower():
        try:
            html_indices, html_evidence = _extract_indices_from_html(
                url=url,
                platform_code=platform_code,
                wait_seconds=wait_seconds,
                timeout=timeout,
            )
            if html_indices:
                data = {
                    "indices": html_indices,
                    "bankName": bank_name,
                    "platformCode": platform_code,
                    "crawlerTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                return json.dumps(
                    {
                        "url": url,
                        "title": html_evidence.get("title"),
                        "data": data,
                        "missing_fields": [],
                        "evidence": html_evidence,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

    _require_pdfplumber()
    pdf_links = _find_pdf_links(list_url=url, max_pdfs=max_pdfs, timeout=timeout)
    indices: List[Dict[str, Any]] = []
    pdf_evidence: List[Dict[str, Any]] = []

    for link in pdf_links:
        try:
            pdf_indices, evidence = _parse_pdf(
                pdf_url=link["url"],
                platform_code=platform_code,
                timeout=timeout,
            )
            evidence["title"] = link.get("title")
            indices.extend(pdf_indices)
            pdf_evidence.append(evidence)
        except Exception as exc:
            pdf_evidence.append(
                {
                    "pdf_url": link["url"],
                    "title": link.get("title"),
                    "error": "{}: {}".format(type(exc).__name__, exc),
                }
            )

    data = {
        "indices": _dedupe_indices(indices),
        "bankName": bank_name,
        "platformCode": platform_code,
        "crawlerTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    missing_fields = []
    if not data["indices"]:
        missing_fields.append("indices")

    response = {
        "url": url,
        "title": None,
        "data": data,
        "missing_fields": missing_fields,
        "evidence": {
            "pdf_link_count": len(pdf_links),
            "pdfs": pdf_evidence,
        },
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
