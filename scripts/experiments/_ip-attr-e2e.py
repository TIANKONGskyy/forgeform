"""端到端验证：IP 角色的细节是否真的进了提示词 + 显示的是不是真实参数。

背景（2026-10-15 修的两个问题）：
  ① 命中已知 IP 后走固定模板，用户说的属性**一个都进不了提示词** ——
     实测熊大那次提交给模型的是
       "Briar Bear, Boonie Bears cartoon style, standing in a friendly pose, ..."
     用户写的"红色马甲"彻底丢了 → 出图和需求对不上（用户报"准确性不行"）。
  ② 回复里那句「（qwen21_ref 8步 turbo）」是**写死的字面量**，
     和实际用的模型/步数/分辨率无关（qwen21_ref 配置 20 步、预览只跑 512）。

本脚本真跑一次生成（约 1~2 分钟，占 GPU），然后断言：
  · 提交给模型的英文提示词里带了 vest / brown
  · 回复里的参数行是**真实值**，且不再出现"8步 turbo"
  · 用的模型就是 config 里那个（qwen21_ref）

跑法：python scripts/_ip-attr-e2e.py
注意：会新建一个 projects/e2e_ip_attr_* 临时组，跑完可自行删除。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.conversation import ConversationManager  # noqa: E402

ZH = "熊出没里的熊大——棕色的卡通小熊，圆脸，红色马甲，开朗活泼，名字叫熊大，是光头强的对手"

cm = ConversationManager()
# prepare_describe 返回 (state, resolved, params) —— 见 conversation.py:911
state, _resolved, _params = cm.prepare_describe(ZH)
if not isinstance(state, dict):
    print("❌ prepare_describe 没返回 state:", type(state))
    sys.exit(1)

# 用独立组名，别动用户真实的那个
state["path"] = str(ROOT / "projects" / f"e2e_ip_attr_{int(time.time())}")
Path(state["path"]).mkdir(parents=True, exist_ok=True)
state["user_prompt"] = ZH
state["req_notes"] = [ZH]

print("=" * 70)
print("真跑一次生成（占 GPU，约 1~2 分钟）")
print("=" * 70)
print(f"  需求: {ZH}")
print(f"  组目录: {state['path']}")

t0 = time.time()
res = cm._do_generate_images(state)
elapsed = round(time.time() - t0)

reply = (res or {}).get("reply") or ""
images = (res or {}).get("images") or []
print(f"\n  用时 {elapsed}s")
print(f"  产出图 {len(images)} 张")

print("\n--- 回复原文 ---")
print(reply[:700])

print("\n" + "=" * 70)
print("断言")
print("=" * 70)

ok = True


def check(label, cond, extra=""):
    global ok
    print(f"  {'✅' if cond else '❌'} {label}  {extra}")
    ok = ok and bool(cond)


prompt_line = ""
for line in reply.splitlines():
    if line.strip().startswith(">"):
        prompt_line = line.strip("> ").strip()
        break

check("回复里亮出了英文提示词", bool(prompt_line), prompt_line[:90])
check("★ 提示词带上了「马甲」这个几何属性", "vest" in prompt_line.lower())
check("★ 提示词带上了用户说的颜色 brown", "brown" in prompt_line.lower())
check("提示词仍是 Briar Bear（IP 安全模板没丢）", "Briar Bear" in prompt_line)

check("★ 参数行不再出现写死的「8步 turbo」", "8步 turbo" not in reply)
check("参数行显示了真实模型名", "qwen21_ref" in reply)
# 真实档位是"快速档"+分辨率；只要不是那句假话即算过
param_line = next((l for l in reply.splitlines() if l.strip().startswith("（")), "")
check("参数行来自 StageResult.metadata（含档位/分辨率）",
      ("档" in param_line) or ("×" in param_line), param_line.strip())

# auto_params 这一步不落盘（_do_generate_images 只写候选图），从内存 state 里取。
ap = state.get("auto_params") or {}
check("★ auto_params 记的模型 = 实际用的 qwen21_ref",
      ap.get("model") == "qwen21_ref", f"记的是 {ap.get('model')}")

print()
print("=" * 70)
print("✅ 全部通过" if ok else "❌ 有失败")
print("=" * 70)
sys.exit(0 if ok else 1)
