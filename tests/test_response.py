import copy
from concurrent.futures import ThreadPoolExecutor

from app.pipeline import calculate
from app.response_orders import publish_order
from app.samples import load_sample
from app.store import Archive


def response(data):
    return calculate(data)["response"]


def drop_reading(data, meter, hour, minute):
    target = f"2026-11-23T{hour:02d}:{minute:02d}:00+08:00"
    data["readings"] = [row for row in data["readings"]
                        if not (row["meter"] == meter and row["at"] == target)]


def test_ready_sample_classifies_three_curtailment_paths():
    result = response(load_sample("response"))
    assert result["state"] == "ready"
    by_meter = {row["meter"]: row for row in result["meters"]}
    # 检修造成的自然降负荷不补偿
    assert by_meter["M-001"]["bucket"] == "natural" and by_meter["M-001"]["compensable"] is False
    # 租户主动停机全额采信
    assert by_meter["M-002"]["bucket"] == "shutdown" and by_meter["M-002"]["compensable"] is True
    # 仍在运行但主动压产同样采信，不漏补偿
    assert by_meter["M-003"]["bucket"] == "curtail" and by_meter["M-003"]["compensable"] is True
    assert result["natural_w_sum"] == 180000
    assert result["compensable_w_sum"] == 786000
    assert result["unverified_w_sum"] == 0


def test_main_meter_reduction_includes_natural_drop_once():
    result = response(load_sample("response"))
    # 总表核减 322kW = 自然 60kW + 主动 262kW，自然部分不得再进补偿
    assert result["main_reduction_w_sum"] == 966000
    assert result["main"]["reduction_kw"] == "322.000"
    assert result["residual_change_w_sum"] == 0


def test_compensation_cent_tail_conservation():
    result = response(load_sample("response"))
    lines = {line["account"]: line for line in result["lines"]}
    # 200kW × 1357 分/kW，整分无尾差
    assert lines["厂房乙"]["amount_cents"] == 67850 and lines["厂房乙"]["tail_cent"] == 0
    # 62kW × 1357 分/kW = 84133.5 分，最大余数法补齐 1 分
    assert lines["仓配丙"]["exact_cents"] == "42067/2"
    assert lines["仓配丙"]["amount_cents"] == 21034 and lines["仓配丙"]["tail_cent"] == 1
    assert result["total_cents"] == 88884 and result["difference_cents"] == 0
    assert result["total_cents"] == sum(line["amount_cents"] for line in result["lines"])


def test_missing_raw_data_blocks_publication():
    data = load_sample("response_dirty")
    result = response(data)
    assert result["state"] == "held" and result["total_cents"] is None
    assert [item["code"] for item in result["issues"]] == ["EVENT_WINDOW_GAP", "ORDER_BLOCKED"]
    assert result["main"]["reduction_w"] is None


def test_response_window_gap_does_not_fill_zero():
    data = load_sample("response")
    drop_reading(data, "M-002", 19, 31)  # G-B 报时偏快 60s，校正后为 19:30 格点
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "EVENT_WINDOW_GAP" for item in result["issues"])


def test_reduction_identity_difference_blocks():
    data = load_sample("response")
    # 篡改总表响应期一格读数，成员削减不变而总表核减变化，对不平
    for row in data["readings"]:
        if row["meter"] == "M-000" and row["at"] == "2026-11-23T19:00:00+08:00":
            row["value"] = "70000"
    result = response(data)
    # 总表篡改不造成物理残差为负，但成员削减与总表核减必然对不平
    assert result["state"] == "held"
    assert any(item["code"] == "REDUCTION_DIFFERENCE" for item in result["issues"])


def test_negative_physical_residual_blocks():
    data = load_sample("response")
    for row in data["readings"]:
        if row["meter"] == "M-003" and row["at"] >= "2026-11-23T19:00:00+08:00":
            row["value"] = "200000"
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "NEGATIVE_PERIOD_RESIDUAL" for item in result["issues"])


