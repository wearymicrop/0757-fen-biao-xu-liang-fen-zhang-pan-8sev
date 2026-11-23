from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.errors import InputError
from app.manifest import identity, make_manifest
from app.pipeline import calculate, validate
from app.samples import load_sample
from app.settings import PROJECT

router = APIRouter(prefix="/api/response-orders", tags=["响应补偿单"])


class OrderSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample: str | None = None
    data: dict[str, Any] | None = None


def _event_id(data):
    events = data.get("events") or []
    if len(events) != 1:
        raise InputError("EVENT_MISSING", "批次须恰好登记一个削峰响应事件")
    event_id = events[0].get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise InputError("BAD_EVENT", "响应事件编号有误")
    return event_id.strip()


def publish_order(store, data, sample):
    data = validate(data)
    event_id = _event_id(data)
    result = calculate(data)
    response = result["response"]
    if response["state"] == "held":
        codes = [item["code"] for item in response["issues"]]
        raise InputError("ORDER_BLOCKED", f"存在阻断项，补偿单不能发布：{'、'.join(dict.fromkeys(codes))}")
    digest = identity(data, sample)
    record = {"id": digest, "order_id": event_id,
              "manifest": {**make_manifest(data, sample, digest, 0.0), "event_id": event_id,
                           "kind": "response_order"},
              "input": data, "result": result}
    stored, status = store.save_response_order(event_id, digest, record)
    return {**stored, "publish_status": status}


@router.post("")
def create_order(body: OrderSubmission, request: Request):
    if (body.sample is None) == (body.data is None):
        raise InputError("CHOOSE_SOURCE", "请选择一个样例或提交数据")
    data = load_sample(body.sample) if body.sample is not None else body.data
    return publish_order(request.app.state.archive, data, body.sample)


@router.post("/{event_id}/rerun")
def rerun_order(event_id: str, request: Request):
    stored = request.app.state.archive.get_response_order(event_id)
    if stored["manifest"]["event_id"] != event_id:
        raise InputError("ORDER_NOT_FOUND", "找不到该事件的补偿单", 404)
    return publish_order(request.app.state.archive, stored["input"], stored["manifest"]["sample"])


@router.get("/{event_id}")
def get_order(event_id: str, request: Request):
    return request.app.state.archive.get_response_order(event_id)


@router.get("/{event_id}/export")
def export_order(event_id: str, request: Request):
    record = request.app.state.archive.get_response_order(event_id)
    return JSONResponse(record, headers={
        "Content-Disposition": f'attachment; filename="{PROJECT}-response-order-{event_id}.json"'
    })
