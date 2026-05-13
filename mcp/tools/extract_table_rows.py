import json
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from agent.config import build_schema
from agent.models import FieldRule
from agent.normalize import coerce_value, normalize_text

from .page_pipeline import fetch_and_clean_page


def _parse_row_schema(row_schema_json: str) -> Dict[str, FieldRule]:
    raw = json.loads(row_schema_json)
    if not isinstance(raw, dict):
        raise ValueError("row_schema_json must be a JSON object.")
    return build_schema(raw)


def _extract_row_cells(row) -> List[str]:
    cells = row.find_all(["th", "td"])
    return [cell.get_text(" ", strip=True) for cell in cells]


def _find_best_table(soup: BeautifulSoup, schema: Dict[str, FieldRule], table_hint: str) -> Tuple[Optional[Any], int]:
    target_hint = normalize_text(table_hint) if table_hint else ""
    best_table = None
    best_score = -1

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_cells = _extract_row_cells(rows[0])
        normalized_headers = [normalize_text(cell) for cell in header_cells]
        score = 0

        for rule in schema.values():
            labels = {normalize_text(label) for label in rule.labels}
            if any(header in labels for header in normalized_headers):
                score += 3

        if target_hint:
            table_text = normalize_text(table.get_text(" ", strip=True))
            if target_hint and target_hint in table_text:
                score += 2

        if score > best_score:
            best_score = score
            best_table = table

    return best_table, best_score


def _build_column_map(header_cells: List[str], schema: Dict[str, FieldRule]) -> Dict[str, int]:
    normalized_headers = [normalize_text(cell) for cell in header_cells]
    column_map: Dict[str, int] = {}
    for field_name, rule in schema.items():
        labels = {normalize_text(label) for label in rule.labels}
        for index, header in enumerate(normalized_headers):
            if header and header in labels:
                column_map[field_name] = index
                break
    return column_map


def _extract_rows_from_table(table, schema: Dict[str, FieldRule]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = table.find_all("tr")
    if not rows:
        return [], {"column_map": {}, "row_count": 0}

    header_cells = _extract_row_cells(rows[0])
    column_map = _build_column_map(header_cells, schema)
    data_rows: List[Dict[str, Any]] = []

    for row in rows[1:]:
        cells = _extract_row_cells(row)
        if not cells or all(not cell for cell in cells):
            continue

        item: Dict[str, Any] = {}
        non_null_count = 0
        for field_name, rule in schema.items():
            value = None
            col_index = column_map.get(field_name)
            if col_index is not None and col_index < len(cells):
                value = coerce_value(cells[col_index], rule.field_type)
            item[field_name] = value
            if value not in (None, "", []):
                non_null_count += 1

        if non_null_count > 0:
            data_rows.append(item)

    evidence = {
        "column_map": column_map,
        "headers": header_cells,
        "row_count": len(data_rows),
    }
    return data_rows, evidence


def _extract_global_candidates_from_page(soup: BeautifulSoup, page_url: str) -> Dict[str, str]:
    """
    Extract page-level values (e.g., fund_name/fund_code) from key-value blocks and URL params.
    """
    candidates: Dict[str, str] = {}

    label_aliases: Dict[str, List[str]] = {
        "fund_name": ["产品全称", "产品名称", "产品简称", "基金名称", "集合计划名称", "计划名称"],
        "fund_code": ["产品代码", "产品编码", "基金代码", "基金编号", "产品编号", "代码", "编号"],
    }

    # 1) Two-column rows: <th>label</th><td>value</td> or first two cells in tr.
    for row in soup.find_all("tr"):
        cells = _extract_row_cells(row)
        if len(cells) < 2:
            continue
        label = normalize_text(cells[0])
        value = cells[1].strip()
        if not value:
            continue
        for field_name, aliases in label_aliases.items():
            if field_name in candidates:
                continue
            normalized_aliases = {normalize_text(x) for x in aliases}
            if label in normalized_aliases:
                candidates[field_name] = value

    # 2) URL query params as fallback for code-like fields.
    parsed = urlparse(page_url or "")
    qs = parse_qs(parsed.query)
    for key in ("fund_code", "product_code", "code"):
        values = qs.get(key) or []
        if values and values[0] and "fund_code" not in candidates:
            candidates["fund_code"] = values[0].strip()
            break

    return candidates


def _backfill_rows_with_global_values(
    data_rows: List[Dict[str, Any]], schema: Dict[str, FieldRule], global_candidates: Dict[str, str]
) -> None:
    if not data_rows:
        return
    for field_name, value in global_candidates.items():
        if field_name not in schema or value in (None, "", []):
            continue
        rule = schema[field_name]
        coerced = coerce_value(value, rule.field_type)
        for row in data_rows:
            if row.get(field_name) in (None, "", []):
                row[field_name] = coerced


def extract_table_rows(
    url: str,
    row_schema_json: str,
    table_hint: str = "",
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
) -> str:
    schema = _parse_row_schema(row_schema_json)
    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        actions_json=actions_json,
        timeout=timeout,
    )
    html = page_payload.get("cleaned_html", "")
    soup = BeautifulSoup(html, "lxml")

    best_table, best_score = _find_best_table(soup, schema, table_hint)
    if best_table is None or best_score <= 0:
        response = {
            "url": page_payload.get("url", url),
            "title": page_payload.get("title"),
            "data": [],
            "missing_fields": list(schema.keys()),
            "evidence": {
                "reason": "No suitable table found.",
                "table_score": best_score,
            },
        }
        return json.dumps(response, ensure_ascii=False, indent=2)

    data_rows, table_evidence = _extract_rows_from_table(best_table, schema)
    global_candidates = _extract_global_candidates_from_page(soup, page_payload.get("url", url))
    _backfill_rows_with_global_values(data_rows, schema, global_candidates)

    missing_fields = []
    for field_name in schema.keys():
        if not any(row.get(field_name) not in (None, "", []) for row in data_rows):
            missing_fields.append(field_name)

    response = {
        "url": page_payload.get("url", url),
        "title": page_payload.get("title"),
        "data": data_rows,
        "missing_fields": missing_fields,
        "evidence": {
            "table_score": best_score,
            "table": table_evidence,
            "global_candidates": global_candidates,
        },
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
