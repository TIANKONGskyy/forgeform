## 这个 PR 做了什么

<!-- 一句话。比如：修了自动建模重复触发，导致同一组建两次模 -->

## 为什么

<!-- 关联 issue 或说明场景。「我觉得这样更好」不算理由 -->

Closes #

## 你怎么验证的

- [ ] `python -m pytest` 全绿
- [ ] `python tools/verify.py` 全绿
- [ ] 真机跑过一次（涉及生成/UI 的改动必填）

<!-- 涉及生成的改动，贴耗时和显存数字；涉及 UI 的，贴改动前后截图 -->

## 检查清单

- [ ] 没有提交 `config.json`、模型权重、`.glb`/`.stl`、`outputs/`
- [ ] 新增的文件路径都走 `backend/env.py`，没有写死本机绝对路径
- [ ] 改了 UI 的按钮 / `ui_outputs` / `_update_ui` 返回值 —— 四处的元数都同步了
- [ ] 新增状态字段 —— `save_project` 和 `load_project` 的白名单都加了
- [ ] 新增 phase —— `_buttons_for_phase` 补了分支
- [ ] 修改了提示词层 —— `_numbers_preserved` 这道闸没被绕过去
