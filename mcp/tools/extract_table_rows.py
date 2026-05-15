import json
import ssl
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
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


def _score_table(table: Any, schema: Dict[str, FieldRule], table_hint: str) -> int:
    target_hint = normalize_text(table_hint) if table_hint else ""
    rows = table.find_all("tr")
    if not rows:
        return -1

    header_cells = _extract_row_cells(rows[0])
    normalized_headers = [normalize_text(cell) for cell in header_cells]
    score = 0

    for rule in schema.values():
        labels = {normalize_text(label) for label in rule.labels}
        if any(header in labels for header in normalized_headers):
            score += 3

    if target_hint:
        table_text = normalize_text(table.get_text(" ", strip=True))
        if target_hint in table_text:
            score += 2

    return score


def _row_level_fields(schema: Dict[str, FieldRule]) -> set[str]:
    page_level_fields = {"fund_name", "fund_code", "product_name", "product_code"}
    return {field_name for field_name in schema.keys() if field_name not in page_level_fields}


def _looks_like_key_value_table(table: Any, schema: Dict[str, FieldRule]) -> bool:
    rows = table.find_all("tr")
    if len(rows) < 2:
        return False

    normalized_labels = set()
    for rule in schema.values():
        normalized_labels.update(normalize_text(label) for label in rule.labels if label)

    first_cell_label_hits = 0
    two_col_rows = 0
    for row in rows:
        cells = _extract_row_cells(row)
        if len(cells) != 2:
            continue
        two_col_rows += 1
        if normalize_text(cells[0]) in normalized_labels:
            first_cell_label_hits += 1

    return two_col_rows >= 2 and first_cell_label_hits >= 2


def _find_best_table(soup: BeautifulSoup, schema: Dict[str, FieldRule], table_hint: str) -> Tuple[Optional[Any], int]:
    scored_tables = _find_candidate_tables(soup=soup, schema=schema, table_hint=table_hint)
    if not scored_tables:
        return None, -1
    return scored_tables[0]


def _find_candidate_tables(soup: BeautifulSoup, schema: Dict[str, FieldRule], table_hint: str) -> List[Tuple[Any, int]]:
    scored_tables: List[Tuple[Any, int]] = []
    row_fields = _row_level_fields(schema)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        if _looks_like_key_value_table(table=table, schema=schema):
            continue
        column_map = _build_column_map(_extract_row_cells(rows[0]), schema)
        if row_fields and not any(column_map.get(field_name) for field_name in row_fields):
            continue
        score = _score_table(table=table, schema=schema, table_hint=table_hint)
        if score > 0:
            scored_tables.append((table, score))
    scored_tables.sort(key=lambda item: item[1], reverse=True)
    return scored_tables


def _build_column_map(header_cells: List[str], schema: Dict[str, FieldRule]) -> Dict[str, List[int]]:
    normalized_headers = [normalize_text(cell) for cell in header_cells]
    column_map: Dict[str, List[int]] = {}
    for field_name, rule in schema.items():
        labels = {normalize_text(label) for label in rule.labels}
        for index, header in enumerate(normalized_headers):
            if header and header in labels:
                column_map.setdefault(field_name, []).append(index)
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

        repeat_count = max((len(indexes) for indexes in column_map.values()), default=1)
        for repeat_index in range(repeat_count):
            item: Dict[str, Any] = {}
            non_null_count = 0
            for field_name, rule in schema.items():
                value = None
                col_indexes = column_map.get(field_name) or []
                if col_indexes:
                    if len(col_indexes) == 1:
                        col_index = col_indexes[0]
                    elif repeat_index < len(col_indexes):
                        col_index = col_indexes[repeat_index]
                    else:
                        col_index = None
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
    candidates: Dict[str, str] = {}

    label_aliases: Dict[str, List[str]] = {
        "fund_name": ["产品全称", "产品名称", "产品简称", "基金名称", "集合计划名称", "计划名称"],
        "fund_code": ["产品代码", "产品编码", "基金代码", "基金编号", "产品编号", "代码", "编号"],
    }

    label_aliases["fund_name"].extend(
        [
            "\u4ea7\u54c1\u5168\u79f0",
            "\u4ea7\u54c1\u540d\u79f0",
            "\u4ea7\u54c1\u7b80\u79f0",
            "\u57fa\u91d1\u540d\u79f0",
            "\u96c6\u5408\u8ba1\u5212\u540d\u79f0",
            "\u8ba1\u5212\u540d\u79f0",
            "\u57fa\u91d1\u7b80\u79f0",
        ]
    )
    label_aliases["fund_code"].extend(
        [
            "\u4ea7\u54c1\u4ee3\u7801",
            "\u4ea7\u54c1\u7f16\u7801",
            "\u57fa\u91d1\u4ee3\u7801",
            "\u57fa\u91d1\u7f16\u7801",
            "\u4ee3\u7801",
            "\u7f16\u7801",
        ]
    )

    for row in soup.find_all("tr"):
        cells = _extract_row_cells(row)
        if len(cells) < 2:
            continue
        label = normalize_text(cells[0])
        value = cells[1].strip()
        if coerce_value(value, "string") is None:
            continue
        for field_name, aliases in label_aliases.items():
            if field_name in candidates:
                continue
            normalized_aliases = {normalize_text(x) for x in aliases}
            if label in normalized_aliases:
                candidates[field_name] = value

    parsed = urlparse(page_url or "")
    qs = parse_qs(parsed.query)
    if not qs and parsed.fragment:
        fragment_query = parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""
        qs = parse_qs(fragment_query)
    for key in ("fund_code", "product_code", "productcode", "fundcode", "code"):
        values = qs.get(key) or []
        if values and values[0] and "fund_code" not in candidates:
            candidates["fund_code"] = values[0].strip()
            break

    api_candidates = _extract_api_global_candidates(page_url=page_url)
    for key, value in api_candidates.items():
        if value not in (None, "", []) and key not in candidates:
            candidates[key] = value

    return candidates


