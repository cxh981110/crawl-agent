import json
from pathlib import Path
from typing import Any, Union

from agent.models import FieldRule


def build_schema(raw: dict[str, Any]) -> dict[str, FieldRule]:
    if not isinstance(raw, dict):
        raise ValueError("schema must be a JSON object.")

    rules: dict[str, FieldRule] = {}
    for field_name, config in raw.items():
        labels: list[str] = []
        field_type = "string"

        if isinstance(config, str):
            labels = [config]
        elif isinstance(config, list):
            labels = [str(item) for item in config]
        elif isinstance(config, dict):
            label_value = config.get("labels") or config.get("label") or config.get("name")
            if isinstance(label_value, str):
                labels = [label_value]
            elif isinstance(label_value, list):
                labels = [str(item) for item in label_value]
            field_type = str(config.get("type", "string"))
        else:
            raise ValueError("Unsupported field config for {}: {}".format(field_name, config))

        if not labels:
            labels = [str(field_name)]

        rules[str(field_name)] = FieldRule(labels=labels, field_type=field_type)
    return rules


def load_schema(path: Union[str, Path]) -> dict[str, FieldRule]:
    schema_path = Path(path)
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    return build_schema(raw)