def test_baseline_gap_does_not_block_response_period():
    data = load_sample("response")
    drop_reading(data, "M-001", 18, 30)  # M-001 走 G-A 无偏移；基线窗缺样，响应窗完整仍可发布
    result = response(data)
    by_meter = {row["meter"]: row for row in result["meters"]}
    assert result["state"] == "ready"
    assert by_meter["M-001"]["baseline_w_sum"] is None
    assert by_meter["M-002"]["compensable"] and by_meter["M-003"]["compensable"]


def test_tolerance_allows_stable_line_loss():
    data = load_sample("response")
    # 仅响应期总表每格抬高 100W（12 格 = 1200W 残差波动）：容差内可发布，收紧即阻断
    for row in data["readings"]:
        if row["meter"] == "M-000" and row["at"] >= "2026-11-23T19:00:00+08:00":
            row["value"] = str(int(row["value"]) + 100)
    data["events"][0]["tolerance_w"] = 1200
    assert response(data)["state"] == "ready"
    data["events"][0]["tolerance_w"] = 1199
    assert response(data)["state"] == "held"


def test_conflicting_actions_block():
    data = load_sample("response")
    data["actions"].append({"meter": "M-002", "kind": "maintenance",
                            "start": "2026-11-23T19:00:00+08:00", "end": "2026-11-23T20:00:00+08:00"})
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "ACTION_CONFLICT" for item in result["issues"])


def test_partial_action_coverage_blocks():
    data = load_sample("response")
    data["actions"] = [row for row in data["actions"] if row["meter"] != "M-003"]
    data["actions"].append({"meter": "M-003", "kind": "curtail",
                            "start": "2026-11-23T19:30:00+08:00", "end": "2026-11-23T20:00:00+08:00"})
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "ACTION_UNCOVERED" for item in result["issues"])


def test_unmapped_compensable_meter_blocks():
    data = load_sample("response")
    data["mappings"] = [row for row in data["mappings"] if row["meter"] != "M-002"]
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "COMPENSABLE_UNMAPPED" for item in result["issues"])


def test_account_switch_inside_response_blocks():
    data = load_sample("response")
    for row in data["mappings"]:
        if row["meter"] == "M-002":
            row["end"] = "2026-11-23T19:30:00+08:00"
    data["mappings"].append({"meter": "M-002", "account": "厂房戊", "version": "v2",
                             "start": "2026-11-23T19:30:00+08:00", "end": "2026-11-23T20:00:00+08:00"})
    result = response(data)
    assert result["state"] == "held"
    assert any(item["code"] == "METER_ACCOUNT_CONFLICT" for item in result["issues"])


def test_shutdown_without_ticket_is_review_not_paid():
    data = load_sample("response")
    data["actions"] = [row for row in data["actions"] if row["meter"] != "M-002"]
    result = response(data)
    by_meter = {row["meter"]: row for row in result["meters"]}
    assert by_meter["M-002"]["bucket"] == "unverified" and by_meter["M-002"]["compensable"] is False
    assert result["state"] == "review"
    assert any(item["code"] == "SHUTDOWN_WITHOUT_TICKET" for item in result["issues"])
    assert result["unverified_w_sum"] == 600000


def test_running_curtail_without_ticket_still_captured():
    data = load_sample("response")
    data["actions"] = [row for row in data["actions"] if row["meter"] != "M-003"]
    result = response(data)
    by_meter = {row["meter"]: row for row in result["meters"]}
    # 只采信操作记录会漏掉的部分：无凭证但实测主动压产仍进补偿，仅挂复核提示
    assert by_meter["M-003"]["compensable"] is True
    assert result["state"] == "review"
    assert any(item["code"] == "CURTAIL_WITHOUT_TICKET" for item in result["issues"])
    assert result["compensable_w_sum"] == 786000


def test_bad_event_held():
    data = load_sample("response")
    data["events"][0]["response_end"] = "2026-11-23T19:30:00+08:00"  # 与基线窗长不一致
    result = response(data)
    assert result["state"] == "held" and result["event"] is None
    assert any(item["code"] == "BAD_EVENT" for item in result["issues"])


