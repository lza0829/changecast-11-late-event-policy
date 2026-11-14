# 04-changecast（变投影）

Changecast — 变更日志投影：源变更流 → 按序投影 → 乱序/晚到策略钩子 → 快照可查互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）、Syncledger（双端对账）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5400 |
| API | http://localhost:10400 |

## 主链

配置投影流 → 按到达波次投递 CDC 夹具 → seq 大于主水位的走普通增量并推进水位 →
水位已推过之后的小 seq 事件按该流的迟到策略处理 → 批次计数与快照互证

## 种子投影流

- **member_profile**：会员档案变更（`member_id`，字段 `display_name` / `tier` / `region`）
- **store_archive**：门店档案变更（`store_id`，字段 `name` / `status` / `city`）
- **channel_attr**：渠道属性变更（`channel_code`，字段 `label` / `active`）

## 迟到事件策略（本切口）

主水位是「已经通过常规路径应用到的最大 seq」。到达事件的 seq 一旦不大于当前水位，
它就是**迟到**，与「缺口尚未到达的高 seq 等待」不是一回事（缺口等待未实现）。
迟到事件绝不进入普通增量批次，两种策略互斥、按流配置：

| 策略 | 行为 |
| --- | --- |
| `reject` | 拒绝纳入常规应用，`late_rejected` 计数 +1，投影与主水位不动 |
| `bypass` | 进迟到旁路批（编号 `LC-…`，与主批次 `PB-…` 分列），按每实体裁决修正投影；主水位不回拨 |

旁路终态裁决：仅当 `迟到 seq > 该实体当前 applied_seq` 才允许盖写，否则记
`late_ineffective`（无效迟到）且不改投影。投影行的 `applied_batch_id` 标明来源车道。

页面 `迟到策略` 演示：选流与策略 → 逐波投递 → 查看水位前后、两车道计数、
逐实体 applied_seq、迟到留痕，以及「主水位从不回拨 / 主批次不含迟到 /
计数分列 / 实体 seq 不超水位 / 投影与留痕互证 / 旁路盖写只在旁路批 /
编号分列」七项自动互证检查。

拒绝裁决对 `event_id` 是终态的：同一事件改策略后重投按重复跳过，不会补应用；
这正是留痕可复查的前提。乱序缓冲表空挂，缺口等待留给后题。对外文案不用「工单」「CRM」。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 测试

```bash
cd backend && pytest -q          # 引擎 + 落库 + 互证检查
cd frontend && npm test          # 页面组件挂载，连真后端跑完两条策略
```

## 未实现（见出题规划）

`ooo_window`（缺口等待/回看窗口） / `tombstone_policy` 深逻辑 / `watermark_rewind` / …
