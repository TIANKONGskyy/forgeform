"""实测对话模型吞吐，追加进 outputs/llm_bench.jsonl（滚动窗口，最近 20 次）。

侧栏「对话 · LLM」读的是这堆样本的中位数，不是单次。

用法：
    PYTHONPATH= python tools/bench_llm.py            # 跑 1 次
    PYTHONPATH= python tools/bench_llm.py -n 5       # 连跑 5 次
    PYTHONPATH= python tools/bench_llm.py --url http://127.0.0.1:8091/v1
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model_info import bench_llm, llm_bench, describe_llm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091/v1")
    ap.add_argument("--model", default="")
    ap.add_argument("-n", "--count", type=int, default=1, help="跑几次")
    args = ap.parse_args()

    ok = 0
    for i in range(max(1, args.count)):
        try:
            rec = bench_llm(args.url, args.model)
        except Exception as exc:
            print(f"第 {i+1} 次失败: {type(exc).__name__}: {exc}")
            if i == 0:
                print("Strata 没起？确认 8091 在跑：curl http://127.0.0.1:8091/v1/models")
                return 1
            continue
        ok += 1
        print(f"  第 {i+1} 次: {rec['predicted_tok_s']} tok/s"
              f"（预填 {rec['prompt_tok_s']}，投机接受 {rec['draft_accepted']}）")

    agg = llm_bench()
    print()
    print(f"窗口样本 n={agg.get('n')}/{agg.get('window')}"
          f"  中位 {agg.get('predicted_tok_s')} tok/s"
          f"  区间 {agg.get('predicted_min')}–{agg.get('predicted_max')}")
    print(f"预填中位 {agg.get('prompt_tok_s')} tok/s")
    print()
    print("侧栏会显示：")
    print("  " + describe_llm()["timing"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
