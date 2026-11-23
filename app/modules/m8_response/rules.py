from collections import defaultdict

from app.evidence import issue, key, rows, timestamp
from app.modules.m7_billing.rules import charge_rows
from app.timegrid import WINDOW, window_label
from app.units import demand_kw

ACTION_KINDS = ("maintenance", "shutdown", "curtail")
COMPENSABLE_KINDS = ("shutdown", "curtail")
WARNING_CODES = {"SHUTDOWN_WITHOUT_TICKET", "CURTAIL_WITHOUT_TICKET"}
BLOCKING_CODES = {"EVENT_WINDOW_GAP", "ACTION_CONFLICT", "ACTION_UNCOVERED",
                  "METER_ACCOUNT_CONFLICT", "COMPENSABLE_UNMAPPED", "REDUCTION_DIFFERENCE",
                  "NEGATIVE_PERIOD_RESIDUAL", "BAD_EVENT", "BAD_ACTION", "EVENT_MISSING"}


def raw_tolerance(data):
    """总分表差跨期波动容差（整瓦），在 events[0].tolerance_w 登记，缺省为 0。"""
    events = data.get("events") or []
    if not events:
        return 0
    value = events[0].get("tolerance_w", 0)
    return value if type(value) is int and value >= 0 else 0


def parse_event(data, sampling):
    """解析响应事件登记；无法构出有效事件时返回 None 与阻断问题。"""
    start, end = timestamp(sampling["start"]), timestamp(sampling["end"])
    event_rows, problems = [], []
    for raw, source in rows(data, "events"):
        try:
            event_id = key(raw["event_id"])
            baseline_start, baseline_end = timestamp(raw["baseline_start"]), timestamp(raw["baseline_end"])
            response_start, response_end = timestamp(raw["response_start"]), timestamp(raw["response_end"])
            rate = raw["rate_cents_per_kw"]
            if type(rate) is not int or not 0 <= rate <= 10**7:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            problems.append(issue("BAD_EVENT", source))
            continue
        event_rows.append({"event_id": event_id, "baseline_start": baseline_start,
                           "baseline_end": baseline_end, "response_start": response_start,
                           "response_end": response_end, "rate_cents_per_kw": rate, "source": source})
    if len(event_rows) > 1:
        problems.append(issue("BAD_EVENT", *(item["source"] for item in event_rows[1:])))
        event_rows = event_rows[:1]
    if not event_rows:
        return None, problems or [issue("EVENT_MISSING")]
    event = event_rows[0]
    try:
        periods = ((event["baseline_start"], event["baseline_end"]),
                   (event["response_start"], event["response_end"]))
        if any(begin >= finish for begin, finish in periods):
            raise ValueError
        if not (start <= event["baseline_start"] and event["response_end"] <= end):
            raise ValueError
        if event["baseline_end"] > event["response_start"]:
            raise ValueError
        for begin, finish in periods:
            if begin.minute % 15 or finish.minute % 15 or begin.second or finish.second or begin.microsecond:
                raise ValueError
            if (finish - begin) % WINDOW:
                raise ValueError
        if (event["baseline_end"] - event["baseline_start"]) != (event["response_end"] - event["response_start"]):
            raise ValueError
    except (TypeError, ValueError):
        problems.append(issue("BAD_EVENT", event["source"]))
        return None, problems
    return event, problems


