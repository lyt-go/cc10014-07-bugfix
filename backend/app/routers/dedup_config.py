"""去重配置接口：维护去重配置，覆盖提交校验、标记生效、停用配置等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.dedup_config import DatasetPageOutOfRange, DedupConfigService

router = APIRouter(prefix="/api/dedup_config", tags=["去重配置"])

service = DedupConfigService()

LIST_FIELDS = ["配置编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值", "配置版本", "目标数据集"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、配置编号关键字与状态过滤去重配置列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dedup_config.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条去重配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry_detail(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/dataset")
def get_dataset(
    entry_id: int,
    page: int = Query(default=1, description="页码，越界会被拒绝"),
    size: int = Query(default=20, ge=1, le=200),
) -> dict:
    """按业务键顺序分页读取去重明细数据集；页码越界时返回 400 而不是空页。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    try:
        payload = service.dataset_page(entry_id, page=page, size=size)
    except DatasetPageOutOfRange as error:
        raise HTTPException(
            status_code=400,
            detail=f"页码越界：请求第 {error.page} 页，数据集共 {error.page_count} 页（{error.total} 个业务键）",
        ) from error
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"去重配置 {entry_id} 尚未生成去重明细数据集，请先执行去重或构建明细表",
        )
    return payload


@router.post("/{entry_id}/dataset/refresh")
def refresh_dataset(entry_id: int, payload: dict[str, object] | None = None) -> dict:
    """从目标数据集重新构建去重明细数据集；同一批次重复执行结果保持不变。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    batch_id = str((payload or {}).get("batch_id") or "")
    dataset = service.build_dataset(entry_id, batch_id=batch_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    return {
        "ok": True,
        "message": (
            f"去重明细数据集已刷新，共 {dataset.get('key_count', 0)} 个业务键、"
            f"{dataset.get('retained_count', 0)} 条保留记录"
        ),
        "dataset": service.dataset_page(entry_id, page=1, size=200),
    }


@router.get("/{entry_id}/dataset/keys")
def list_business_keys(entry_id: int) -> dict:
    """按业务键顺序返回全部业务键的保留记录与去重明细（不分页，供跨模块编排查询）。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    dataset = service.get_dataset(entry_id)
    if dataset is None:
        raise HTTPException(
            status_code=404,
            detail=f"去重配置 {entry_id} 尚未生成去重明细数据集，请先执行去重或构建明细表",
        )
    return {
        "config_no": dataset.get("config_no", ""),
        "config_version": dataset.get("config_version", ""),
        "watermark": dataset.get("watermark", ""),
        "key_count": dataset.get("key_count", 0),
        "items": dataset.get("items", []),
    }


@router.get("/{entry_id}/dedup_dataset")
def get_dataset_alias(
    entry_id: int,
    page: int = Query(default=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict:
    """去重明细数据集的别名入口，与 /dataset 行为一致。"""
    return get_dataset(entry_id, page=page, size=size)


@router.get("/{entry_id}/dataset/keys/{business_key}")
def get_business_key(entry_id: int, business_key: str) -> dict:
    """回查单个业务键的保留记录、被淘汰重复记录与冲突说明。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"去重配置 {entry_id} 不存在或已归档")
    item = service.business_key_detail(entry_id, business_key)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"业务键 {business_key} 不在去重配置 {entry_id} 的去重明细数据集中",
        )
    return item


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条去重配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="去重配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条去重配置执行提交校验、标记生效、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
