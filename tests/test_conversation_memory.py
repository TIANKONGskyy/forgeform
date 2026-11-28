# -*- coding: utf-8 -*-
"""对话侧回归：同组记忆 / 换东西开新组 / 切回续聊（2026-11-28 实测）。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def conv(tmp_path, monkeypatch):
    from backend import project_manager as pm
    from backend.conversation import ConversationManager

    monkeypatch.setattr(pm, "PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    cm = ConversationManager()
    cm.current_project = cm.new_project()
    return cm


def test_same_group_keeps_requirement(conv):
    conv.handle_user_input("做一个直径 90mm 的六边形笔筒")
    folder = conv.current_project["folder"]
    prompt = conv.current_project.get("base_prompt") or ""
    assert "笔筒" in prompt
    assert conv.current_project["folder"] == folder
    # 补充要求留在本组，不换组
    conv.handle_user_input("底座再加厚一点")
    assert conv.current_project["folder"] == folder
    assert "笔筒" in (conv.current_project.get("base_prompt") or "")


def test_different_object_opens_new_group(conv):
    conv.handle_user_input("做一个直径 90mm 的六边形笔筒")
    first = conv.current_project["folder"]
    conv.handle_user_input("做个白色陶瓷马克杯")
    second = conv.current_project["folder"]
    assert second != first, "换了东西必须开新组，否则需求会被叠在一起"
    assert "马克杯" in (conv.current_project.get("base_prompt") or "")


def test_switch_back_keeps_memory(conv):
    cm = conv
    cm.handle_user_input("做一个直径 90mm 的六边形笔筒")
    first_state = cm.current_project
    first_folder = first_state["folder"]

    cm.handle_user_input("做个白色陶瓷马克杯")
    assert cm.current_project["folder"] != first_folder

    # 切回第一组：需求与对话都在
    cm.current_project = first_state
    assert "笔筒" in (cm.current_project.get("base_prompt") or "")
    assert cm.current_project["folder"] == first_folder


def test_reply_always_returned(conv):
    """无论走 agent 还是关键词路径，都必须给用户一句话。"""
    r = conv.handle_user_input("做个笔筒")
    assert isinstance(r.get("reply"), str) and r["reply"].strip()