def parse_actions(data, roster, event):
    """按表归组操作记录，并裁决每张分表在响应窗的操作类别。"""
    grouped, problems = defaultdict(list), []
    for raw, source in rows(data, "actions"):
        try:
            meter, kind = key(raw["meter"]), key(raw["kind"])
            begin, finish = timestamp(raw["start"]), timestamp(raw["end"])
            if meter not in roster or kind not in ACTION_KINDS or begin >= finish:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            problems.append(issue("BAD_ACTION", source))
            continue
        grouped[meter].append({"kind": kind, "start": begin, "end": finish, "source": source})

    response_start, response_end = event["response_start"], event["response_end"]
    decisions = {}
    for meter in sorted(roster):
        covering = [action for action in grouped[meter]
                    if action["start"] <= response_start and action["end"] >= response_end]
        sources = [action["source"] for action in grouped[meter]]
        if not covering:
            decisions[meter] = {"kind": None, "sources": sources}
            if grouped[meter]:
                problems.append(issue("ACTION_UNCOVERED", *sources))
            continue
        kinds = {action["kind"] for action in covering}
        if len(kinds) > 1:
            decisions[meter] = {"kind": "conflict",
                                "sources": [action["source"] for action in covering]}
            problems.append(issue("ACTION_CONFLICT", *(action["source"] for action in covering)))
        else:
            decisions[meter] = {"kind": covering[0]["kind"],
                                "sources": [action["source"] for action in covering]}
    return decisions, problems


def period_windows(alignment, begin, finish):
    return [window for window in alignment if begin <= timestamp(window["window"]) < finish]


def period_total(window_rows, meter):
    """该表在期间的三点瓦数和合计；任一窗口缺样返回 None。"""
    totals = [row["meters"][meter]["w_sum"] for row in window_rows]
    return None if any(value is None for value in totals) else sum(totals)


def period_sources(window_rows, meter):
    sources = []
    for row in window_rows:
        sources.extend(row["meters"][meter]["sources"])
    return sources


def held_result(event, rate, windows, meters, issues, main_fields=None):
    return {"state": "held", "event": event, "rate_cents_per_kw": rate, "windows": windows,
            "main": main_fields, "meters": meters, "accounts": [], "lines": [],
            "natural_w_sum": None, "natural_kw": None, "unverified_w_sum": None, "unverified_kw": None,
            "compensable_w_sum": None, "compensable_kw": None, "main_reduction_w_sum": None,
            "residual_base_w_sum": None, "residual_response_w_sum": None,
            "residual_change_w_sum": None, "currency": "CNY",
            "total_cents": None, "difference_cents": None, "issues": issues}


