import json
import traceback
import ast
import sys
from pathlib import Path
from typing import Any, Callable, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.extract_fields import extract_fields
from tools.extract_list_rows import extract_list_rows
from tools.extract_table_rows import extract_table_rows


class ToolSpec:
    def __init__(self, name: str, description: str, input_schema: Dict[str, Any], handler: Callable[..., Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


TOOLS: Dict[str, ToolSpec] = {
    "extract_fields": ToolSpec(
        name="extract_fields",
        description=(
            "Extract structured fields from URL based on fields_json config. "
            "HTML fetch + cleaning + context compression are fixed internal steps."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "fields_json": {
                    "type": "string",
                    "description": "JSON object as string that defines field labels/types",
                },
                "token_budget": {"type": "integer", "default": 3000},
            },
            "required": ["url", "fields_json"],
            "additionalProperties": False,
        },
        handler=extract_fields,
    ),
    "extract_table_rows": ToolSpec(
        name="extract_table_rows",
        description=(
            "Extract repeated table rows from URL using row_schema_json. "
            "Returns data as a list of objects, one item per table row."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "row_schema_json": {
                    "type": "string",
                    "description": "JSON object string that defines row fields and labels",
                },
                "table_hint": {
                    "type": "string",
                    "default": "",
                    "description": "Optional hint to locate target table (e.g. table title or keyword).",
                },
                "auto_paginate": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to click pager controls and merge all collected pages.",
                },
                "max_pages": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of pages to collect when auto_paginate is enabled.",
                },
                "next_selector": {
                    "type": "string",
                    "default": "",
                    "description": "Optional CSS/XPath selector for the next-page control.",
                },
            },
            "required": ["url", "row_schema_json"],
            "additionalProperties": False,
        },
        handler=extract_table_rows,
    ),
    "extract_list_rows": ToolSpec(
        name="extract_list_rows",
        description=(
            "Extract repeated records from list-style pages (announcements/news/notices). "
            "Returns data as a list of objects."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "row_schema_json": {
                    "type": "string",
                    "description": "JSON object string that defines row fields and labels",
                },
                "list_selector": {
                    "type": "string",
                    "default": "",
                    "description": "Optional CSS selector for list container.",
                },
                "item_selector": {
                    "type": "string",
                    "default": "",
                    "description": "Optional CSS selector for row/item nodes.",
                },
                "date_regex": {
                    "type": "string",
                    "default": "",
                    "description": "Optional date regex for publish date extraction.",
                },
                "max_items": {"type": "integer", "default": 100},
                "auto_paginate": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to click pager controls and merge all collected pages.",
                },
                "max_pages": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of pages to collect when auto_paginate is enabled.",
                },
                "next_selector": {
                    "type": "string",
                    "default": "",
                    "description": "Optional CSS/XPath selector for the next-page control.",
                },
            },
            "required": ["url", "row_schema_json"],
            "additionalProperties": False,
        },
        handler=extract_list_rows,
    ),
}


def _write_response(message_id: Any, result: Any = None, error: Dict[str, Any] = None) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _handle_tools_list(message_id: Any) -> None:
    tools = []
    for spec in TOOLS.values():
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
            }
        )
    _write_response(message_id, {"tools": tools})


def _handle_tools_call(message_id: Any, params: Dict[str, Any]) -> None:
    tool_name = params.get("name")
    arguments = params.get("arguments", {}) or {}
    spec = TOOLS.get(tool_name)
    if spec is None:
        _write_response(
            message_id,
            error={"code": -32602, "message": "Unknown tool: {}".format(tool_name)},
        )
        return

    try:
        output = spec.handler(**arguments)
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        _write_response(message_id, {"content": [{"type": "text", "text": output}], "isError": False})
    except Exception as exc:
        _write_response(
            message_id,
            {"content": [{"type": "text", "text": str(exc)}], "isError": True},
        )


def main() -> None:
    for line in iter(input, ""):
        line = line.strip()
        if not line:
            continue

        try:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                parsed = ast.literal_eval(line)
                if not isinstance(parsed, dict):
                    raise ValueError("Invalid message payload type.")
                message = parsed
            message_id = message.get("id")
            method = message.get("method")
            params = message.get("params", {}) or {}

            if method == "tools/list":
                _handle_tools_list(message_id)
            elif method == "tools/call":
                _handle_tools_call(message_id, params)
            elif method == "shutdown":
                _write_response(message_id, {"ok": True})
                break
            else:
                _write_response(message_id, error={"code": -32601, "message": "Method not found: {}".format(method)})
        except EOFError:
            break
        except Exception:
            _write_response(
                None,
                error={"code": -32000, "message": traceback.format_exc()},
            )


if __name__ == "__main__":
    main()
