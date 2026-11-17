import copy
import importlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend import project_manager as pm


def test_pending_message_is_visible_without_being_persisted():
    with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'PROJECTS_DIR', Path(temp)), patch('threading.Thread.start'):
        app = importlib.import_module('app')
        state = {'conversation': [{'role': 'user', 'content': '旧消息', 'ai_reply': '旧回复'}], 'phase': 'empty'}
        original = copy.deepcopy(state)
        with patch.object(app, 'refresh_history', return_value={}):
            view = app._update_ui_with_note(state, '正在处理', pending_input='新消息')
        assert view[0][-1] == ['新消息', '正在处理']
        assert state == original
        assert view[1]['visible'] is False


def test_submit_renders_user_message_before_model_warmup():
    with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'PROJECTS_DIR', Path(temp)), patch('threading.Thread.start'):
        app = importlib.import_module('app')
        state = {'conversation': [], 'phase': 'empty'}
        with patch.object(app, 'refresh_history', return_value={}), patch.object(app, 'agent_enabled') as enabled:
            stream = app.on_submit('制作笔筒', state)
            first = next(stream)
            enabled.assert_not_called()
            assert first[2][-1] == ['制作笔筒', '正在处理…']
            assert state['conversation'] == []
            stream.close()


def test_agent_exception_does_not_replay_through_fallback():
    with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'PROJECTS_DIR', Path(temp)), patch('threading.Thread.start'):
        app = importlib.import_module('app')
        state = {'conversation': [], 'phase': 'empty'}
        def broken(_):
            raise RuntimeError('action failed')
            yield
        with patch.object(app, 'refresh_history', return_value={}), patch.object(app, 'agent_enabled', return_value=True), \
             patch.object(app.agent, 'is_ready', return_value=True), \
             patch.object(app.conv, 'current_project', state), \
             patch.object(app.conv, 'handle_agent_turn_stream', side_effect=broken), \
             patch.object(app.ic, 'classify') as fallback:
            rows = list(app.on_submit('生成笔筒', state))
        fallback.assert_not_called()
        assert '检查项目状态' in rows[-1][2][-1][1]
        assert rows[-1][2][-1][0] == '生成笔筒'


def test_warmup_failure_restores_unsent_draft():
    with tempfile.TemporaryDirectory() as temp, patch.object(pm, 'PROJECTS_DIR', Path(temp)), patch('threading.Thread.start'):
        app = importlib.import_module('app')
        state = {'conversation': [], 'phase': 'empty'}
        with patch.object(app, 'refresh_history', return_value={}), patch.object(app, 'agent_enabled', return_value=True), \
             patch.object(app.agent, 'is_ready', return_value=False), patch.object(app.agent, 'warm_up', side_effect=RuntimeError('offline')):
            rows = list(app.on_submit('笔筒', state))
        assert rows[-1][0] == '笔筒'
        assert '加载失败' in rows[-1][2][-1][1]
