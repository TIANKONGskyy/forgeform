"""意图分类器：根据用户输入判断当前阶段和操作（纯规则，无 LLM API）"""

import re
from enum import Enum


class Intent(Enum):
    # 阶段 0
    DESCRIBE_OBJECT  = "描述物体（进入需求确认）"
    # 阶段 1
    START_GENERATE   = "开始生成图片"
    REGENERATE       = "重新生成"
    # 阶段 2
    SELECT_IMAGE     = "选中某张图（进入下一阶段）"
    REFINE           = "修改/微调当前图"
    # 阶段 3
    START_3D         = "开始3D建模"
    # 阶段 4
    EXPORT_STL       = "导出STL"
    OPEN_BAMBU      = "打开Bambu Studio"
    # 全局
    NEW_PROJECT      = "新建对话组"
    UNKNOWN          = "无法识别"
    # 细化（仍在阶段0）
    REFINE_REQUIREMENT = "继续细化要求"


# 实体别名映射（常见卡通/角色 -> 标准描述）
ENTITY_ALIASES = {
    "熊大": "熊出没里的熊大——棕色的卡通小熊，圆脸，红色马甲（后来穿绿色工装裤），开朗活泼，名字叫熊大，是光头强的对手",
    "熊二": "熊出没里的熊二——棕色的卡通小熊，比熊大略矮，橙色鼻子，喜欢吃蜂蜜，性格憨厚可爱",
    "光头强": "熊出没里的光头强——人类，伐木工，戴安全帽，常拿锯子，想砍树却被熊大熊二阻止",
    "机器猫": "哆啦A梦——蓝色机器猫，圆脑袋，脖子上有铃铛，口袋里能掏出各种神奇道具，来自未来",
    "哆啦A梦": "哆啦A梦——蓝色机器猫，圆脑袋，脖子上有铃铛，口袋里能掏出各种神奇道具，来自未来",
    "皮卡丘": "宝可梦的皮卡丘——黄色的电老鼠，尖耳朵，脸颊能放电流，尾巴是闪电形状，是小智最重要的伙伴",
    "蜘蛛侠": "漫威蜘蛛侠——穿红色蓝色紧身衣，面罩遮脸，能吐丝爬墙，身手敏捷",
    "蝙蝠侠": "DC蝙蝠侠——穿黑色披风，戴蝙蝠面罩，是哥谭市的义警",
    "钢铁侠": "漫威钢铁侠——穿金色红色机甲，胸口有弧形反应堆，名字叫托尼·斯塔克",
    "马里奥": "超级马里奥——红帽子红衣服的意大利水管工，戴手套，留胡子，吃蘑菇能变大",
    "路易吉": "超级路易吉——马里奥的弟弟，绿衣服绿帽子，比马里奥跳得高",
    "悟空": "七龙珠的孙悟空——橙衣服黑头发，能变超级赛亚人，能龟派气功",
    "鸣人": "火影忍者的漩涡鸣人——橙衣金发，脸上有胡须印记，擅长影分身",
    "路飞": "海贼王的路飞——草帽海贼团船长，橡胶果能力者，戴草帽，伤疤从眼角到嘴边",
    "高达": "高达/ Gundam——大型人形机甲，通常 18 米高，驾驶舱在胸口或头部，武器丰富",
    "霸王龙": "霸王龙/T-Rex——史前大型食肉恐龙，双足行走，短小前肢，巨大头部和颌部",
}


def _clean(text: str) -> str:
    return text.strip().lower()


