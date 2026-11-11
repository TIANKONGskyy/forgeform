"""验证本轮对铸想的改动 —— 放在 tests/ 下，与仓库既有用例同风格（unittest）。

★ 为什么断言前要**先剔除 docstring/注释**：
   本次我把"旧的错误清理条件"原文引在 docstring 里当说明，
   于是 `assertNotIn("main.py.*(8188|8189)", src)` 会命中那句**注释**而误报失败。
   这个坑我的 skill 里写着（"先剔除注释再做文本断言"），
   结果自己又踩了一次 —— 所以这里显式用 AST 取"可执行代码"再断言。
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _executable_source(path: Path, fn_name: str | None = None) -> str:
    """取文件（或某个函数）的**可执行**代码：docstring 与注释都不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if fn_name is None:
        return ast.unparse(tree)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    return "\n".join(ast.unparse(n) for n in body)


class TestResourcePlan(unittest.TestCase):
    """铸想启动前的资源检查（backend/startup.py）。

    ★★ 2026-10-16 修：这两个用例原本**直接调真实 CLI**，于是结果取决于
       机器当时的忙闲 —— ComfyUI 一起来就必然返回 WAIT，测试就红 ✗
       这不是产品 bug，是**测试设计错了**：`_resource_plan` 是个
       `subprocess.run` 的包装，它的价值在于**解析 CLI 输出**，
       而不是"此刻机器忙不忙"。

       所以改成 **stub 掉 subprocess.run**，喂各种真实出现过的输出格式，
       断言解析结果。这样测试变得确定、可重复、且真正覆盖了易错点
       （判据词里的连字符 RELEASE-FIRST、reason 被截断、退出码兜底）。
    """

    def setUp(self):
        import backend.startup as SU
        self.SU = SU
        # 让 exists() 检查通过（真实路径确实都在，但显式说明意图）
        self._real_run = SU.subprocess.run

    def tearDown(self):
        self.SU.subprocess.run = self._real_run

    def _stub(self, stdout: str, returncode: int):
        """把 CLI 调用替换成固定输出。"""
        class _R:
            pass
        r = _R()
        r.stdout, r.stderr, r.returncode = stdout, "", returncode
        self.SU.subprocess.run = lambda *a, **k: r

    def test_parses_ok_from_cli_output(self):
        """CLI 说 OK → 解析出 action=ok，reason 是完整句子（不被截成一个字）"""
        self._stub("申请：显存 0.5GB  内存 0.0GB\n判定：OK — 资源充足，可以启动\n", 0)
        r = self.SU._resource_plan(512, 0)
        self.assertIsNotNone(r)
        self.assertEqual(r["action"], "ok")
        # 曾经因为 fallback 分支类型不一致，匹配成功时取了字符串的 [0]，
        # reason 只剩 "资" ✗
        self.assertGreater(len(r["reason"]), 4, f"reason 被截断: {r['reason']!r}")

    def test_release_first_hyphen_is_matched(self):
        """★ 判据词含连字符（RELEASE-FIRST）—— 字符类漏了 '-' 就会走错 fallback

        历史 bug：正则写的是 [\\w]+（不含连字符），于是 RELEASE-FIRST
        整条匹配失败、悄悄走 fallback，把"显存不足"报成"内存…"，方向完全错。
        """
        self._stub("申请：显存 976.6GB  内存 0.0GB\n判定：RELEASE-FIRST — 显存要 976.6GB，超过总量，先释放\n", 2)
        r = self.SU._resource_plan(999999, 0)
        self.assertEqual(r["action"], "release-first")
        self.assertIn("显存", r["reason"], "原因该指向显存，不能张冠李戴说内存")

    def test_exit_code_mapping_exists(self):
        """没有这张表时 exit=2 会 fallthrough 成 wait，建议方向就错了"""
        self.assertEqual(self.SU._EXIT_TO_ACTION[0], "ok")
        self.assertEqual(self.SU._EXIT_TO_ACTION[1], "wait")
        self.assertEqual(self.SU._EXIT_TO_ACTION[2], "release-first")

    def test_degrades_safely_when_coordinator_missing(self):
        """协调器坏掉必须降级为 None —— 绝不能因此阻止用户启动铸想"""
        real = self.SU.RESOURCE_GUARD
        self.SU.RESOURCE_GUARD = Path(r"D:\nonexistent\guard.mjs")
        try:
            self.assertIsNone(self.SU._resource_plan(20000))
        finally:
            self.SU.RESOURCE_GUARD = real


class TestComfyUiCleanupSafety(unittest.TestCase):
    """★ 曾经的严重 bug：清理条件会误杀**外部**起的 ComfyUI。"""

    def test_no_blanket_kill_by_commandline(self):
        code = _executable_source(ROOT / "backend" / "startup.py", "_start_comfyui")
        # 旧写法 CommandLine -match 'main.py.*(8188|8189)' 会杀掉任何监听该端口的实例，
        # 包括手动起的 / 别的项目起的 —— 只能清自己记下 PID 的那个
        self.assertNotIn("main.py.*(8188|8189)", code, "又出现了全杀条件")
        self.assertNotIn("CommandLine -match", code)
        self.assertIn("comfyui-8188.pid", code, "改为只清自己的 pid 文件")

    def test_writes_own_pid_after_spawn(self):
        code = _executable_source(ROOT / "backend" / "startup.py", "_start_comfyui")
        self.assertIn("comfyui-8188.pid", code)
        self.assertIn("write_text", code, "起了新实例必须记下 PID")

    def test_label_no_longer_mentions_8189(self):
        """8189 早在 2026-09 拆卡时就撤了，日志里还写会误导排查"""
        import backend.startup as SU
        c = SU.StartupChecker()
        c.results = []
        c._comfy_online = lambda: True
        with __import__("contextlib").redirect_stdout(__import__("io").StringIO()):
            c._check_comfyui()
        msg = c.results[0].message
        self.assertNotIn("8189", msg, f"文案仍未更新: {msg}")


class TestIdleWatchdogAgentRelease(unittest.TestCase):
    """空闲看门狗：跑完释放时**连对话模型一起卸**（它 10.4GB 比 ComfyUI 的更死）。"""

    def setUp(self):
        import backend.idle_watchdog as IW
        self.IW = IW

    def test_agent_idle_probe_exists(self):
        self.assertTrue(hasattr(self.IW, "_agent_idle"))
        # 探不到服务时必须返回 False（不要因为探不到就去卸）
        self.assertIsInstance(self.IW._agent_idle(timeout=1.0), bool)

    def test_loop_accepts_agent_flag(self):
        self.assertIn("also_unload_agent", self.IW._loop.__code__.co_varnames)

    def test_config_can_disable_agent_unload(self):
        src = (ROOT / "backend" / "idle_watchdog.py").read_text(encoding="utf-8")
        self.assertIn("unload_agent", src)

    def test_queue_busy_distinguishes_unreachable_from_empty(self):
        """None（探不到）必须与 False（空队列）区分开 ——
        否则服务重启期间会被当成闲置，白白触发一次 /free"""
        self.assertIsNone(self.IW._queue_busy(9999, timeout=1.0))
        self.assertIn(self.IW._queue_busy(8188, timeout=2.0), (True, False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
