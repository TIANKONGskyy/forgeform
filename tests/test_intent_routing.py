# -*- coding: utf-8 -*-
"""意图分类的回归用例（2026-11-28 实测修的两个真 bug）。

背景（真机跑出来的）：
1. 阶段名 `idle` / `editing` 没被识别 → 所有输入落 UNKNOWN；
2. `3d` 阶段对所有不认识的话都返回 EXPORT_STL →
   「把把手加粗」= 导出 STL、「换个颜色」= 导出 STL，用户怎么改都改不动。
"""
from __future__ import annotations

import pytest

from backend.intent_classifier import Intent, classify


@pytest.mark.parametrize("phase", ["idle", "empty"])
def test_new_request_from_idle(phase):
    it, arg = classify("做个手机支架", phase)
    assert it is Intent.DESCRIBE_OBJECT
    assert "手机支架" in arg


@pytest.mark.parametrize("text", [
    "把把手加粗一点",
    "换个颜色，蓝色",
    "加个圆柱底座",
    "尺寸改成 120mm",
    "去掉底部的字",
    "壁厚加到 3mm",
])
def test_3d_phase_modify_is_refine_not_export(text):
    it, _ = classify(text, "3d")
    assert it is Intent.REFINE, f"{text} 应判为修改，实际 {it}"


@pytest.mark.parametrize("text", ["重来", "重做", "再来一次"])
def test_3d_phase_retry_is_regenerate(text):
    assert classify(text, "3d")[0] is Intent.REGENERATE


@pytest.mark.parametrize("text", ["再做一个笔筒", "做个新的杯垫"])
def test_3d_phase_new_object(text):
    assert classify(text, "3d")[0] is Intent.DESCRIBE_OBJECT


@pytest.mark.parametrize("text", ["这个打印要多久", "多少钱", "能做吗？"])
def test_questions_are_not_actions(text):
    assert classify(text, "3d")[0] is Intent.UNKNOWN


def test_3d_phase_export_still_works():
    assert classify("导出 STL", "3d")[0] is Intent.EXPORT_STL
    assert classify("可以了", "3d")[0] is Intent.EXPORT_STL


def test_editing_phase_normalizes_to_selecting():
    """editing 是代码里真实存在的阶段，必须按 selecting 处理。"""
    it, _ = classify("第二张", "editing")
    assert it is Intent.SELECT_IMAGE
