import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm_agent import run_llm_agent



def main() -> int:
    # if len(sys.argv) < 2:
    #     print('Usage: python -m agent.llm_cli "<url>" [--business <name>] [--model <name>]', file=sys.stderr)
    #     return 1

    url = "https://www.ydamc.com/#/personal/funddetail?fundcode=017440"
    business = "fund_snapshot"
    parser = argparse.ArgumentParser(description="Run LLM + MCP extraction agent.")
    parser.add_argument("url", nargs="?", default=url, help="Target page URL")
    parser.add_argument("--business", default=business, help="Business config name in businesses/*.json")
    parser.add_argument("--model", default=None, help="Optional model name override")
    args = parser.parse_args()

    try:
        result = run_llm_agent(url=args.url, model=args.model, business=args.business)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print("LLM agent failed: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