def classify(text: str, current_phase: str) -> tuple[Intent, str]:
    """
    返回 (Intent, extracted_arg)
    extracted_arg 对某些意图有意义（如选中了哪张图）
    """
    t = _clean(text)
    original = text.strip()

    # 全局关键词（任何阶段都优先匹配）
    if any(k in t for k in ["新建项目", "新对话", "新对话组", "重新开始", "下一个", "开个新的"]):
        return Intent.NEW_PROJECT, ""

    # 阶段 0 - 需求确认阶段（含细化中）
    if current_phase in ("empty", "confirm", "refining"):
        # 明确要生成的指令
        if any(k in t for k in ["生成图片", "开始生成", "画出来", "生图"]):
            return Intent.START_GENERATE, ""
        # 继续细化
        if any(k in t for k in ["调整", "改一下", "细化", "细化要求", "继续细化", "改改", "改个"]):
            return Intent.REFINE_REQUIREMENT, original
        # 描述物体（细化阶段里，用户输入的是细化内容，但可能含"我要"等词，仍应走细化合并）
        if current_phase == "refining":
            # 细化中：任何输入都当作细化内容（除了生图/新建/细化，已在上面处理）
            return Intent.REFINE, original
        if any(k in t for k in ["我要", "帮我", "做个", "做一个", "画一个", "生成一个", "设计一个", "打印", "想打印"]):
            return Intent.DESCRIBE_OBJECT, original

        # 默认认为是描述
        return Intent.DESCRIBE_OBJECT, original

    # 阶段 1 - 生成图片阶段
    if current_phase == "generating":
        if any(k in t for k in ["重新生成", "再来", "换一批", "再来几张"]):
            return Intent.REGENERATE, ""
        # 序号优先匹配："选第二张"/"第二张" 等
        for i in ["选第", "第1", "第2", "第3", "1", "2", "3"]:
            if i in t:
                idx_map = {"选第": -1, "第1": 0, "第2": 1, "第3": 2, "1": 0, "2": 1, "3": 2}
                if i == "选第":
                    for num, idx in [("一", 0), ("二", 1), ("三", 2)]:
                        if num in t:
                            return Intent.SELECT_IMAGE, str(idx)
                else:
                    return Intent.SELECT_IMAGE, str(idx_map.get(i, 0))
        if any(k in t for k in ["可以了", "可以", "这张行", "选这张", "选这张图", "用这张", "这张可以", "确认"]):
            return Intent.SELECT_IMAGE, "0"   # 默认选第一张
        if any(k in t for k in ["微调", "修改", "调整", "改一下", "改一改", "这张不满意", "不太满意", "换"]):
            return Intent.REFINE, original
        if any(k in t for k in ["继续", "生成", "开始"]):
            return Intent.REGENERATE, ""
        return Intent.UNKNOWN, original

    # 阶段 2 - 图片确认/修改阶段
    if current_phase == "selecting":
        # 顺序很重要（每条都有回归用例）：
        #   先排「明确指令」→ 再排「新的需求描述」→ 最后才是宽松关键字，
        #   避免「我想打印一只小猫」被"打印"抢成 OPEN_BAMBU、
        #   「生成3D」里的 3 被序号匹配抢成 SELECT_IMAGE（2026-09-22 修）。
        if any(k in t for k in ["重新建模", "重新生成3d", "再生成3d", "换个3d", "换一个3d"]):
            return Intent.START_3D, ""
        if any(k in t for k in ["重新生成", "重新画", "换一批", "再来几张",
                                "重新做", "重做", "再做", "再来一张", "重新来"]):
            return Intent.REGENERATE, ""
        # 不满 / 要改图（原关键字太窄：「图片有问题」「这图不行」「不好看」
        # 「换个颜色」全被判成"新需求"，导致用户说了图有问题却完全没改图）
        if any(k in t for k in [
            # 不满意
            "有问题", "问题", "不行", "不好", "不好看", "不满意", "难看", "丑", "不对",
            "错了", "不像", "太假", "模糊", "奇怪", "重新做一", "不满意",
            # 要修改
            "继续修改", "修改", "微调", "改一下", "改改", "再改", "调整", "改图",
            "改成", "改为", "改个", "换成", "变成", "变为", "换色", "改色",
            "换个", "换一个", "换张", "换图", "替换",
            # 尺寸/结构类改动（「小一点」原本被判成新需求 → 改了也没用）
            "大一点", "小一点", "大一些", "小一些", "大点", "小点", "放大", "缩小",
            "加个", "加上", "去掉", "删掉", "简化", "细节", "加厚", "变薄",
        ]):
            return Intent.REFINE, original
        # 序号选图：只认「选第N/第N」或「阿拉伯数字+张/个/图」或整句就是数字。
        # 注意不能用「中文数字+个」——「我要一个熊大」的"一个"会被误判成选图。
        num_map = {"1": 0, "一": 0, "2": 1, "二": 1, "3": 2, "三": 2}
        m = (re.search(r"(?:选第|第)\s*([123一二三])", t)
             or re.search(r"([123])\s*[张个图]", t)
             or re.fullmatch(r"\s*([123])\s*", original))
        if m:
            return Intent.SELECT_IMAGE, str(num_map.get(m.group(1), 0))
        # 显式确认 → 进 3D
        if any(k in t for k in ["可以了", "可以", "确认", "往下走", "继续", "开始建模", "生成3d",
                                "生成建模", "开始3d", "下一步", "往下"]):
            return Intent.START_3D, ""
        # 新的需求描述（优先于"打印"等宽松词）
        if any(k in t for k in ["我要", "帮我", "做个", "做一个", "画一个", "生成一个",
                                "设计一个", "想打印", "打印一个", "想要", "来一个"]):
            return Intent.DESCRIBE_OBJECT, original
        if any(k in t for k in ["导出stl", "转stl", "导出", "出stl", "要stl"]):
            return Intent.EXPORT_STL, ""
        if any(k in t for k in ["打开bambu", "bambu studio", "打印", "开始打印"]):
            return Intent.OPEN_BAMBU, ""
        # 兜底：视为「新的需求描述」，绝不默认跑 3D。
        # 原实现此处 return START_3D —— 导致用户在选图阶段一输入提示词就被静默
        # 判成"开始建模"，直接弹出「建模已完成」（2026-09-22 宇凯报的 bug）。
        return Intent.DESCRIBE_OBJECT, original

    # 阶段 3 - 3D建模阶段
    if current_phase == "3d":
        if any(k in t for k in ["导出stl", "转stl", "导出", "出stl", "要stl", "stl"]):
            return Intent.EXPORT_STL, ""
        if any(k in t for k in ["打开bambu", "bambu", "打印", "开始打印"]):
            return Intent.OPEN_BAMBU, ""
        if any(k in t for k in ["重新建模", "重新生成", "再来", "换一个"]):
            return Intent.REGENERATE, ""
        return Intent.EXPORT_STL, ""

    # 阶段 4 - STL/打印阶段
    if current_phase == "stl":
        if any(k in t for k in ["打开bambu", "bambu", "打印", "开始打印", "发送打印"]):
            return Intent.OPEN_BAMBU, ""
        if any(k in t for k in ["调整", "改尺寸", "换尺寸", "大一点", "小一点", "矮一点", "高一点", "再调"]):
            return Intent.REFINE, original
        if any(k in t for k in ["新建", "新对话", "下一个", "新项目"]):
            return Intent.NEW_PROJECT, ""
        return Intent.UNKNOWN, original

    return Intent.UNKNOWN, original


def resolve_entity(text: str) -> str:
    """如果用户提到实体名称，返回完整描述；否则返回原文"""
    for alias, description in ENTITY_ALIASES.items():
        if alias in text:
            # 替换别名
            return text.replace(alias, description)
    return text