def _extract_api_global_candidates(page_url: str) -> Dict[str, str]:
    parsed = urlparse(page_url or "")
    qs = parse_qs(parsed.query)
    product_codes = qs.get("productcode") or qs.get("productCode") or []
    if "gfund.com" not in parsed.netloc or not product_codes:
        return {}

    api_url = (
        "https://www.gfund.com/ws-business-server/fund/getFundInfo"
        "?productCode={}&siteno=main&merchantId=0"
    ).format(product_codes[0])
    try:
        with urlopen(api_url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}

    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {}

    candidates: Dict[str, str] = {}
    if coerce_value(data.get("productName"), "string") is not None:
        candidates["fund_name"] = str(data.get("productName")).strip()
    if coerce_value(data.get("productCode"), "string") is not None:
        candidates["fund_code"] = str(data.get("productCode")).strip()
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


def _infer_distributor_flags(data_rows: List[Dict[str, Any]], schema: Dict[str, FieldRule]) -> None:
    if "is_distributor" not in schema:
        return

    direct_markers = ("直销", "分支机构")
    direct_markers = direct_markers + ("\u76f4\u9500", "\u57fa\u91d1\u7ba1\u7406\u4eba")
    for row in data_rows:
        if row.get("is_distributor") is not None:
            continue
        org_name = str(row.get("org_name") or "").strip()
        if not org_name:
            continue
        row["is_distributor"] = not any(marker in org_name for marker in direct_markers)


def _row_key(row: Dict[str, Any], schema: Dict[str, FieldRule]) -> Tuple[str, ...]:
    return tuple(str(row.get(field_name, "")) for field_name in schema.keys())


