import json
import queue
import re
import subprocess
import sys
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


class StdioMCPClient:
    def __init__(self, server_script: Path, cwd: Optional[Path] = None):
        self.server_script = Path(server_script)
        self.cwd = Path(cwd) if cwd else self.server_script.parent.parent
        self._proc: Optional[subprocess.Popen] = None
        self._request_id = 0

    def start(self) -> None:
        if self._proc is not None:
            return

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        self._proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(self.server_script)],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self.request("shutdown", {})
        except Exception:
            pass
        finally:
            self._proc.terminate()
            self._proc = None

    def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("MCP client is not started.")

        self._request_id += 1
        safe_params = _sanitize_json_value(params)
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": safe_params,
        }

        self._proc.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
        self._proc.stdin.flush()
        timeout_seconds = float(os.getenv("MCP_READ_TIMEOUT_SECONDS", "90"))
        line = self._read_stdout_line(timeout_seconds=timeout_seconds)
        if not line:
            stderr_text = ""
            if self._proc.stderr is not None:
                stderr_text = self._proc.stderr.read()
            raise RuntimeError("No MCP response. stderr={}".format(stderr_text))

        response = json.loads(line)
        if "error" in response:
            raise RuntimeError("MCP error: {}".format(response["error"]))
        return response.get("result", {})

    def _read_stdout_line(self, timeout_seconds: float) -> str:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("MCP client is not started.")

        result_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                result_queue.put(self._proc.stdout.readline())
            except Exception:
                result_queue.put("")

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            return result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
            raise TimeoutError("MCP request timed out after {} seconds.".format(timeout_seconds)) from exc

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self.request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        is_error = bool(result.get("isError", False))
        if not content:
            return "TOOL_ERROR: empty tool response" if is_error else ""
        text = content[0].get("text", "")
        return "TOOL_ERROR: {}".format(text) if is_error else text


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"[\ud800-\udfff]", "", value)
    return value
