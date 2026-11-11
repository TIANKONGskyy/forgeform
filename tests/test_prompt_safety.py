"""回归测试：生图提示词的「主体不能丢、中文不能喂」。

覆盖 2026-10-15 实测抓到的真实事故（用户报「要浣熊，图是对的，但最后建模出来一个人」）：

  · 空需求 → base_prompt 空 → enhance_image_prompt("") **静默返回空串**
    （不抛异常也不打日志）→ 兜底拼出 "a high quality 3D figurine of , ..."
    **主体位置是空的** → 扩散模型自己随机编主体（卡通小男孩 / 女战士，每次不同）
  · _OBJECT_EN 原来 38 条**全是物件**（杯垫/笔筒/齿轮…），一个动物都没有
    → 任何动物都掉进"把中文原样留着"的分支 → 中文进扩散模型 → 主题漂移

全为纯逻辑、不加载 GPU。词表命中和兜底模板都是**确定性**的；
只有"词表没命中 → 试翻译"那条会走网络，这里用桩把它按失败处理（正好也是线上常态：
GPU 被占时那条翻译要 31~39s，60s 超时后就是失败）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.describer as de  # noqa: E402


class TestPromptSubjectSafety(unittest.TestCase):
    def test_空需求返回空串_调用方据此拒绝生成(self):
        """空串是"本组没有需求"的信号 —— 调用方必须拦下，不能拿去生成。

        这个返回值本身没错，错的是调用方以前硬着头皮往下走：
        详见 conversation.py 里"需求为空时拒绝生成"那段。
        """
        self.assertEqual(de.enhance_image_prompt(""), "")
        self.assertEqual(de.enhance_image_prompt("   "), "")

    def test_常见动物必须命中词表且是英文(self):
        """事故主角。这些原来一条都不在表里，全掉进"保留中文"分支。"""
        cases = [
            ("一只大象，写实风格，全身，站在地面上", "elephant"),
            ("一只浣熊，写实风格", "raccoon"),
            ("一条龙", "dragon"),
            ("一只猫", "cat"),
            ("一只小狗", "puppy"),
            ("一个机器人", "robot"),
        ]
        for zh, want in cases:
            out = de._safe_english_template(zh)
            self.assertIn(want, out, f"{zh} → 兜底没给出 {want}：{out}")
            self.assertFalse(de._has_cn(out), f"{zh} → 兜底里还留着中文：{out}")

    def test_最长优先_短词不能抢长词(self):
        """"猫头鹰" 必须命中 owl，不能被 "猫" 抢走；否则又变回错误主体。"""
        self.assertIn("owl", de._safe_english_template("一只猫头鹰"))
        self.assertIn("giant panda", de._safe_english_template("一只大熊猫"))

    def test_词表没命中也不许把中文喂给模型(self):
        """翻译失败（线上常态）时必须把中文删干净，而不是原样传下去。

        原实现是 `subj_en = zh`，注释还写着"至少材质/风格没错"——
        实测这个赌注是错的，且后果严重得多：模型会自己补一个完全无关的主体。
        """
        with mock.patch("backend.prompt_enhancer.translate", return_value=""):
            out = de._safe_english_template("一个奇奇怪怪的东西xyz")
        self.assertFalse(de._has_cn(out), f"兜底里还留着中文：{out}")
        self.assertTrue(out.strip(), "兜底不能返回空串 —— 主体整个没了比认不出更糟")

    def test_兜底永远带风格描述(self):
        """兜底是在"扩写失败"时用的，至少要保住画质/风格那一半。"""
        with mock.patch("backend.prompt_enhancer.translate", return_value=""):
            out = de._safe_english_template("一个奇奇怪怪的东西xyz")
        self.assertIn("clean light background", out)

    def test_翻译成功则用翻译结果(self):
        """能翻出来就用翻译结果，别去删中文。"""
        with mock.patch("backend.prompt_enhancer.translate", return_value="a mysterious gizmo"):
            out = de._safe_english_template("一个奇奇怪怪的东西xyz")
        self.assertIn("mysterious gizmo", out)
        self.assertFalse(de._has_cn(out))


class TestKnownIpRecognition(unittest.TestCase):
    """回归：已知 IP 必须走**确定性模板**，不能交给 LLM 猜主体。

    ══════════════════════════════════════════════════════════════
    2026-10-16 实测事故（宇凯报「我要钢铁侠，出图是小黄人」）
    ══════════════════════════════════════════════════════════════
      根因：IP 别名表只有 11 项（熊出没/海绵宝宝/佩奇/猫和老鼠），
            **没有钢铁侠** → 掉进 LLM 扩写那条路 →
            模型把
              "漫威钢铁侠——穿金色红色机甲，胸口有弧形反应堆，
               名字叫托尼·斯塔克风格的零件，红色，机械风格"
            **认成了 Minion / yellow**，出图就是小黄人，建模跟着错。

      决定性对比（同一角色，换说法结果相反）：
        "钢铁侠"        → Iron Man ... glowing arc reactor   ✓
        上面那句长句     → Minion, yellow, cartoon style      ✗

      即**修饰语一多，主体就被淹没**。所以修法是：把高频 IP 补进表里，
      让它们走查表（零猜测），而不是继续赌 LLM 认不认得出。
    """

    def test_钢铁侠长句必须识别成_Iron_Man_而不是_Minion(self):
        """事故原句 —— 加表之前它出的是小黄人。"""
        zh = "漫威钢铁侠——穿金色红色机甲，胸口有弧形反应堆，名字叫托尼·斯塔克风格的零件，红色，机械风格"
        got = de._detect_known_ip(zh)
        self.assertIsNotNone(got, "没命中已知 IP → 又会交给 LLM 猜主体 ✗")
        base = got["base_en"].lower()
        self.assertIn("iron man", base)
        for bad in ("minion", "yellow", "boonie"):
            self.assertNotIn(bad, base, f"混进了 {bad!r} —— 正是当初出小黄人的原因")

    def test_钢铁侠干脆说也要命中(self):
        got = de._detect_known_ip("钢铁侠")
        self.assertIsNotNone(got)
        self.assertIn("iron man", got["base_en"].lower())

    def test_命中IP走模板时不调LLM(self):
        """★ IP 分支存在的意义就是绕开对话模型（历史上 80% 概率超时）。

        用桩让 _chat_call 抛错 —— 走模板就不该碰到它。
        """
        with mock.patch.object(de, "_chat_call", side_effect=AssertionError("走了 LLM ✗")):
            out = de.enhance_image_prompt("钢铁侠")
        self.assertIn("Iron Man", out)

    def test_机甲IP不能拼卡通风格(self):
        """风格后缀必须按 IP 分类。

        原实现给**所有** IP 硬拼 "Boonie Bears cartoon style" ——
        表里当初只有熊出没所以写死了；现在加了漫威，
        再拼熊出没会把钢铁侠画成熊 ✗
        """
        got = de._detect_known_ip("钢铁侠")
        base = got["base_en"]
        self.assertNotIn("Boonie Bears", base)
        self.assertIn("metallic armor", base.lower())

    def test_原有IP没被弄坏(self):
        """加新角色不能影响老角色（回归护栏）。"""
        for zh, want in (("熊大", "Briar Bear"), ("海绵宝宝", "SpongeBob"),
                         ("小猪佩奇", "Peppa Pig"), ("汤姆", "Tom Cat")):
            got = de._detect_known_ip(zh)
            self.assertIsNotNone(got, f"{zh} 掉出表了")
            self.assertIn(want.lower(), got["base_en"].lower(), f"{zh} 映射错了")

    def test_普通物件不该被误判成IP(self):
        """茶杯垫/机械臂底座这类不能命中任何 IP —— 否则会被套上角色模板。"""
        for zh in ("一个茶杯垫", "机械臂底座", "六角笔筒"):
            self.assertIsNone(de._detect_known_ip(zh), f"{zh} 被误判成 IP 了")


class TestUnknownSubjectHonesty(unittest.TestCase):
    """未知角色要**如实说不知道 + 要细节**，不能硬猜。

    ══════════════════════════════════════════════════════════════
    2026-10-16 宇凯的要求（比"补 IP 表"更根本）
    ══════════════════════════════════════════════════════════════
      "重点不是补上IP而是确认需求，如果是已知的IP还好说，但是如果是未知的
       我们要实事求是的说不知道，然后根据用户的反馈以及细节描述来生成"

      IP 表是**有限**的 —— 补表只能覆盖已想到的角色。
      真正的解法是承认不知道，并用用户自己的描述去生成。

    ── 实测：表外角色有两条坏路径 ──
      ① 被认成别的角色（钢铁侠 → Minion / yellow）
      ② ★ 主体被**整个抹掉**：实测「原神里的钟离，岩王帝君造型」扩写成
         "stylized cartoon character, ..." —— 没有任何主体。
         这条最隐蔽：输出看着"正常"，模型却会随机发挥。
    """

    # 实测抓到的真实输出（钟离那次主体真丢了）
    _ZH_ZL = "原神里的钟离，岩王帝君造型"
    _EN_LOST = ("stylized cartoon character, faithful to the original cartoon design, "
                "smooth simplified forms, collectible toy figurine, no realistic human anatomy, "
                "no skin pores, character portrait, standing figurine, mild pose, "
                "plain white background, ultra detailed, sharp focus")

    def test_主体被抹掉要能检测出来(self):
        """事故原样 —— 这条必须判成"丢了"，否则会白烧 GPU 出个随机角色。"""
        self.assertTrue(de.subject_lost(self._ZH_ZL, self._EN_LOST))

    def test_用户补完外观描述后不该再拦(self):
        """按引导补了体型/颜色/特征 → 主体落地 → 放行生成。"""
        zh = "原神里的钟离，岩王帝君造型，人形，棕色长发，黑色长袍带金色纹路，手持长枪"
        en = ("Zhongli, humanoid character, long brown hair, black robe with golden patterns, "
              "holding a spear, brown and gold color scheme, standing pose, collectible figurine, "
              "plain white background, ultra detailed, sharp focus")
        self.assertFalse(de.subject_lost(zh, en))

    def test_名字被翻译过去也算主体在(self):
        """★ 误报防护：中文名翻成英文后，原中文不可能出现在英文里。

        「我的世界里的苦力怕」→ "minecraft creeper" 完全正确，
        但查中文词必然查不到 —— 只按关键词匹配会误判成"丢了"、拦住正常生成 ✗
        """
        self.assertFalse(de.subject_lost(
            "我的世界里的苦力怕",
            "minecraft creeper, stylized cartoon character, vibrant green body with glowing eyes, "
            "smooth simplified forms, collectible toy figurine, plain white background",
        ))

    def test_引导文案要如实说要细节(self):
        msg = de.describe_unknown_subject(self._ZH_ZL)
        self.assertIn("没有把握", msg, "必须如实说不知道，不能装作认得出")
        self.assertIn("外观描述", msg, "要引导用户给描述")
        for dim in ("体型", "主色调", "标志性特征"):
            self.assertIn(dim, msg, f"缺少可操作维度 {dim}")
        self.assertIn("钟离", msg, "复述用户原话，方便他接着补充")
        self.assertNotIn("尽力", msg, "不要'我尽力'这种含糊承诺")

    def test_普通物件与已知IP都不该被误拦(self):
        """误拦比漏拦更烦人 —— 正常需求必须放行。"""
        for zh, en in (
            ("一个六边形笔筒", "hexagonal pen holder, matte white plastic, minimalist design"),
            ("一个茶杯垫", "round coaster, cork material, clean product render"),
            ("钢铁侠", "Iron Man, Marvel superhero style, glossy metallic armor, red, golden"),
        ):
            self.assertFalse(de.subject_lost(zh, en), f"{zh} 被误拦了")


class TestUnknownCharacterSpecRule(unittest.TestCase):
    """★ 未知角色的**真正**根因：扩写 spec 缺"认不出时怎么办"这一支。

    ══════════════════════════════════════════════════════════════
    2026-10-17 实测暴露（比"主体被抹掉"更深一层）
    ══════════════════════════════════════════════════════════════
      现象：请求「原神里的钟离」，出图是**小黄人**。

      追查过程（两次不同的失败，别混为一谈）：
        ① 第一次：扩写直接丢掉主体 → "stylized cartoon character, ..."
           → 这条由 subject_lost() 拦（见上一个测试类）
        ② 第二次：扩写**写了** "Zhongli from Genshin Impact, long brown hair,
           black robe with golden patterns" —— 看着完全正确，
           但图模型**照样画了小黄人** ✗
           → subject_lost 判"主体在"是对的（提示词里确实有），
             拦不住，因为问题不在提示词，在**图模型不知道钟离长什么样**，
             于是抓了它最熟的"卡通角色"= Minion。

      根因：_IMG_PROMPT_SPEC 只有三支 —— IP角色 / 工业件 / 家居件，
           **没有"未知角色"这一支**。未知角色掉进 IP 那支，而 IP 那支
           要求"faithful to the original design / keep the canonical
           proportions"—— 对一个不认识的角**根本做不到**，
           模型只能拿最刻板的卡通形象顶替 ✗

      修法：给 spec 加 ★★★ 规则 —— 认不出就**别装忠实原作**，
           只用用户给的外观词描述（体型/颜色/衣物/特征），
           宁可简短也不冒充别的角色。
    """

    def test_spec_必须有未知角色这一支(self):
        """没有这一支，未知角色就会被套进「忠实原作」模板 → 画出小黄人。"""
        spec = de._IMG_PROMPT_SPEC
        self.assertIn(
            "NOT a well-known IP", spec,
            "spec 缺未知角色分支 —— 又会退回 faithful to the original 模板",
        )

    def test_spec_明确禁止退回通用卡通模板(self):
        """通用卡通模板正是 Minion 的来源，必须显式禁止。"""
        spec = de._IMG_PROMPT_SPEC
        self.assertIn("Do NOT fall back to a generic cartoon-character template", spec)
        self.assertIn("Minion", spec, "要把实测后果写进 spec，模型才知道不能这么干")

    def test_spec_要求按用户描述而非原作(self):
        """认不出时只能用用户的话描述外观，不能声称 canon 准确。"""
        spec = de._IMG_PROMPT_SPEC
        self.assertIn("PURELY from what the USER said", spec)
        self.assertIn("NEVER substitute a different named character", spec)


if __name__ == "__main__":
    unittest.main()
