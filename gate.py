#!/usr/bin/env python3
"""prompt-gate：把 Managed Agents 的提示词版本机制变成一行命令的晋升闸门。

背景（灵感来自阿里云百炼最佳实践《进阶：提示词版本管理与回滚》，并把同构机制
落到火山方舟 Managed Agents 上）：托管平台把 system prompt 变成服务端可版本化
的资源——每次 POST 更新产生一个不可变的新版本，Session 可锁定任意历史版本，
回滚 = 再发布一个与旧版本等价的新版本，全程不需要部署代码。

本工具在其上补三件事，凑成一个可复用的晋升闸门：
  1. eval   —— 用标注测试集给某个提示词版本打分（三后端：mock / chat / agents）
  2. gate   —— 候选版本对比基线版本，任何一个团队命中数下降即拒绝晋升（退出码 1）
  3. promote—— 通过闸门才改写 production.json（「生产用哪个版本」这份受管控产物）

后端说明
  mock     零 Key 演示后端：一个复刻官方文档记录行为的分类模拟器（不是真模型）
  chat     任意 OpenAI 兼容接口（默认火山方舟 /api/v3，换 doubao-seed-* 模型 ID 即用）
  agents   托管平台原生版本 API：ark（火山方舟 Managed Agents）或 bailian（百炼）
           评测历史版本无需回滚——创建 Session 时传对象形式锁定指定版本即可

凭据只从环境变量读取，不写进代码与产物：
  GATE_API_KEY      方舟 / 百炼 API Key（chat 与 agents 后端用）
  GATE_BASE_URL     默认 https://ark.cn-beijing.volces.com/api/v3
  GATE_MODEL        chat 后端模型 ID，默认 doubao-seed-2-1-pro-260915
  GATE_AGENT_ID     agents 后端的 Agent ID
  GATE_PLATFORM     ark（默认）或 bailian

典型流程（对应官方文档六步，全部在本地留痕）：
  python3 gate.py eval     --backend mock --prompt prompts/v1.md --out runs/v1
  python3 gate.py eval     --backend mock --prompt prompts/v2.md --out runs/v2
  python3 gate.py gate     --baseline runs/v1/score.json --candidate runs/v2/score.json
  python3 gate.py promote  --production production.json --agent-version 2   # v2 会被拦下
  python3 gate.py rollback --to 1                                            # 发等价 v1 的新版本

本文件与 README 如实区分：mock 通道的输出是模拟器行为；真实 Seed / 真实托管
版本 API 的全链路需要在有 Key 的环境里跑同一组命令（脚本已就绪，未在本仓库
CI 内验证平台侧字段——以两家官方文档为准）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"
DEFAULT_FIXTURES = HERE / "fixtures" / "tickets.json"
TEAMS = ["api-platform", "auth", "billing", "dashboard"]
REQUEST_TIMEOUT_SECONDS = 120


# ---------- 基础工具 ----------

def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_base_url(url: str) -> str:
    """只放行 http/https，且目标主机不能是本机、环回、私有或保留地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SystemExit(f"接口地址只支持 http/https：{url}")
    host = parsed.hostname
    if not host:
        raise SystemExit(f"接口地址缺少主机名：{url}")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise SystemExit(f"拒绝请求本机地址：{host}")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise SystemExit(f"无法解析接口主机 {host}：{exc}")
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    for address in addresses:
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise SystemExit(f"拒绝请求私有/保留地址：{host} -> {address}")
    return url.rstrip("/")


