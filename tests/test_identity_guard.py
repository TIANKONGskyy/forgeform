"""
识别特征保护（identity_guard）的回归测试。

★ 这组测试锁住的是**一个真实事故**（2026-10-17）：
  宇凯要"菠萝，去掉四肢，头部简约" → 出图是纯黄剪影
  → 3D 重建出一块带碎片的板（长薄比 145）→ 质量闸判不合格
  → 自动兜底重跑 → 还是板。白烧两次 GPU（约 20 分钟），用户拿到废品。

  根因不是代码 bug：这个需求**抹掉了菠萝唯一的识别特征**（叶冠+菱形纹理），
  扩散模型没法画"没有特征的菠萝"，只能给个色块。

  ★ 做法（宇凯 2026-10-17 定）：**不弹窗、不打断**，
    检测到就静默把会抹掉特征的要求剔掉，照常出图。
    （第一版做成 A/B 弹窗，被明确否掉："不要拦截机制"）
"""
import unittest

from backend import identity_guard as ig


class TestFeatureErasureDetection(unittest.TestCase):
    def test_菠萝简化需求必须识别出来(self):
        """事故原句 —— 这条是这组测试存在的理由。"""
        c = ig.feature_erasure_conflict(
            "菠萝，黄色 偏卡通 小尺寸，去掉四肢，头部改为简约风格")
        self.assertIsNotNone(c, "这条必须被识别，否则又会白烧 20 分钟 GPU")
        self.assertEqual(c["subject"], "菠萝")

    def test_整体简化指令也要识别(self):
        """用户不会说"去掉叶冠"，只会说"头部简约" —— 这类必须也能识别。"""
        for zh in ("菠萝，头部简约", "一个菠萝，极简风格", "菠萝，简化一下"):
            self.assertIsNotNone(ig.feature_erasure_conflict(zh), f"{zh} 应被识别")

    def test_显式抹除特征要识别(self):
        cases = {
            "菠萝，去掉叶冠": "菠萝",
            "草莓，去掉表面的籽": "草莓",
            "皮卡丘，不要闪电尾巴": "皮卡丘",
            "钢铁侠，去掉胸口反应堆": "钢铁侠",
        }
        for zh, subj in cases.items():
            c = ig.feature_erasure_conflict(zh)
            self.assertIsNotNone(c, f"{zh} 应被识别")
            self.assertEqual(c["subject"], subj)

    def test_正常需求绝不能误判(self):
        """误判会悄悄改掉用户的要求 —— 比漏判更糟。"""
        for zh in (
            "菠萝",                                    # 没提简化
            "漫威钢铁侠——穿金色红色机甲，胸口有弧形反应堆",  # 反而强调特征
            "一个简单的圆形杯垫，直径 90 毫米",
            "一个六边形的笔筒",
            "原神里的钟离，岩王帝君造型",
            "菠萝，去掉背景",                          # 去背景与识别性无关
            "菠萝，不要背景不要文字",
            "一个菠萝，去掉底座",
            "茶杯垫，极简",                            # 基础几何体，简化不影响识别
            "笔筒，去掉盖子",
            "一个自定义角色，简约风格",                # 表里没有的主体 → 不判
        ):
            self.assertIsNone(ig.feature_erasure_conflict(zh), f"{zh} 被误判了")


class TestSilentAutoFix(unittest.TestCase):
    """检测到冲突后，静默把抹除要求剔掉（不问用户）。"""

    def test_剔除抹除类分句保留其余(self):
        from backend.conversation import ConversationManager
        got = ConversationManager._strip_erasing_words(
            None, "菠萝，黄色卡通，去掉四肢，头部简约")
        self.assertEqual(got, "菠萝，黄色卡通",
                         "抹除类分句要去掉，颜色/风格要保留")

    def test_没有抹除词时原样返回(self):
        from backend.conversation import ConversationManager
        zh = "菠萝，黄色，小尺寸"
        self.assertEqual(ConversationManager._strip_erasing_words(None, zh), zh)

    def test_修正后不再触发冲突(self):
        """修正完必须能顺利生成 —— 否则每次都被自己拦。"""
        from backend.conversation import ConversationManager
        zh = "菠萝，黄色卡通，去掉四肢，头部简约"
        self.assertIsNotNone(ig.feature_erasure_conflict(zh))
        fixed = ConversationManager._strip_erasing_words(None, zh)
        self.assertIsNone(ig.feature_erasure_conflict(fixed),
                          f"修正后仍是冲突：{fixed}")


if __name__ == "__main__":
    unittest.main()
