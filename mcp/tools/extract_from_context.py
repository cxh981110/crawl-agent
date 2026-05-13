import re
from typing import Any, Dict, Optional, Tuple

from agent.models import ExtractionEvidence, FieldRule
from agent.normalize import coerce_value, normalize_text


def _match_key_value(
    key_values: list[dict[str, Any]],
    rule: FieldRule,
) -> Tuple[Any, Optional[ExtractionEvidence]]:
    labels = {normalize_text(label) for label in rule.labels}
    for item in key_values:
        label = str(item.get("label", ""))
        value = item.get("value")
        if normalize_text(label) in labels:
            return value, ExtractionEvidence(
                source="context_key_value",
                matched_label=label,
                matched_text=str(value)[:300],
            )
    return None, None


def _match_table(
    tables: list[dict[str, Any]],
    rule: FieldRule,
) -> Tuple[Any, Optional[ExtractionEvidence]]:
    labels = {normalize_text(label) for label in rule.labels}
    for table in tables:
        rows = table.get("rows", [])
        if not rows:
            continue

        header = rows[0]
        normalized_header = [normalize_text(str(cell)) for cell in header]
        matched_col = None
        for idx, col_name in enumerate(normalized_header):
            if col_name in labels:
                matched_col = idx
                break
        if matched_col is None:
            continue

        for row_idx, row in enumerate(rows[1:], start=1):
            if matched_col < len(row):
                value = row[matched_col]
                if value not in (None, "", []):
                    return value, ExtractionEvidence(
                        source="context_table",
                        locator="{}.rows[{}][{}]".format(table.get("id", "table"), row_idx, matched_col),
                        matched_label=header[matched_col] if matched_col < len(header) else None,
                        matched_text=str(value)[:300],
                    )
    return None, None


def _match_blocks(
    blocks: list[dict[str, Any]],
    rule: FieldRule,
) -> Tuple[Any, Optional[ExtractionEvidence]]:
    labels = [label for label in rule.labels if label]
    for block in blocks:
        text = str(block.get("text", ""))
        if not text:
            continue
        for label in labels:
            escaped = re.escape(label)
            inline_match = re.search(rf"{escaped}\s*[:\uFF1A]\s*(.+)", text, flags=re.IGNORECASE)
            if inline_match:
                value = inline_match.group(1).splitlines()[0].strip()
                return value, ExtractionEvidence(
                    source="context_block",
                    locator=block.get("id"),
                    matched_label=label,
                    matched_text=value[:300],
                )
    return None, None


def extract_from_context_payload(
    context: Dict[str, Any],
    schema: Dict[str, FieldRule],
) -> Tuple[Dict[str, Any], Dict[str, Optional[dict[str, Any]]]]:
    selected_blocks = list((context.get("selected_blocks") or []))
    tables = list((context.get("tables") or []))
    key_values = list((context.get("key_values") or []))

    data: Dict[str, Any] = {}
    evidence: Dict[str, Optional[dict[str, Any]]] = {}

    for field_name, rule in schema.items():
        value = None
        matched: Optional[ExtractionEvidence] = None

        value, matched = _match_key_value(key_values, rule)
        if value in (None, "", []):
            value, matched = _match_table(tables, rule)
        if value in (None, "", []):
            value, matched = _match_blocks(selected_blocks, rule)

        data[field_name] = coerce_value(value, rule.field_type)
        evidence[field_name] = matched.__dict__ if matched else None

    return data, evidence