def http_json(method: str, url: str, api_key: str, payload=None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def parse_verdict(text: str) -> dict | None:
    """从模型回复里抠出 {"team":...,"priority":...}。容忍代码围栏与多余解释。"""
    cleaned = re.sub(r"```(?:json)?", "", text)
    for m in re.finditer(r"\{[^{}]*\}", cleaned):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("team") in TEAMS:
            return {"team": obj["team"], "priority": obj.get("priority", "?")}
    return None


# ---------- 三后端：每条工单返回一行模型输出 ----------

def mock_answer(system: str, ticket: dict) -> str:
    """复刻官方文档记录的分类行为的模拟器（关键词规则，不是真模型）。"""
    text = (ticket["subject"] + " " + ticket["body"]).lower()
    rule = re.search(r"ROUTING RULE|路由规则", system)
    if rule and re.search(r"api usage|usage charges|rate limit|quota|request volume"
                          r"|用量|限流|配额|请求量", text):
        return '{"team": "api-platform", "priority": "P2"}'
    if "429" in text or "too many requests" in text:
        return '{"team": "api-platform", "priority": "P1"}'
    if "oauth" in text or "token" in text or "refresh" in text:
        return '{"team": "auth", "priority": "P2"}'
    if "chart" in text or "blank" in text or "renders as an empty" in text:
        return '{"team": "dashboard", "priority": "P3"}'
    return '{"team": "billing", "priority": "P2"}'


def chat_answer(base_url: str, api_key: str, model: str, system: str, ticket: dict) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Subject: {ticket['subject']}\n\n{ticket['body']}"},
        ],
        "temperature": 0,
    }
    resp = http_json("POST", f"{base_url}/chat/completions", api_key, payload)
    return resp["choices"][0]["message"]["content"]


def agents_triage(base_url: str, api_key: str, platform: str, agent_id: str,
                  agent_version: int | None, ticket: dict) -> str:
    """托管平台原生通道：建 Session（字符串锁最新 / 对象锁指定版本）→ 发工单 → 轮询 → 删除。"""
    if platform == "ark":
        agent_ref = {"type": "agent", "id": agent_id, "version": agent_version} if agent_version \
            else agent_id
        sess = http_json("POST", f"{base_url}/sessions", api_key,
                         {"agent": agent_ref, "title": "prompt-gate-eval"})
        session_id = sess["id"]
        http_json("POST", f"{base_url}/sessions/{session_id}/events", api_key, {
            "events": [{"type": "message", "role": "user",
                        "content": [{"type": "text",
                                     "text": f"Subject: {ticket['subject']}\n\n{ticket['body']}"}]}]})
    else:  # bailian
        agent_ref = {"id": agent_id, "version": agent_version} if agent_version else agent_id
        sess = http_json("POST", f"{base_url}/sessions", api_key,
                         {"agent": agent_ref, "title": "prompt-gate-eval"})
        session_id = sess["id"]
        http_json("POST", f"{base_url}/sessions/{session_id}/events", api_key, {
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "text",
                                    "text": f"Subject: {ticket['subject']}\n\n{ticket['body']}"}]}]})
    deadline = dt.datetime.now() + dt.timedelta(seconds=60)
    while dt.datetime.now() < deadline:
        page = http_json("GET",
                         f"{base_url}/sessions/{session_id}/events?order=desc&limit=1", api_key)
        last = (page.get("data") or [{}])[0]
        status_blob = json.dumps(last)
        if '"idle"' in status_blob or '"end_turn"' in status_blob:
            break
        import time
        time.sleep(2)
    page = http_json("GET", f"{base_url}/sessions/{session_id}/events?order=desc&limit=50", api_key)
    texts = []
    for ev in reversed(page.get("data") or []):
        if ev.get("type") == "message" and ev.get("role") == "assistant":
            for block in ev.get("content") or []:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
        if texts:
            break
    http_json("DELETE", f"{base_url}/sessions/{session_id}", api_key)
    return "\n".join(texts)


# ---------- 打分与闸门 ----------

