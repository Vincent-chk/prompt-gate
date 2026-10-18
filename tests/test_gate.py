#!/usr/bin/env python3
"""prompt-gate 单元测试：零依赖 unittest，CI 直接跑。

覆盖：回复解析、回归判定（gate_check）、mock 后端两条真实路径（v1 全对 / v2 触发 T3 回归）、
promote 拦截与放行。不联网、不需要 Key。CLI 层直接调用 gate.main()（进程内），
用 SystemExit 断言退出码。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import gate  # noqa: E402


def run_cli(argv):
    """调用 gate.main，返回 (退出码, stdout)。0 = 正常返回。"""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            gate.main(argv)
        return 0, buf.getvalue()
    except SystemExit as exc:
        return (exc.code or 0), buf.getvalue()


class ParseTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(gate.parse_verdict('{"team": "billing", "priority": "P2"}')["team"], "billing")

    def test_fenced_and_prose(self):
        text = 'Sure!\n```json\n{"team": "auth", "priority": "P1"}\n```\nHope this helps.'
        self.assertEqual(gate.parse_verdict(text)["team"], "auth")

    def test_garbage(self):
        self.assertIsNone(gate.parse_verdict("I cannot classify this ticket."))


class GateCheckTest(unittest.TestCase):
    @staticmethod
    def score(billing, rest=1):
        return {"score": {"billing": [billing, 1], "auth": [rest, 1],
                          "api-platform": [rest, 1], "dashboard": [rest, 1]}}

    def test_regression_detected(self):
        problems = gate.gate_check(self.score(1), self.score(0))
        self.assertTrue(any("billing 回归" in p for p in problems))

    def test_improvement_passes(self):
        self.assertEqual(gate.gate_check(self.score(1), self.score(1)), [])


class MockFlowTest(unittest.TestCase):
    """复刻官方文档剧本：v1 基线 4/4，v2 的宽规则把 T3 从 billing 抢走。"""

    def test_v1_baseline_perfect(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli(["eval", "--prompt", str(ROOT / "prompts/v1.md"), "--out", str(Path(d) / "v1")])
            self.assertEqual(code, 0)
            self.assertIn("4/4", out)

    def test_v2_regression_on_T3(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v1.md"), "--out", str(d / "v1")])
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v2.md"), "--out", str(d / "v2")])
            cand = json.loads((d / "v2/score.json").read_text(encoding="utf-8"))
            self.assertEqual(cand["score"]["billing"], [0, 1])


class CLITest(unittest.TestCase):
    def test_gate_command_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v1.md"), "--out", str(d / "v1")])
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v2.md"), "--out", str(d / "v2")])
            code, out = run_cli(["gate", "--baseline", str(d / "v1/score.json"),
                                 "--candidate", str(d / "v2/score.json")])
            self.assertEqual(code, 1)
            self.assertIn("拒绝晋升", out)

    def test_promote_blocked_then_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v1.md"), "--out", str(d / "v1")])
            run_cli(["eval", "--prompt", str(ROOT / "prompts/v2.md"), "--out", str(d / "v2")])
            prod = d / "production.json"
            prod.write_text(json.dumps({"agent_id": "a", "agent_version": 1}), encoding="utf-8")
            code, _ = run_cli(["promote", "--production", str(prod),
                               "--candidate", str(d / "v2/score.json"),
                               "--baseline", str(d / "v1/score.json"), "--agent-version", "2"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(prod.read_text(encoding="utf-8"))["agent_version"], 1)
            code, out = run_cli(["promote", "--production", str(prod),
                                 "--candidate", str(d / "v1/score.json"),
                                 "--baseline", str(d / "v1/score.json"), "--agent-version", "3"])
            self.assertEqual(code, 0)
            self.assertIn("晋升通过", out)
            self.assertEqual(json.loads(prod.read_text(encoding="utf-8"))["agent_version"], 3)


if __name__ == "__main__":
    unittest.main()
