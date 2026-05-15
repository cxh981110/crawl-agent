import re


def normalize_text(value: str) -> str:
    return re.sub(r"[\s:\uFF1A\-_/|]+", "", value or "").lower()


def coerce_value(value, field_type: str):
    if value is None:
        return None

    if isinstance(value, list):
        if field_type == "list":
            return [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(str(item).strip() for item in value if str(item).strip())

    text = str(value).strip()
    if not text or text in {"-", "--", "—", "暂无", "无"}:
        return None

    if field_type == "list":
        return [item.strip() for item in re.split(r"[\u3001,\uFF0C;/\n]+", text) if item.strip()]

    if field_type == "number":
        match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        if not match:
            return None
        number_text = match.group(0)
        return float(number_text) if "." in number_text else int(number_text)

    if field_type == "boolean":
        normalized = text.strip().lower()
        true_values = {
            "true", "yes", "y", "1",
            "\u662f", "\u6709", "\u652f\u6301", "\u4ee3\u9500", "\u7b2c\u4e09\u65b9",
            "thirdparty", "third-party", "distributor",
        }
        false_values = {
            "false", "no", "n", "0",
            "\u5426", "\u65e0", "\u4e0d\u652f\u6301", "\u76f4\u9500", "\u81ea\u9500",
            "directsale", "direct-sale", "direct",
        }
        compact = re.sub(r"[\s_\-]+", "", normalized)
        if normalized in true_values or compact in true_values:
            return True
        if normalized in false_values or compact in false_values:
            return False
        return None

    return text