def evaluate(backend: str, args) -> tuple[list, dict]:
    fixtures = load_json(Path(args.fixtures))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    system = Path(args.prompt).read_text(encoding="utf-8") if args.prompt else None
    results = []
    transcript = []
    for t in fixtures:
        if backend == "mock":
            raw = mock_answer(system, t)
        elif backend == "chat":
            raw = chat_answer(validate_base_url(args.base_url), args.api_key, args.model, system, t)
        elif backend == "from-response":
            raw = (Path(args.responses) / f"{t['id']}.txt").read_text(encoding="utf-8")
        elif backend == "agents":
            raw = agents_triage(validate_base_url(args.base_url), args.api_key, args.platform,
                                 args.agent_id, args.agent_version, t)
        else:
            raise SystemExit(f"未知后端：{backend}")
        verdict = parse_verdict(raw)
        got = verdict["team"] if verdict else None
        results.append({"id": t["id"], "expected": t["expected"], "got": got,
                        "priority": (verdict or {}).get("priority")})
        transcript.append(f"[{t['id']}] expected={t['expected']} got={got}\n{raw.strip()}\n")
    score = {team: [sum(1 for r in results if r["expected"] == team and r["got"] == team),
                    sum(1 for r in results if r["expected"] == team)] for team in TEAMS}
    total_hits = sum(v[0] for v in score.values())
    summary = {"backend": backend, "prompt": args.prompt, "agent_version": args.agent_version,
               "score": score, "total": f"{total_hits}/{len(results)}",
               "ran_at": dt.datetime.now().isoformat(timespec="seconds")}
    (out / "score.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    (out / "detail.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    (out / "transcript.txt").write_text("\n".join(transcript), "utf-8")
    return results, summary


def print_score(summary: dict, label: str):
    print(f"── {label} 成绩（{summary['backend']}，{summary['total']} 命中）")
    for team, (hit, tot) in summary["score"].items():
        if tot:
            mark = "OK " if hit == tot else "MISS"
            print(f"   {team:14s} {hit}/{tot}  [{mark}]")


def gate_check(baseline: dict, candidate: dict) -> list[str]:
    """回归判定：任何一个团队命中数低于基线即回归；整体也不许退。返回回归描述列表。"""
    problems = []
    for team, (chit, _) in candidate["score"].items():
        bhit = baseline["score"].get(team, [0, 0])[0]
        if chit < bhit:
            problems.append(f"{team} 回归：{bhit} → {chit}")
    return problems


# ---------- 托管平台版本操作（ark / bailian 两家同构） ----------

def agents_endpoint(args, path: str) -> str:
    return f"{validate_base_url(args.base_url)}{path}"


def cmd_publish(args):
    """POST 更新 = 发布新版本（不可变，版本号自增）。"""
    system = Path(args.prompt).read_text(encoding="utf-8")
    api_key = args.api_key or os.environ.get("GATE_API_KEY", "")
    agent_id = args.agent_id or os.environ["GATE_AGENT_ID"]
    base = agents_endpoint(args, "")
    current = http_json("GET", f"{base}/agents/{agent_id}", api_key)
    body = {"name": current["name"], "version": current["version"], "system": system}
    resp = http_json("POST", f"{base}/agents/{agent_id}", api_key, body)
    print(f"已发布新版本：v{resp['version']}（基于 v{current['version']}）")


def cmd_versions(args):
    api_key = args.api_key or os.environ.get("GATE_API_KEY", "")
    agent_id = args.agent_id or os.environ["GATE_AGENT_ID"]
    base = agents_endpoint(args, "")
    page = http_json("GET", f"{base}/agents/{agent_id}/versions", api_key)
    print(f"{'版本':>4}  {'创建时间':25} system 首行")
    for item in page.get("data", []):
        first_line = (item["config"]["system"] or "").splitlines()[0][:40]
        print(f"v{item['version']:>3}  {item['created_at']:25} {first_line}")


def cmd_rollback(args):
    """回滚 = 取 vN 的 system 原文，再发布一个等价的新版本；默认流量（锁最新）立刻回到 vN 行为。"""
    api_key = args.api_key or os.environ.get("GATE_API_KEY", "")
    agent_id = args.agent_id or os.environ["GATE_AGENT_ID"]
    base = agents_endpoint(args, "")
    page = http_json("GET", f"{base}/agents/{agent_id}/versions", api_key)
    target = next((v for v in page.get("data", []) if v["version"] == args.to), None)
    if target is None:
        raise SystemExit(f"找不到版本 v{args.to}")
    current = http_json("GET", f"{base}/agents/{agent_id}", api_key)
    body = {"name": current["name"], "version": current["version"],
            "system": target["config"]["system"]}
    resp = http_json("POST", f"{base}/agents/{agent_id}", api_key, body)
    print(f"回滚完成：v{resp['version']} 与 v{args.to} 等价，新建 Session 即刻生效，无需部署。")


def cmd_promote(args):
    """晋升闸门：production.json 是唯一受管控产物。校验候选成绩后，才改版本号。"""
    production = load_json(Path(args.production))
    candidate = load_json(Path(args.candidate))
    baseline_path = Path(args.baseline) if args.baseline else None
    if baseline_path:
        problems = gate_check(load_json(baseline_path), candidate)
        if problems:
            print("拒绝晋升：")
            for p in problems:
                print(f"  ✗ {p}")
            sys.exit(1)
    production["agent_version"] = args.agent_version
    production["promoted_at"] = dt.datetime.now().isoformat(timespec="seconds")
    Path(args.production).write_text(json.dumps(production, ensure_ascii=False, indent=2), "utf-8")
    print(f"晋升通过：production 锁定 v{args.agent_version}（该文件的变更走正常代码评审）。")


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, needs_prompt=True):
        sp.add_argument("--backend", default="mock",
                        choices=["mock", "chat", "agents", "from-response"])
        sp.add_argument("--prompt", default=None)
        sp.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
        sp.add_argument("--base-url", default=os.environ.get("GATE_BASE_URL", DEFAULT_BASE_URL))
        sp.add_argument("--api-key", default=os.environ.get("GATE_API_KEY", ""))
        sp.add_argument("--model", default=os.environ.get("GATE_MODEL", DEFAULT_MODEL))
        sp.add_argument("--platform", default=os.environ.get("GATE_PLATFORM", "ark"),
                        choices=["ark", "bailian"])
        sp.add_argument("--agent-id", default=os.environ.get("GATE_AGENT_ID"))
        sp.add_argument("--agent-version", type=int, default=None,
                        help="agents 后端锁定评测的历史版本号；不传 = 锁最新")
        sp.add_argument("--responses", default=None, help="from-response 后端：回复文件目录")
        if needs_prompt:
            pass

    sp_eval = sub.add_parser("eval", help="给提示词版本打分")
    common(sp_eval)
    sp_eval.add_argument("--out", required=True)

    sp_gate = sub.add_parser("gate", help="候选 vs 基线，回归即拒绝（退出码 1）")
    sp_gate.add_argument("--baseline", required=True)
    sp_gate.add_argument("--candidate", required=True)

    sp_pro = sub.add_parser("promote", help="通过闸门才改写 production.json 的版本号")
    sp_pro.add_argument("--production", required=True)
    sp_pro.add_argument("--candidate", required=True)
    sp_pro.add_argument("--baseline", default=None)
    sp_pro.add_argument("--agent-version", type=int, required=True)

    sp_pub = sub.add_parser("publish", help="（agents 后端）发布新版本")
    common(sp_pub)

    sp_ver = sub.add_parser("versions", help="（agents 后端）列出全部历史版本")
    common(sp_ver)

    sp_rb = sub.add_parser("rollback", help="（agents 后端）发布与 vN 等价的新版本")
    common(sp_rb)
    sp_rb.add_argument("--to", type=int, required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "eval":
        if args.backend in ("chat", "from-response", "mock") and not args.prompt:
            raise SystemExit(f"{args.backend} 后端需要 --prompt")
        _, summary = evaluate(args.backend, args)
        label = args.prompt or f"agent v{args.agent_version or '最新'}"
        print_score(summary, label)
        print(f"成绩写入 {args.out}/score.json")
    elif args.cmd == "gate":
        baseline, candidate = load_json(Path(args.baseline)), load_json(Path(args.candidate))
        print_score(baseline, "基线 " + str(Path(args.baseline).parent))
        print_score(candidate, "候选 " + str(Path(args.candidate).parent))
        problems = gate_check(baseline, candidate)
        if problems:
            print("闸门结论：✗ 拒绝晋升")
            for prob in problems:
                print(f"  ✗ {prob}")
            sys.exit(1)
        print("闸门结论：✓ 无回归，可晋升")
    elif args.cmd == "promote":
        cmd_promote(args)
    elif args.cmd == "publish":
        cmd_publish(args)
    elif args.cmd == "versions":
        cmd_versions(args)
    elif args.cmd == "rollback":
        cmd_rollback(args)


if __name__ == "__main__":
    main()
