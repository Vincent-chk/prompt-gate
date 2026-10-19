# prompt-gate · 提示词晋升闸门

把「托管智能体平台的提示词版本机制」变成一行命令的晋升闸门：**改提示词不再靠部署，
但也不能随口就改**——先锁定评测，再和基线对比，出现回归就拒绝晋升。

机制出处：火山方舟 Managed Agents 官方教程《[基础] 工单分诊 Agent 系统提示词版本管理》
（docs.volcengine.com/docs/82379/2598399）——创建即 v1、每次 POST 更新产生不可变新版本、
Session 可锁定任意历史版本、回滚 = 再发布一个与旧版本等价的新版本，全程零部署。
`gate.py` 的 `agents` 后端直接对接这套原生 API。

## 它解决什么

workflow / multi-agent / 重复任务式 agent 做到一定规模，提示词就是生产资产：
vibe 改一句话 → 线上行为悄悄漂移 → 出事想退回昨天的版本，发现没存。
托管平台（方舟 / Anthropic Managed Agents）已经补上了地基：
**每次更新产生不可变版本、Session 可锁定任意历史版本、回滚 = 发布一个等价旧配置的新版本。**
本工具在其上补闸门与留痕，一行命令封住晋升：

```
gate.py eval      锁定某个提示词版本，在标注测试集上打分（产出 score.json）
gate.py gate      候选 vs 基线：任何一个团队命中数下降 = 回归，退出码 1
gate.py promote   通过闸门才改写 production.json（生产用哪个版本的唯一受管控产物）
gate.py publish   在托管平台发布新版本（版本号自增，旧版本不可变）
gate.py versions  列出全部历史版本
gate.py rollback  发布一个与 vN 等价的新版本，默认流量立刻回到 vN 行为
```

## 快速上手（零 Key，30 秒）

```bash
python3 gate.py eval --prompt prompts/v1.md --out runs/v1     # 基线 4/4
python3 gate.py eval --prompt prompts/v2.md --out runs/v2     # v2 的宽规则把 T3 抢走
python3 gate.py gate --baseline runs/v1/score.json --candidate runs/v2/score.json
# → ✗ 拒绝晋升 · billing 回归：1 → 0（退出码 1）
```

`--backend mock` 是复刻官方文档记录行为的模拟器（不是真模型）；
换 `--backend chat` + 方舟 API Key 即接真实 Seed（默认 `doubao-seed-2-1-pro-260915`）；
换 `--backend agents` 走托管平台原生版本 API（评测历史版本用 `--agent-version N` 锁定，
无需先回滚——这正是官方机制最反直觉的好用之处）。

## 测试

```bash
python3 -m unittest discover -s tests -v    # 9 项，不联网、不需要 Key
```

## 凭据

只从环境变量读取，不进代码、不进产物：`GATE_API_KEY`（也接受 `ARK_API_KEY`）、
`GATE_BASE_URL`、`GATE_MODEL`、`GATE_AGENT_ID`。
出网目标仅允许 http/https，且拒绝本机 / 环回 / 私有 / 保留地址。

## 如实声明

- 本工具代码与评测剧本由 agent 编程环境辅助开发完成（开发主力非豆包 Seed）；
  默认对接的推理模型为火山方舟 `doubao-seed-2-1-pro-260915`，chat 后端兼容任意
  OpenAI 协议接口，agents 后端按方舟官方 API 文档实现。
- 仓库内 CI 用 `mock` 通道跑通全链路（晋升闸门逻辑、成绩格式、退出码契约均真实验证）。
- `chat` / `agents` 后端**本仓库 CI 未在平台侧实跑**（无 Key 环境）；
  动真格前请先用一条工单冒烟，字段以官方文档为准。
- 演示成绩（runs/ 下的 score.json）全部出自 `mock` 模拟器，不代表真实模型分布。

## 许可

MIT。测试集 `fixtures/tickets.json` 与两条演示提示词（prompts/v1.md、v2.md）
为自创剧本，机制与演示场景参照上述方舟官方教程。