def test_old_samples_without_event_are_held():
    for name in ("clean", "dirty"):
        result = response(load_sample(name))
        assert result["state"] == "held"
        assert result["issues"][0]["code"] == "EVENT_MISSING"


def test_publish_and_rerun_use_single_event_record(tmp_path):
    store = Archive(tmp_path / "orders.sqlite3")
    data = load_sample("response")
    first = publish_order(store, copy.deepcopy(data), "response")
    assert first["publish_status"] == "created"
    second = publish_order(store, copy.deepcopy(data), "response")
    assert second["publish_status"] == "reused" and second["id"] == first["id"]
    third = publish_order(store, first and store.get_response_order(first["manifest"]["event_id"])["input"], "response")
    assert third["publish_status"] == "reused"


def test_changed_input_same_event_conflicts(tmp_path):
    store = Archive(tmp_path / "orders.sqlite3")
    store.connect().close()  # 初始化表结构
    data = load_sample("response")
    first = publish_order(store, copy.deepcopy(data), "response")
    changed = copy.deepcopy(data)
    changed["config"]["rate_cents_per_kw"] = 2000
    changed["events"][0]["rate_cents_per_kw"] = 2000
    conflict = publish_order(store, changed, None)
    assert conflict["publish_status"] == "conflict"
    # 原单保持不可变
    assert store.get_response_order(first["manifest"]["event_id"])["id"] == first["id"]


def test_blocked_input_never_creates_order(tmp_path):
    store = Archive(tmp_path / "orders.sqlite3")
    store.connect().close()
    try:
        publish_order(store, load_sample("response_dirty"), "response_dirty")
    except Exception as error:
        assert error.code == "ORDER_BLOCKED"
    import sqlite3
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM response_orders").fetchone()[0] == 0


def test_concurrent_publish_commits_once(tmp_path):
    store = Archive(tmp_path / "orders.sqlite3")
    data = load_sample("response")
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: publish_order(store, copy.deepcopy(data), "response"), range(8)))
    assert [item["publish_status"] for item in outcomes].count("created") == 1


def test_client_publish_rerun_export_flow(client):
    created = client.post("/api/response-orders", json={"sample": "response"}).json()
    event_id = created["manifest"]["event_id"]
    rerun = client.post(f"/api/response-orders/{event_id}/rerun").json()
    assert rerun["publish_status"] == "reused" and rerun["order_id"] == created["order_id"]
    export = client.get(f"/api/response-orders/{event_id}/export")
    assert export.headers["content-disposition"].startswith("attachment;")
    assert export.json()["result"]["response"]["total_cents"] == created["result"]["response"]["total_cents"]
    blocked = client.post("/api/response-orders", json={"sample": "response_dirty"})
    assert blocked.status_code == 422 and blocked.json()["code"] == "ORDER_BLOCKED"
    assert client.get("/api/response-orders/unknown").status_code == 404


def test_client_same_event_id_changed_input_conflicts(client):
    first = client.post("/api/response-orders", json={"sample": "response"}).json()
    changed = copy.deepcopy(load_sample("response"))
    changed["events"][0]["rate_cents_per_kw"] = 9999
    conflict = client.post("/api/response-orders", json={"data": changed}).json()
    assert conflict["publish_status"] == "conflict"
    # 原单金额保持不变
    event_id = first["manifest"]["event_id"]
    stored = client.get(f"/api/response-orders/{event_id}").json()
    assert stored["result"]["response"]["total_cents"] == first["result"]["response"]["total_cents"]


def test_response_page_includes_stage_nav(client):
    page = client.get("/work/response?sample=response").text
    assert 'aria-current="page">响应补偿' in page
    assert "发布唯一补偿单" in page and "下载归档" in page
    held_page = client.get("/work/response?sample=response_dirty").text
    assert "阻断 · 不能发布" in held_page
