# 深度登记 分表需量分账盘

- 规模：中型
- 核心规则：先校时并检验所有分表的共同 15 分钟窗口，再取园区总表最高完整窗口并按该窗归因。
- 使用者：园区计量与账务人员复核总表计费及各租户的峰时贡献。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 网关取样 | 校正网关时钟并为每条原始读数分流 | `GET /api/sampling/{sample}` | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py` |
| M2 分表映射 | 按窗口裁决生效的账户版本 | `GET /api/mapping/{sample}` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 需量齐时 | 确认公共窗口的三个连续格点 | `GET /api/alignment/{sample}` | `app/modules/m3_alignment/rules.py` | `tests/test_alignment.py` |
| M4 园区找峰 | 取完整公共窗口的园区主表峰值 | `GET /api/peak/{sample}` | `app/modules/m4_peak/rules.py` | `tests/test_peak.py` |
| M5 住户分摊 | 按有效映射归并同窗分表贡献 | `GET /api/allocation/{sample}` | `app/modules/m5_allocation/rules.py` | `tests/test_allocation.py` |
| M6 功率勾稽 | 拆分已归因、未映射和总分表差 | `GET /api/balance/{sample}` | `app/modules/m6_balance/rules.py` | `tests/test_balance.py` |
| M7 账单证据 | 按整分生成可核对的计费证据 | `GET /api/billing/{sample}` | `app/modules/m7_billing/rules.py` | `tests/test_billing.py` |
| M8 响应补偿 | 对照事件前后负荷，剥离自然减产并发布唯一补偿单 | `GET /api/response/{sample}` | `app/modules/m8_response/rules.py` | `tests/test_response.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-06 | 脏样例：全角千位功率 | 同值重传错误隔离 | 统一数字与单位后比较 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_normalized_replay` |
| TRAP-07 | 脏样例：读数含空值哨兵 | 按零功率参与计算 | 留行隔离 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_sentinel_quarantined` |
| TRAP-11 | 脏样例：重传报时用两种时区 | 同一格点产生两条读数 | UTC 校时后去重 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_gateway_and_zone_normalization` |
| TRAP-13 | 脏样例：第一条分表读数迟到 | 依到达顺序直接丢弃 | 在批次内回填原格点 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_late_sample_still_counted` |
| TRAP-14 | 脏样例：同一格点有两种取值 | 末次写入掩盖冲突 | 双记录隔离并使窗口缺样 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_conflicting_replay_not_last_write` |
| TRAP-15 | 干净样例：同表读数单位混用 | 平均功率失真 | 整瓦计算 | `app/modules/m1_sampling/rules.py` | `tests/test_sampling.py::test_mixed_power_units` |
| TRAP-23 | 干净样例：归属在窗口端点切换 | 同一表分入两份账 | 左闭右开生效 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_half_open_version_boundary` |
| TRAP-24 | 边界样例：计费尾差恰为两个半分 | 逐行四舍五入增加合计 | 最大余数补齐 | `app/modules/m7_billing/rules.py` | `tests/test_billing.py::test_cent_tail_conservation` |
| TRAP-25 | 脏样例：同一分表多个有效归属 | 后到者吞并旧账 | 隔离归属并留入未分配 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_overlapping_maps_pending` |
| TRAP-27 | 削峰响应：检修自然降负荷混在总表降幅里 | 把总表降幅整体算作响应成果，重复补偿自然减产 | 凭 maintenance 操作记录把该表削减归入自然桶剔除 | `app/modules/m8_response/rules.py` | `tests/test_response.py::test_ready_sample_classifies_three_curtailment_paths` |
| TRAP-28 | 削峰响应：租户在运行中主动压产但未停机 | 只采信停机操作记录，漏掉仍在运行的压产部分 | 无停机凭证但实测正削减按 curtail 采信并挂复核提示 | `app/modules/m8_response/rules.py` | `tests/test_response.py::test_running_curtail_without_ticket_still_captured` |
| TRAP-29 | 削峰响应：同一事件重复提交 | 重跑再发一份补偿单 | 事件编号为主键，重跑复用原单，输入变化判冲突拒发 | `app/store.py`、`app/response_orders.py` | `tests/test_response.py::test_publish_and_rerun_use_single_event_record` |
| TRAP-30 | 削峰响应：响应窗原始读数丢失 | 缺样按零功率补，虚增削减量 | 任一格点缺样即阻断发布，不做零填充 | `app/modules/m8_response/rules.py` | `tests/test_response.py::test_missing_raw_data_blocks_publication` |
| TRAP-31 | 削峰响应：成员削减与总表核减对不平 | 按成员申报量直接出单 | 总分表差跨期波动超容差即阻断（默认整瓦对平） | `app/modules/m8_response/rules.py` | `tests/test_response.py::test_reduction_identity_difference_blocks` |

## 对账规则

原始取样由采信、同值重传和隔离三类组成。总表共同峰等于有效分表贡献与未分配量，未分配量由未映射分表加总分表差构成。功率勾稽采用三点瓦数和，计费到整分。

## 削峰响应补偿规则

- 基线窗与响应窗窗数相同、端点落在 15 分钟格点；响应窗任一格点缺样即阻断，不做零填充，基线窗缺样只使该表削减不可测。
- 总表核减 = 成员削减合计 + 总分表差跨期变化。残差波动须落在 `tolerance_w`（缺省 0，整瓦对平）内，否则判定成员削减与总表核减无法对平，禁止发布。
- 分表按响应窗全程操作记录分桶：`maintenance` 为自然减产剔除补偿；`shutdown`、`curtail` 为租户主动贡献计入有效削减；无凭证停机不采信，无凭证但实测压产仍采信并挂复核提示。
- 补偿按账户汇总有效削减，费率取事件登记值，金额用最大余数法保留到整分；补偿单按事件编号唯一存档，重跑复用、输入变更判冲突。

## 已知边界

适用固定 5 分钟采样与固定 15 分钟需量周期。总表作为园区峰源。校时偏移在同次批内固定，账单为草案。响应事件一批至多登记一个，操作记录须完整覆盖响应窗，暂不支持跨窗分段动作。
