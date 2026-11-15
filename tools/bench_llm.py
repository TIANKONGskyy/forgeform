"""实测对话模型吞吐，写进 outputs/llm_bench.json（侧栏「对话 · LLM」读它）。

用法：
    PYTHONPATH= python tools/bench_llm.py
    PYTHONPATH= python tools/bench_llm.py --url http://127.0.0.1:8091/v1 --model qwen3.8-flash-next-iq3_s
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model_info import bench_llm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091/v1")
    ap.add_argument("--model", default="")
    args = ap.parse_args()
    try:
        rec = bench_llm(args.url, args.model)
    except Exception as exc:
        print(f"bench 失败: {type(exc).__name__}: {exc}")
        print("Strata 没起？确认 8091 在跑：curl http://127.0.0.1:8091/v1/models")
        return 1
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"\n生成 {rec['predicted_tok_s']} tok/s，预填 {rec['prompt_tok_s']} tok/s"
          f"（{rec['measured_at']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
