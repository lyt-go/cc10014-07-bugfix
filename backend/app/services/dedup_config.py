"""去重配置业务规则：按业务键去重并保留可追溯记录，生成去重明细数据集，驱动DWD表生成。

保留策略以配置声明为准（``取最新`` / ``保留最后`` / ``保留最先``），排序字段支持
``dt desc`` / ``pay_time asc`` 这样的写法。排序时间完全相同的重复记录按记录内容做
确定性裁决，不再沿用“取尾行”，因此重复运行同一批次得到的保留记录、业务键数量与
去重水位都保持不变。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "dedup_config"
LIST_FIELDS = ["配置编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值", "配置版本", "目标数据集"]
REQUIRED_FIELDS = ["配置编号", "业务键列"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用配置": "已停用", "标记废弃": "已停用"}
NEGATIVE_ACTIONS = ["停用配置", "标记废弃"]

# 去重明细数据集的保留原因
REASON_OLDER = "保留策略淘汰"
REASON_TIE = "相同排序时间"
REASON_CROSS_WINDOW = "跨窗口到达"


class DatasetPageOutOfRange(Exception):
    """请求的页码超出数据集页数：由路由层转换成 400，拒绝越界读取。"""

    def __init__(self, page: int, page_count: int, total: int) -> None:
        self.page = page
        self.page_count = page_count
        self.total = total
        super().__init__(f"页码 {page} 超出范围：去重明细数据集共 {page_count} 页（{total} 个业务键）")


class DedupConfigService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("配置编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self._with_dataset_summary(row) for row in rows[start:start + size]], total

    def _with_dataset_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表行附带数据集摘要，方便从列表入口直接回查业务键结果。"""
        return self.dataset_summary(dict(row))

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_entry_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情读取：与列表同源带上数据集摘要，避免详情页回查到旧值。"""
        entry = self.get_entry(entry_id)
        return self._with_dataset_summary(entry) if entry else None

    def dataset_summary(self, entry: dict[str, Any]) -> dict[str, Any]:
        """把数据集摘要写回配置本身，让列表、详情与明细层读到同一份结果。"""
        item = dict(entry)
        dataset = store.get_dedup_dataset(int(item.get("id", 0)))
        if not dataset:
            return item
        item["去重水位"] = dataset.get("watermark", "")
        item["业务键数量"] = dataset.get("key_count", 0)
        item["保留记录数"] = dataset.get("retained_count", 0)
        item["去重明细数据集版本"] = dataset.get("dataset_version", 0)
        item["数据集批次"] = dataset.get("batch_id", "")
        return item

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于去重配置可执行范围"
        target = ACTION_RULES[action]
        current = entry.get("status", STATUS_ORDER[0])
        current_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else 0
        target_idx = STATUS_ORDER.index(target)
        # 状态只能按顺序推进：不回退、不跳级
        if target_idx < current_idx:
            return None, f"状态不能回退：当前{current} → 目标{target}"
        if target_idx > current_idx + 1:
            return None, f"状态不能跳级：当前{current} → 目标{target}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action in ("标记生效", "提交校验"):
            self._mark_dwd_refresh(entry)
        return entry, f"去重配置已{action}"

    def _mark_dwd_refresh(self, entry: dict[str, Any]) -> None:
        dataset = entry.get("目标数据集", "")
        config_no = entry.get("配置编号", "")
        for row in store.rows("dwd_layer"):
            if row.get("去重配置") == config_no:
                row["待刷新"] = True
                row["刷新原因"] = f"去重配置 {config_no} 变更"
                store.save_version("dwd_layer", row["id"], row)
            else:
                parent = row.get("父层来源", "")
                ods = store.find_by_field("ods_layer", "表编号", parent)
                if ods and ods.get("表名称") == dataset:
                    row["待刷新"] = True
                    row["刷新原因"] = f"去重配置 {config_no} 变更"
                    store.save_version("dwd_layer", row["id"], row)

    def update_dedup_version(self, entry_id: int, new_values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """更新去重配置并升级版本号。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重配置 {entry_id} 不存在"
        old_version = entry.get("配置版本", "v1")
        try:
            vnum = int(str(old_version).replace("v", ""))
            entry["配置版本"] = f"v{vnum + 1}"
        except Exception:
            entry["配置版本"] = "v2"
        for k in ["业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值"]:
            if k in new_values:
                entry[k] = new_values[k]
        entry["pending"] = True
        store.save_version(MODULE, entry_id, entry)
        self._mark_dwd_refresh(entry)
        return entry, f"去重配置已升级至 {entry['配置版本']}"

    # ---- 去重明细数据集 ----

    def deduplicate(
        self,
        entry_id: int,
        data_rows: list[dict[str, Any]],
        *,
        batch_id: str = "",
        persist: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按业务键去重并产出可追溯的去重明细数据集。

        返回 ``(保留记录, 数据集)``；同一批次的相同输入重复运行会复用已落地的数据集，
        业务键数量、保留记录与去重水位保持不变。
        """
        entry = self.get_entry(entry_id)
        if entry is None:
            return list(data_rows), {}
        dataset = self._build_dataset(entry, data_rows, batch_id)
        if persist:
            dataset = self._persist_dataset(entry_id, dataset)
        kept = [dict(item["retained"]) for item in dataset.get("items", [])]
        return kept, dataset

    def apply_dedup(self, entry_id: int, data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按业务键去重，保留策略与排序字段按配置执行；结果稳定可复现。"""
        kept, _ = self.deduplicate(entry_id, data_rows, persist=False)
        return kept

    def build_dataset(
        self,
        entry_id: int,
        rows: list[dict[str, Any]] | None = None,
        *,
        batch_id: str = "",
    ) -> dict[str, Any] | None:
        """从目标数据集（或显式传入的行）构建并持久化去重明细数据集。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        source = rows if rows is not None else store.table_rows(str(entry.get("目标数据集") or ""))
        if rows is None and not source:
            # 目标数据集还没来得及落数据时不要把已有结果刷成空集，保持上一次可追溯结果
            existing = store.get_dedup_dataset(entry_id)
            if existing and existing.get("key_count"):
                return existing
        if not batch_id:
            # 未显式指定批次时沿用目标数据集所在贴源表的创建批次，保证刷新可重复
            batch_id = self._default_batch(str(entry.get("目标数据集") or ""))
        _, dataset = self.deduplicate(entry_id, source, batch_id=batch_id)
        return dataset

    def _default_batch(self, dataset: str) -> str:
        if not dataset:
            return ""
        ods = store.find_by_field("ods_layer", "表名称", dataset)
        return str((ods or {}).get("创建批次") or "")

    def get_dataset(self, entry_id: int) -> dict[str, Any] | None:
        return store.get_dedup_dataset(entry_id)

    def dataset_page(self, entry_id: int, page: int = 1, size: int = 20) -> dict[str, Any] | None:
        """按业务键顺序分页读取数据集；页码越界时抛出异常由路由层拒绝。"""
        dataset = self.get_dataset(entry_id)
        if dataset is None:
            return None
        return self._page(dataset, page, size)

    def business_key_detail(self, entry_id: int, business_key: str) -> dict[str, Any] | None:
        dataset = self.get_dataset(entry_id)
        if dataset is None:
            return None
        for item in dataset.get("items", []):
            if str(item.get("business_key")) == str(business_key):
                return copy.deepcopy(item)
        return None

    def _persist_dataset(self, entry_id: int, dataset: dict[str, Any]) -> dict[str, Any]:
        signature = self._signature(dataset)
        existing = store.get_dedup_dataset(entry_id)
        # 同一批次重复运行：内容一致就复用原数据集，不生成新版本、不改水位
        if existing and existing.get("signature") == signature:
            return existing
        dataset.pop("dataset_version", None)
        dataset["signature"] = signature
        return store.save_dedup_dataset(entry_id, dataset)

    def _build_dataset(
        self,
        entry: dict[str, Any],
        rows: list[dict[str, Any]],
        batch_id: str,
    ) -> dict[str, Any]:
        key_col = str(entry.get("业务键列") or "").strip()
        sort_col, sort_descending = self._normalize_sort(
            str(entry.get("排序字段") or ""), str(entry.get("保留策略") or ""))
        keep_policy = str(entry.get("保留策略") or "")
        keep_max = self._keep_newest(keep_policy)
        window_days = self._window_days(entry.get("时间窗口"))

        prepared: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            source_row = dict(row)
            key = str(source_row.get(key_col, "")).strip() if key_col else ""
            raw_value = source_row.get(sort_col, "") if sort_col else ""
            prepared.append({
                "index": index,
                "key": key,
                "row": source_row,
                "raw": raw_value,
                "comp": self._comparable(raw_value),
                "canonical": json.dumps(source_row, ensure_ascii=False, sort_keys=True, default=str),
                "time": self._parse_time(raw_value),
            })

        timed = [item for item in prepared if item["time"] is not None]
        watermark = ""
        window_start: datetime | None = None
        if timed:
            latest = max(timed, key=lambda item: (item["time"], item["index"]))
            watermark = str(latest["raw"])
            if window_days:
                window_start = latest["time"] - timedelta(days=window_days)

        groups: dict[str, list[dict[str, Any]]] = {}
        unkeyed = 0
        for item in prepared:
            if not item["key"]:
                unkeyed += 1
                continue
            groups.setdefault(item["key"], []).append(item)

        items: list[dict[str, Any]] = []
        conflict_count = 0
        cross_window_count = 0
        warnings: list[str] = []
        for seq, business_key in enumerate(sorted(groups, key=lambda key: (key.casefold(), key)), start=1):
            group = groups[business_key]
            pool = group
            window_fallback = False
            if window_start is not None:
                in_window = [item for item in group
                             if item["time"] is None or item["time"] >= window_start]
                if in_window:
                    pool = in_window
                else:
                    window_fallback = True
            # 排序时间相同时按记录内容做确定性裁决，再以源行位置兜底：
            # 同一批次即使行序抖动，重跑也得到同一个终态，不依赖“尾行”
            picker = max if keep_max else min
            kept = picker(pool, key=lambda item: (item["comp"], item["canonical"], item["index"]))
            duplicates = sorted(
                (item for item in group if item is not kept),
                key=lambda item: (item["comp"], item["canonical"], item["index"]),
            )

            duplicate_records: list[dict[str, Any]] = []
            tie_indexes: list[int] = []
            for item in duplicates:
                cross_window = (
                    window_start is not None
                    and item["time"] is not None
                    and item["time"] < window_start
                )
                if cross_window:
                    reason = REASON_CROSS_WINDOW
                    cross_window_count += 1
                elif item["comp"] == kept["comp"]:
                    reason = REASON_TIE
                else:
                    reason = REASON_OLDER
                if item["comp"] == kept["comp"]:
                    tie_indexes.append(item["index"])
                duplicate_records.append({
                    "source_index": item["index"],
                    "sort_value": item["raw"],
                    "reason": reason,
                    "record": item["row"],
                })

            key_warnings: list[str] = []
            if tie_indexes:
                conflict_count += 1
                key_warnings.append(
                    f"业务键 {business_key} 存在 {len(tie_indexes) + 1} 条相同排序时间的重复记录，"
                    f"已按记录内容稳定保留源行第 {kept['index'] + 1} 条"
                )
                warnings.append(key_warnings[-1])
            if any(record["reason"] == REASON_CROSS_WINDOW for record in duplicate_records):
                key_warnings.append(
                    f"业务键 {business_key} 存在跨窗口到达记录，DWD 保留末条记录并附跨窗口到达说明"
                )
                warnings.append(key_warnings[-1])
            if window_fallback:
                key_warnings.append(
                    f"业务键 {business_key} 在时间窗口内没有记录，按全量记录回退裁决"
                )
                warnings.append(key_warnings[-1])

            items.append({
                "seq": seq,
                "business_key": business_key,
                "sort_value": kept["raw"],
                "retained": kept["row"],
                "source_index": kept["index"],
                "duplicate_count": len(duplicate_records),
                "duplicates": duplicate_records,
                "conflict": bool(tie_indexes),
                "conflict_indexes": tie_indexes,
                "cross_window": any(
                    record["reason"] == REASON_CROSS_WINDOW for record in duplicate_records),
                "window": "窗口外回退" if window_fallback else "窗口内",
                "warnings": key_warnings,
            })

        return {
            "config_id": int(entry.get("id", 0)),
            "config_no": str(entry.get("配置编号", "")),
            "config_version": str(entry.get("配置版本", "")),
            "target_dataset": str(entry.get("目标数据集", "")),
            "business_key_column": key_col,
            "sort_column": sort_col,
            "sort_descending": sort_descending,
            "keep_policy": str(entry.get("保留策略", "")),
            "window_days": window_days,
            "batch_id": batch_id,
            "watermark": watermark,
            "window_start": window_start.isoformat() if window_start else "",
            "source_rows": len(rows),
            "unkeyed_count": unkeyed,
            "key_count": len(items),
            "retained_count": len(items),
            "dropped_count": sum(item["duplicate_count"] for item in items),
            "conflict_count": conflict_count,
            "cross_window_count": cross_window_count,
            "items": items,
            "warnings": warnings[:50],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _page(self, dataset: dict[str, Any], page: int, size: int) -> dict[str, Any]:
        items = dataset.get("items", [])
        total = len(items)
        size = max(1, int(size))
        page = int(page)
        page_count = max(1, (total + size - 1) // size)
        if page < 1 or page > page_count:
            raise DatasetPageOutOfRange(page, page_count, total)
        start = (page - 1) * size
        entry = self.get_entry(int(dataset.get("config_id", 0)))
        current_version = str(entry.get("配置版本", "")) if entry else ""
        payload = {
            key: dataset.get(key)
            for key in (
                "config_id", "config_no", "config_version", "target_dataset",
                "business_key_column", "sort_column", "sort_descending", "keep_policy",
                "window_days", "batch_id", "watermark", "window_start", "source_rows",
                "unkeyed_count", "key_count", "retained_count", "dropped_count",
                "conflict_count", "cross_window_count", "dataset_version", "generated_at",
            )
        }
        payload.update({
            "items": copy.deepcopy(items[start:start + size]),
            "total": total,
            "page": page,
            "size": size,
            "page_count": page_count,
            "warnings": list(dataset.get("warnings", [])),
            # 配置随后被升级也不改写已落地的数据集，只标记过期，页面仍展示原值
            "stale": bool(current_version and current_version != str(dataset.get("config_version", ""))),
            "current_config_version": current_version,
        })
        return payload

    def _signature(self, dataset: dict[str, Any]) -> str:
        canonical = {
            "config_version": dataset.get("config_version"),
            "business_key_column": dataset.get("business_key_column"),
            "sort_column": dataset.get("sort_column"),
            "keep_policy": dataset.get("keep_policy"),
            "window_days": dataset.get("window_days"),
            "batch_id": dataset.get("batch_id"),
            "watermark": dataset.get("watermark"),
            "items": [
                {
                    "business_key": item["business_key"],
                    "source_index": item["source_index"],
                    "retained": item["retained"],
                    "duplicates": [
                        {"source_index": record["source_index"], "reason": record["reason"]}
                        for record in item["duplicates"]
                    ],
                }
                for item in dataset.get("items", [])
            ],
        }
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_sort(spec: str, keep_policy: str) -> tuple[str, bool]:
        text = (spec or "").strip()
        if not text:
            return "", True
        parts = re.split(r"[\s,，]+", text)
        direction = ""
        if len(parts) >= 2 and parts[-1].lower() in ("desc", "asc", "降序", "升序"):
            direction = parts[-1].lower()
            parts = parts[:-1]
        column = " ".join(parts).strip()
        if direction in ("asc", "升序"):
            descending = False
        elif direction in ("desc", "降序"):
            descending = True
        else:
            descending = DedupConfigService._keep_newest(keep_policy)
        return column, descending

    @staticmethod
    def _keep_newest(keep_policy: str) -> bool:
        text = (keep_policy or "").strip()
        if any(token in text for token in ("最先", "最早", "首条", "第一条")):
            return False
        return True

    @staticmethod
    def _window_days(value: Any) -> int | None:
        digits = re.sub(r"[^0-9]", "", str(value or ""))
        if not digits:
            return None
        days = int(digits)
        return days if days > 0 else None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    @classmethod
    def _comparable(cls, value: Any) -> tuple[int, float, str]:
        """把排序字段归一成可比较的键；数值最小、字符串居中、时间最大。"""
        text = str(value or "").strip()
        parsed = cls._parse_time(text)
        if parsed is not None:
            try:
                return (2, parsed.timestamp(), "")
            except (OverflowError, OSError, ValueError):
                return (2, 0.0, parsed.isoformat())
        try:
            return (1, float(text), "")
        except ValueError:
            return (0, 0.0, text.casefold())

    def get_configs_for_dataset(self, dataset: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "目标数据集", dataset)
