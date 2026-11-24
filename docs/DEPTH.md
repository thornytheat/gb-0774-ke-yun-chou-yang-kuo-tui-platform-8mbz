# 客运抽样扩推台深度登记

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 抽样回收采信 | 复查重访裁决与原件行 | `/api/survey/returns` | `app/modules/returns/csv_input.py` `app/modules/returns/visits.py` | `tests/test_returns.py` |
| M2 班次与分层校验 | 查出票版次和答卷归属 | `/api/survey/frame` | `app/modules/frame/register.py` `app/modules/frame/join.py` | `tests/test_frame.py` |
| M3 非响应权重校准 | 核对基础权和校准因子 | `/api/survey/weights` | `app/modules/weights/calibration.py` | `tests/test_weights.py` |
| M4 扩推整数分配 | 查层内整数尾差 | `/api/survey/allocation` | `app/modules/allocation/remainders.py` | `tests/test_allocation.py` |
| M5 层级偏差对账 | 核算人口、回收行和百分点 | `/api/survey/balance` | `app/modules/balance/service.py` `app/modules/balance/shares.py` | `tests/test_balance.py` |
| M6 批次留档复跑 | 事务留档及同源复算 | `/api/runs` | `app/modules/ledger/storage.py` `app/modules/ledger/service.py` | `tests/test_archive.py` |
| M7 样本证据签发 | 按原件和结果摘要签发 | `/api/export/<id>` | `app/modules/release/audit.py` `app/modules/release/service.py` | `tests/test_release.py` |
| M8 补正接用与发车分层 | 签发页接用补正人数，发车分层保留原排班 | `/api/adoption` `/api/schedule/<id>` | `app/modules/adoption/plan.py` `app/modules/adoption/service.py` `app/modules/adoption/store.py` | `tests/test_adoption.py` `tests/test_adoption_workbench.py` |

## 陷阱登记

| TRAP | 现象 | 朴素处理后果 | 本项目规则 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 答卷 BOM | 无法识别首列 | 读取时去 BOM | `app/modules/returns/csv_input.py` | `tests/test_returns.py::test_bom` |
| TRAP-04 | 拼接表头 | 错把表头当答卷 | 单列分段统计 | `app/modules/returns/csv_input.py` | `tests/test_returns.py::test_header` |
| TRAP-05 | 多出分隔列 | 答案错位 | 逐行隔离列宽 | `app/modules/returns/csv_input.py` | `tests/test_returns.py::test_bad_width` |
| TRAP-07 | N/A 答案 | 隐式当某选项 | 原件留档且排除 | `app/modules/frame/join.py` | `tests/test_frame.py::test_missing_choice` |
| TRAP-08 | 前导零票号与班次 | 丢失连接键 | 全程文本标识 | `app/modules/frame/join.py` | `tests/test_frame.py::test_leading_zero` |
| TRAP-10 | 循环小数校准因子 | 三类合计失真 | 分数精确守恒 | `app/modules/weights/calibration.py` | `tests/test_weights.py::test_calibration_exact` |
| TRAP-14 | 同版问项冲突 | 后写覆盖 | 全部冲突票隔离 | `app/modules/returns/visits.py` | `tests/test_returns.py::test_conflict` |
| TRAP-19 | 新旧重访与重复同时出现 | 顺序覆盖 | 先版本再副本 | `app/modules/returns/visits.py` | `tests/test_returns.py::test_latest_then_duplicates` |
| TRAP-24 | 城北三类各 100/3 人 | 四舍五入只剩 99 人 | 最大余数，直达先补 | `app/modules/allocation/remainders.py` | `tests/test_allocation.py::test_tie_is_stable` |
| TRAP-32 | 补正签发遇已发车分层 | 未发车层拿不到新人数，或把已发车层也改掉 | 未发车且无牵连的层照常接用；已发车层保留原排班人数 | `app/modules/adoption/plan.py` | `tests/test_adoption.py::test_plan_keeps_departed_and_adopts_rest` |
| TRAP-33 | 出票改层跨过已发车分层 | 只换一边，改层两端人数多算或漏算 | 一端落在已发车层的改层整笔留待人工，牵累冻结相连未发车层 | `app/modules/adoption/plan.py` | `tests/test_adoption.py::test_plan_keeps_departed_and_adopts_rest` |
| TRAP-34 | 接用中途失败后重试 | 已生效的一端残留、另一端回旧，或重试再加一遍 | 改层两端与两层人数同组一个事务；已接用组记账，重试跳过 | `app/modules/adoption/store.py` `app/modules/adoption/service.py` | `tests/test_adoption.py::test_failed_group_rolls_back_both_sides_then_retry` |

脏样例出票 290 = 250 扩推 + 40 不可推断。原始答卷 18 = 6 回收隔离 + 4 问项隔离 + 2 未回应 + 6 有效。枢纽 150 人按基础权 4、3、2 校准为 200/3、50、100/3。城北按三份等权回收分得 34、33、33。旧分层及隔离证据都可由签发包还原。

## 补正接用与发车分层

补正签发是同一批扩推重新核算后的已签发结果，原排班是上一版基准签发。签发页（`/survey/release`）选定补正签发、勾选已发车分层后先出接用方案（`GET /api/adoption`，只读），再办理接用（`POST /api/adoption`）。规则见数据字典：已发车分层保留原排班人数；未发车且与已发车分层无改层牵连的分层照常接用补正人数；一端落在已发车分层的出票改层整笔留待人工，牵累沿改层关系冻结相连分层；两端都未发车的改层与两层人数同组，在一个事务内一起生效。接用中途失败时失败组两端都保留旧排班人数，重试时按基准与补正重新排队，已接用的组由 `adoption_log` 挡住、不重复生效。