def _dedupe_rows(rows: List[Dict[str, Any]], schema: Dict[str, FieldRule]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = _row_key(row, schema)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _json_candidate_keys(field_name: str) -> List[str]:
    aliases = {
        "fund_name": ["fundname", "fundfullname", "fund_name", "productname", "product_name"],
        "fund_code": ["fundcode", "fund_code", "productcode", "product_code"],
        "org_name": ["agencyname", "agency_name", "orgname", "org_name", "organization", "name"],
        "phone": ["phone", "tel", "telephone", "mobile", "servicetel", "servicephone"],
        "WEB": ["web", "website", "url", "href", "link"],
        "is_distributor": ["isdistributor", "is_distributor"],
    }
    return aliases.get(field_name, [])


def _normalized_json_key_map(item: Dict[str, Any]) -> Dict[str, Any]:
    return {normalize_text(str(key)): value for key, value in item.items()}


def _extract_json_arrays(value: Any) -> List[List[Dict[str, Any]]]:
    arrays: List[List[Dict[str, Any]]] = []
    if isinstance(value, list):
        dict_items = [item for item in value if isinstance(item, dict)]
        if dict_items:
            arrays.append(dict_items)
        for item in value:
            arrays.extend(_extract_json_arrays(item))
    elif isinstance(value, dict):
        for item in value.values():
            arrays.extend(_extract_json_arrays(item))
    return arrays


def _map_json_item_to_row(item: Dict[str, Any], schema: Dict[str, FieldRule]) -> Dict[str, Any]:
    key_map = _normalized_json_key_map(item)
    row: Dict[str, Any] = {}
    for field_name, rule in schema.items():
        value = None
        keys = [normalize_text(label) for label in rule.labels] + _json_candidate_keys(field_name)
        for key in keys:
            normalized_key = normalize_text(key)
            if normalized_key in key_map:
                value = key_map[normalized_key]
                break
        row[field_name] = coerce_value(value, rule.field_type)
    return row


def _read_json_url(url: str) -> Optional[Any]:
    try:
        context = ssl._create_unverified_context()
        with urlopen(url, timeout=20, context=context) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read()
        text = raw.decode("utf-8", errors="ignore").strip()
        if "json" not in content_type.lower() and not text.startswith(("{", "[")):
            return None
        return json.loads(text)
    except Exception:
        return None


def _score_json_url(url: str) -> int:
    lowered = url.lower()
    score = 0
    if any(token in lowered for token in ("agency", "sales", "sale", "org", "fundagency")):
        score += 10
    if any(token in lowered for token in ("fundinfo", "funddetail")):
        score += 4
    if any(token in lowered for token in ("query", "api", "json")):
        score += 2
    return score


def _required_json_row_fields(schema: Dict[str, FieldRule]) -> set[str]:
    for field_group in ({"org_name"}, {"name", "title"}):
        matched = field_group.intersection(schema.keys())
        if matched:
            return matched
    return _row_level_fields(schema)


def _extract_rows_from_json_resources(
    resource_urls: List[str],
    schema: Dict[str, FieldRule],
    page_url: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    urls = sorted(
        {url for url in resource_urls if isinstance(url, str) and _score_json_url(url) > 0},
        key=_score_json_url,
        reverse=True,
    )
    global_candidates = _extract_global_candidates_from_page(BeautifulSoup("", "lxml"), page_url)
    rows: List[Dict[str, Any]] = []
    used_urls: List[str] = []

    for url in urls[:20]:
        payload = _read_json_url(url)
        if payload is None:
            continue

        if isinstance(payload, dict):
            for field_name, rule in schema.items():
                if field_name in global_candidates:
                    continue
                value = _map_json_item_to_row(payload, {field_name: rule}).get(field_name)
                if value not in (None, "", []):
                    global_candidates[field_name] = value

        for array in _extract_json_arrays(payload):
            mapped_rows = [_map_json_item_to_row(item, schema) for item in array]
            row_fields = _required_json_row_fields(schema)
            useful_rows = [
                row for row in mapped_rows
                if any(row.get(field_name) not in (None, "", []) for field_name in row_fields)
            ]
            if not useful_rows:
                continue
            rows.extend(useful_rows)
            used_urls.append(url)

    _backfill_rows_with_global_values(rows, schema, global_candidates)
    _infer_distributor_flags(rows, schema)
    rows = _dedupe_rows(rows, schema)
    evidence = {
        "source": "json_resource_urls",
        "used_urls": used_urls,
        "candidate_url_count": len(urls),
        "global_candidates": global_candidates,
    }
    return rows, evidence


def extract_table_rows(
    url: str,
    row_schema_json: str,
    table_hint: str = "",
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
    auto_paginate: bool = True,
    max_pages: int = 20,
    next_selector: str = "",
) -> str:
    schema = _parse_row_schema(row_schema_json)
    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        actions_json=actions_json,
        timeout=timeout,
        section_hint=table_hint,
        auto_paginate=auto_paginate,
        max_pages=max_pages,
        next_selector=next_selector,
    )
    html = page_payload.get("cleaned_html", "")
    soup = BeautifulSoup(html, "lxml")

    candidate_tables = _find_candidate_tables(soup, schema, table_hint)
    if not candidate_tables:
        json_rows, json_evidence = _extract_rows_from_json_resources(
            resource_urls=page_payload.get("resource_urls", []),
            schema=schema,
            page_url=page_payload.get("url", url),
        )
        if json_rows:
            missing_fields = []
            for field_name in schema.keys():
                if not any(row.get(field_name) not in (None, "", []) for row in json_rows):
                    missing_fields.append(field_name)

            response = {
                "url": page_payload.get("url", url),
                "title": page_payload.get("title"),
                "data": json_rows,
                "missing_fields": missing_fields,
                "evidence": {
                    **json_evidence,
                    "table_count": 0,
                    "extracted_table_count": 0,
                    "page_count": page_payload.get("page_count", 1),
                    "pages": page_payload.get("pages", []),
                },
            }
            return json.dumps(response, ensure_ascii=False, indent=2)

        response = {
            "url": page_payload.get("url", url),
            "title": page_payload.get("title"),
            "data": [],
            "missing_fields": list(schema.keys()),
            "evidence": {
                "reason": "No suitable table found.",
                "table_score": -1,
                "page_count": page_payload.get("page_count", 1),
            },
        }
        return json.dumps(response, ensure_ascii=False, indent=2)

    data_rows: List[Dict[str, Any]] = []
    table_evidence_items: List[Dict[str, Any]] = []
    for table_index, (table, table_score) in enumerate(candidate_tables):
        table_rows, table_evidence = _extract_rows_from_table(table, schema)
        if not table_rows:
            continue
        data_rows.extend(table_rows)
        table_evidence_items.append(
            {
                "table_index": table_index,
                "table_score": table_score,
                "table": table_evidence,
            }
        )

    global_candidates = _extract_global_candidates_from_page(soup, page_payload.get("url", url))
    _backfill_rows_with_global_values(data_rows, schema, global_candidates)
    _infer_distributor_flags(data_rows, schema)
    data_rows = _dedupe_rows(data_rows, schema)

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
            "table_count": len(candidate_tables),
            "extracted_table_count": len(table_evidence_items),
            "tables": table_evidence_items,
            "global_candidates": global_candidates,
            "page_count": page_payload.get("page_count", 1),
            "pages": page_payload.get("pages", []),
        },
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