def run(data, previous):
    sampling = previous["sampling"]
    alignment_rows = previous["alignment"]["windows"]
    issues = []
    event, event_problems = parse_event(data, sampling)
    issues.extend(event_problems)
    if event is None:
        return held_result(None, None, 0, [], issues)

    roster = list(sampling["meters"])
    decisions, action_problems = parse_actions(data, set(roster), event)
    issues.extend(action_problems)

    base_rows = period_windows(alignment_rows, event["baseline_start"], event["baseline_end"])
    response_rows = period_windows(alignment_rows, event["response_start"], event["response_end"])
    windows = len(response_rows)
    main = sampling["main_meter"]

    event_label = {"event_id": event["event_id"],
                   "baseline_window": {"start": event["baseline_start"].isoformat(),
                                       "end": event["baseline_end"].isoformat(),
                                       "label": f"{window_label(event['baseline_start'])}–{window_label(event['baseline_end'] - WINDOW)}"},
                   "response_window": {"start": event["response_start"].isoformat(),
                                       "end": event["response_end"].isoformat(),
                                       "label": f"{window_label(event['response_start'])}–{window_label(event['response_end'] - WINDOW)}"}}

    # 阻断项一：响应窗原始数据丢失（任一格点缺样即不得发布）。
    missing_sources = [source for row in response_rows for gap in row["missing"] for source in gap["sources"]]
    response_gaps = [gap for row in response_rows for gap in row["missing"]]
    if response_gaps:
        issues.append(issue("EVENT_WINDOW_GAP", *missing_sources))
        base_main = period_total(base_rows, main)
        meters = []
        for meter in roster:
            base_w, response_w = period_total(base_rows, meter), period_total(response_rows, meter)
            meters.append({"meter": meter, "account": None, "action_kind": decisions[meter]["kind"],
                           "bucket": "unverified", "mapping_state": None,
                           "baseline_w_sum": base_w, "response_w_sum": response_w,
                           "baseline_kw": demand_kw(base_w) if base_w is not None else None,
                           "response_kw": None, "reduction_w": None, "reduction_kw": None,
                           "compensable": False, "action_sources": decisions[meter]["sources"],
                           "sources": period_sources(base_rows, meter) + period_sources(response_rows, meter),
                           "mapping_sources": []})
        issues.append(issue("ORDER_BLOCKED"))
        return held_result(event_label, event["rate_cents_per_kw"], windows, meters, issues,
                           main_fields={"meter": main, "baseline_w_sum": base_main, "response_w_sum": None,
                                        "baseline_kw": demand_kw(base_main) if base_main is not None else None,
                                        "response_kw": None, "reduction_w": None, "reduction_kw": None})

    def residual_of(rows_):
        main_total = period_total(rows_, main)
        sub_totals = [period_total(rows_, meter) for meter in roster]
        # 任一表缺样时残差不可计算，交由 EVENT_WINDOW_GAP 阻断，不得把缺样当零功率
        if main_total is None or any(value is None for value in sub_totals):
            return None
        return main_total - sum(sub_totals)

    # 阻断项二：任一周期物理残差为负（分表合计超过总表）。
    residual_base, residual_response = residual_of(base_rows), residual_of(response_rows)
    residual_values = [value for value in (residual_base, residual_response) if value is not None]
    if residual_values and min(residual_values) < 0:
        issues.append(issue("NEGATIVE_PERIOD_RESIDUAL"))

    mapping_windows = previous["mapping"]["windows"]
    base_main, response_main = period_total(base_rows, main), period_total(response_rows, main)
    main_reduction = (base_main - response_main) if base_main is not None and response_main is not None else None
    sum_reduction = 0
    meter_rows, blocked = [], bool(residual_values) and min(residual_values) < 0

    for meter in roster:
        base_w, response_w = period_total(base_rows, meter), period_total(response_rows, meter)
        reduction_w = None if base_w is None or response_w is None else base_w - response_w
        if reduction_w is not None:
            sum_reduction += reduction_w
        decision = decisions[meter]
        relation_windows = [mapping_windows[row["window"]][meter] for row in response_rows]
        accounts = {relation["account"] for relation in relation_windows}
        states = {relation["state"] for relation in relation_windows}
        mapping_sources = [source for relation in relation_windows for source in relation["sources"]]
        account_conflict = len(accounts) > 1
        if account_conflict:
            issues.append({**issue("METER_ACCOUNT_CONFLICT", *mapping_sources), "meter": meter})

        kind = decision["kind"]
        stopped = response_w == 0 and base_w and base_w > 0
        measurable = reduction_w is not None
        if kind in (None, "conflict"):
            if kind == "conflict":
                bucket, blocking = "unverified", True
            elif stopped:
                bucket, blocking = "unverified", False
                issues.append({**issue("SHUTDOWN_WITHOUT_TICKET",
                                       *period_sources(base_rows, meter), *period_sources(response_rows, meter)),
                               "meter": meter})
            else:
                bucket, blocking = "curtail", False
                if measurable and reduction_w > 0:
                    issues.append({**issue("CURTAIL_WITHOUT_TICKET", *period_sources(response_rows, meter)),
                                   "meter": meter})
        elif kind == "maintenance":
            bucket, blocking = "natural", False
            if measurable and reduction_w > 0:
                issues.append({**issue("NATURAL_DROP_EXCLUDED", *decision["sources"]), "meter": meter})
        else:
            bucket, blocking = kind, False

        unassigned = states != {"assigned"} or account_conflict
        if unassigned and bucket in COMPENSABLE_KINDS and measurable and reduction_w > 0:
            issues.append({**issue("COMPENSABLE_UNMAPPED", *mapping_sources), "meter": meter})
            blocking = True
        if account_conflict:
            blocking = True
        blocked = blocked or blocking

        meter_rows.append({
            "meter": meter, "account": next(iter(accounts)) if len(accounts) == 1 else None,
            "action_kind": kind, "bucket": bucket, "mapping_state": "|".join(sorted(states)),
            "baseline_w_sum": base_w, "response_w_sum": response_w,
            "baseline_kw": demand_kw(base_w) if base_w is not None else None,
            "response_kw": demand_kw(response_w) if response_w is not None else None,
            "reduction_w": reduction_w,
            "reduction_kw": demand_kw(max(reduction_w, 0)) if measurable else None,
            "compensable": measurable and bucket in COMPENSABLE_KINDS and reduction_w > 0
                           and not unassigned and not account_conflict,
            "action_sources": decision["sources"],
            "sources": period_sources(base_rows, meter) + period_sources(response_rows, meter),
            "mapping_sources": mapping_sources,
        })

    # 阻断项三：成员削减与总表核减对不平。
    # 总表核减 = 成员削减合计 + 总分表差变化；线损/表差在两期间应保持稳定，
    # 残差波动超过登记容差（默认整瓦对平）即视为无法对平，
    # 任何篡改总表或成员读数都会放大残差波动而被拦下。
    measurable_main = base_main is not None and response_main is not None
    residual_change = (base_main - response_main - sum_reduction) if measurable_main else None
    tolerance = raw_tolerance(data)
    if residual_base is not None and residual_response is not None and abs(residual_base - residual_response) > tolerance:
        issues.append(issue("REDUCTION_DIFFERENCE"))
        blocked = True

    def positive_drop(row):
        return max(row["reduction_w"], 0) if row["reduction_w"] is not None else 0

    natural_w = sum(positive_drop(row) for row in meter_rows if row["bucket"] == "natural")
    unverified_w = sum(positive_drop(row) for row in meter_rows if row["bucket"] == "unverified")
    compensable_meters = [row for row in meter_rows if row["compensable"]]
    compensable_w = sum(row["reduction_w"] for row in compensable_meters)

    grouped_accounts = defaultdict(list)
    for row in compensable_meters:
        grouped_accounts[row["account"]].append(row)
    entries = [{"account": account, "w_sum": sum(item["reduction_w"] for item in items),
                "meters": [item["meter"] for item in items]}
               for account, items in sorted(grouped_accounts.items())]
    rate = event["rate_cents_per_kw"]
    lines, total_cents = charge_rows(entries, rate, denominator=3000 * windows)
    for line in lines:
        line["kw"] = demand_kw(line["w_sum"])

    present_codes = {item["code"] for item in issues}
    if present_codes & BLOCKING_CODES or blocked:
        state = "held"
        if "ORDER_BLOCKED" not in present_codes:
            issues.append(issue("ORDER_BLOCKED"))
    elif present_codes & WARNING_CODES:
        state = "review"
    else:
        state = "ready"

    return {
        "state": state, "event": event_label, "rate_cents_per_kw": rate, "windows": windows,
        "main": {"meter": main, "baseline_w_sum": base_main, "response_w_sum": response_main,
                 "baseline_kw": demand_kw(base_main) if base_main is not None else None,
                 "response_kw": demand_kw(response_main) if response_main is not None else None,
                 "reduction_w": main_reduction,
                 "reduction_kw": demand_kw(max(main_reduction, 0)) if main_reduction is not None else None},
        "meters": meter_rows, "accounts": entries, "lines": lines,
        "natural_w_sum": natural_w, "natural_kw": demand_kw(natural_w),
        "unverified_w_sum": unverified_w, "unverified_kw": demand_kw(unverified_w),
        "compensable_w_sum": compensable_w, "compensable_kw": demand_kw(compensable_w),
        "main_reduction_w_sum": main_reduction,        "residual_base_w_sum": residual_base, "residual_response_w_sum": residual_response,
        "residual_change_w_sum": residual_change,
        "currency": "CNY",
        "total_cents": None if state == "held" else total_cents,
        "difference_cents": None if state == "held" else total_cents - sum(line["amount_cents"] for line in lines),
        "issues": issues,
    }
