from typing import Any, Dict, List

from bs4 import BeautifulSoup

from agent.models import FieldRule
from agent.normalize import normalize_text

def _iter_blocks(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    index = 0
    selectors = ["main", "article", "section", "div", "table", "dl", "ul", "ol"]
    for node in soup.select(",".join(selectors)):
        text = node.get_text("\n", strip=True)
        if not text or len(text) < 12:
            continue
        blocks.append(
            {
                "id": "block_{}".format(index),
                "tag": node.name,
                "text": text,
            }
        )
        index += 1
    return blocks


def _label_set(schema: Dict[str, FieldRule]) -> Dict[str, set[str]]:
    return {
        field_name: {normalize_text(label) for label in rule.labels if label}
        for field_name, rule in schema.items()
    }


def _score_block(text: str, labels: Dict[str, set[str]]) -> Dict[str, Any]:
    normalized_text = normalize_text(text)
    field_hits: Dict[str, int] = {}
    score = 0
    for field_name, field_labels in labels.items():
        hit_count = 0
        for label in field_labels:
            if label and label in normalized_text:
                hit_count += 1
        if hit_count > 0:
            field_hits[field_name] = hit_count
            score += hit_count * 3
    return {"score": score, "field_hits": field_hits}


def _extract_tables(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    tables: List[Dict[str, Any]] = []
    for idx, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if not rows:
            continue
        parsed_rows: List[List[str]] = []
        for row in rows:
            cells = row.find_all(["th", "td"])
            parsed_rows.append([cell.get_text(" ", strip=True) for cell in cells])
        tables.append({"id": "table_{}".format(idx), "rows": parsed_rows})
    return tables


def _extract_key_values(soup: BeautifulSoup) -> List[Dict[str, str]]:
    kvs: List[Dict[str, str]] = []
    # label:value lines
    for node in soup.find_all(["p", "li", "td", "th", "span", "div"]):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if ":" in text or "\uFF1A" in text:
            parts = text.replace("\uFF1A", ":").split(":", 1)
            if len(parts) == 2:
                label = parts[0].strip()
                value = parts[1].strip()
                if label and value:
                    kvs.append({"label": label, "value": value})
    return kvs


def build_context_payload(
    html: str,
    schema: Dict[str, FieldRule],
    token_budget: int = 3000,
) -> Dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    labels = _label_set(schema)
    blocks = _iter_blocks(soup)

    ranked_blocks: List[Dict[str, Any]] = []
    for block in blocks:
        scored = _score_block(block["text"], labels)
        ranked_blocks.append(
            {
                "id": block["id"],
                "tag": block["tag"],
                "score": scored["score"],
                "field_hits": scored["field_hits"],
                "text": block["text"],
            }
        )

    ranked_blocks.sort(key=lambda item: item["score"], reverse=True)

    # Approximate token budget by characters (~4 chars per token).
    char_budget = max(token_budget, 800) * 4
    selected_blocks: List[Dict[str, Any]] = []
    used_chars = 0
    for block in ranked_blocks:
        block_chars = len(block["text"])
        if selected_blocks and used_chars + block_chars > char_budget:
            continue
        selected_blocks.append(block)
        used_chars += block_chars
        if used_chars >= char_budget:
            break

    tables = _extract_tables(soup)
    key_values = _extract_key_values(soup)

    return {
        "selected_blocks": selected_blocks,
        "tables": tables,
        "key_values": key_values,
        "meta": {
            "token_budget": token_budget,
            "char_budget": char_budget,
            "selected_block_count": len(selected_blocks),
            "table_count": len(tables),
            "key_value_count": len(key_values),
        },
    }
