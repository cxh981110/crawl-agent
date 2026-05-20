import json
import traceback
import ast
import sys
from pathlib import Path
from typing import Any, Callable, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.get_html_content import get_html_content
from tools.parse_pdf_content import parse_pdf_content


class ToolSpec:
    def __init__(self, name: str, description: str, input_schema: Dict[str, Any], handler: Callable[..., Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


TOOLS: Dict[str, ToolSpec] = {
    "get_html_content": ToolSpec(
        name="get_html_content",
        description=(
            "Fetch and clean a web page. Returns cleaned HTML, visible text, and detected tables. "
            "Does not extract business fields."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait_seconds": {"type": "number", "default": 3.0},
                "timeout": {"type": "integer", "default": 20},
                "section_hint": {
                    "type": "string",
                    "default": "",
                    "description": "Optional visible section/tab text to activate before reading the page.",
                },
                "auto_paginate": {"type": "boolean", "default": False},
                "max_pages": {"type": "integer", "default": 1},
                "next_selector": {"type": "string", "default": ""},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=get_html_content,
    ),
    "parse_pdf_content": ToolSpec(
        name="parse_pdf_content",
        description=(
            "Download and parse a PDF. Returns page text and detected tables. "
            "Does not extract business fields."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer", "default": 20},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=parse_pdf_content,
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
