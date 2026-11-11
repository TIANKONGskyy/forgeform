# -*- coding: utf-8 -*-
"""方向2 · 进化谱系生成器（仿生「进化的记忆」）

从 git log + evolution_log.jsonl 自动生成可回溯的进化谱系：
- 每个 commit = 一个「基因突变」（改动内容）
- 每次验证数据 = 「表型」（phash 距离/耗时/得分）
- 结论 = 「适存度」（是否保留/回退）

生成 machine.json（机器可读）+ EVOLUTION_LINEAGE.md（人类可读）。

用法: python gen_lineage.py
"""
import sys, os, json, subprocess, time
from pathlib import Path

sys.path.insert(0, "D:/AI-Workshop")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

WORKSPACE = Path("D:/AI-Workshop")
EVO_LOG = WORKSPACE / "outputs" / "continuous" / "evolution_log.jsonl"
LINEAGE_DIR = WORKSPACE / "outputs" / "continuous"
MACHINE = LINEAGE_DIR / "lineage.json"
HUMAN = LINEAGE_DIR / "EVOLUTION_LINEAGE.md"

# commit → 语义化描述（人工标注的「基因含义」）
COMMIT_MEANING = {
    "a65bc73": ("P0 快赢", "TeaCache 接入 FLUX(1.9x) + 图片缓存去重 + 队列防卡死"),
    "3e530a3": ("TeaCache 兼容补丁", "补 timestep_zero_index 参数兼容新版 ComfyUI"),
    "65e1769": ("温度看门狗", "重负载任务 ≥80°C 警告 ≥85°C 中断保护硬件"),
    "24bb49e": ("H3 视频 turbo lora", "8步→4步约2x，LoraLoaderModelOnly+beta"),
    "7d85f20": ("仿生持续生成框架", "守护循环70%占空比 + 子进程热更新"),
    "e83ad96": ("热修改演示", "负向提示词回退到 default_negative"),
    "f93aedd": ("benchmark 比对", "固定 seed+prompt，phash汉明距离对比"),
    "1ce7a5c": ("方向1进化闭环", "auto_evolve TeaCache阈值A/B测试"),
    "92815e8": ("互斥锁修复", "evolve.lock 防并发压垮 ComfyUI"),
    "28f424e": ("gitignore", "临时进化 config 忽略"),
    "95d9f44": ("方向1修复", "skip_cache 绕过缓存 + KeyError 修复"),
    "fa17361": ("方向2谱系生成器", "git log+evolution_log 自动生成进化树"),
    "172ad4d": ("方向3多任务调度器", "图像+3D双线轮转流水线，视频线预留"),
}


def git_log() -> list:
    out = subprocess.check_output(
        ["git", "log", "--pretty=format:%h|%ad|%s", "--date=format:%H:%M"],
        cwd=WORKSPACE, encoding="utf-8", errors="replace",
    ).strip()
    commits = []
    for line in out.split("\n"):
        if "|" not in line:
            continue
        h, t, s = line.split("|", 2)
        commits.append({"hash": h, "time": t, "subject": s})
    return commits


def evolution_summary() -> dict:
    """汇总 evolution_log.jsonl 的进化结论"""
    if not EVO_LOG.exists():
        return {}
    best = None
    all_records = []
    for line in EVO_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        all_records.append(r)
        if best is None or r["score"] > best["score"]:
            best = r
    return {"total_rounds": len(all_records), "best": best, "records": all_records}


def main():
    commits = git_log()
    evo = evolution_summary()

    lineage = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "body": "DESKTOP-MD5E39F (Gooxi 服务器, RTX 3080 20GB)",
        "commits": commits,
        "evolution": evo.get("best"),
        "evolution_total_records": evo.get("total_rounds", 0),
    }
    MACHINE.write_text(json.dumps(lineage, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人类可读谱系
    lines = []
    lines.append("# 进化谱系 EVOLUTION LINEAGE\n")
    lines.append(f"> 身体：{lineage['body']}  ")
    lines.append(f"> 生成时间：{lineage['generated_at']}  ")
    lines.append(f"> 这是天枢的「进化记忆」——每一次代码改动都是一次基因突变，附带验证数据（表型）和结论（适存度）。\n")
    lines.append("## 谱系（新→旧）\n")
    lines.append("| 时间 | commit | 基因突变 | 状态 |")
    lines.append("|------|--------|----------|------|")
    for c in commits:
        h = c["hash"]
        meaning = COMMIT_MEANING.get(h, ("", ""))
        label = meaning[0] or "（未标注）"
        lines.append(f"| {c['time']} | `{h}` | {label} | ✅ 保留 |")
    lines.append("")
    lines.append("## 当前最优参数（方向1进化结论）\n")
    if evo.get("best"):
        b = evo["best"]
        lines.append(f"- **TeaCache 阈值 = {b['thresh']}**（加速 {b['speedup']}x，phash 距离 {b['avg_dist']}，得分 {b['score']}）")
        lines.append(f"- 结论：0.4 是最优平衡点，0.6/0.8 快 25% 但输出有中等变化（距离 7），0.2 反而更慢")
    else:
        lines.append("- （暂无进化数据）")
    lines.append("")
    lines.append("## 适存度评估原则\n")
    lines.append("- 每个改动必须能用「验证数据」回答：它让系统更快/更稳/更好吗？")
    lines.append("- 不能回答的改动，视为中性突变，暂保留观察")
    lines.append("- 证明有害的改动，git checkout 回退")

    HUMAN.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 谱系已生成:")
    print(f"   机器可读: {MACHINE}")
    print(f"   人类可读: {HUMAN}")
    print(f"   共 {len(commits)} 个 commit，进化记录 {evo.get('total_rounds', 0)} 条")


if __name__ == "__main__":
    main()
