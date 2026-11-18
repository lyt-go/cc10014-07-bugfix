"""测试2：清洗+去重→DWD表链路。

验证：
- DWD服务能消费ODS数据
- 清洗规则应用到数据行
- 去重规则按业务键去重
- 结果写入DWD模拟表并生成血缘
"""
from __future__ import annotations

from app.store import store
from app.services.dwd_layer import DwdLayerService
from app.services.cleanse_rule import CleanseRuleService
from app.services.dedup_config import DedupConfigService


def reset_store():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._dedup_datasets.clear()


class TestCleanseDedupDwd:
    def setup_method(self):
        reset_store()

    def test_cleanse_applies_regex(self):
        """清洗规则应将不符合正则的值替换为默认值。"""
        cs = CleanseRuleService()
        data = [
            {"order_id": "O001", "order_amount": "100.50"},
            {"order_id": "O002", "order_amount": "-10.00"},
            {"order_id": "O003", "order_amount": ""},
        ]
        result = cs.apply_cleanse(1, data)
        assert result[0]["order_amount"] == "100.50"
        assert result[1]["order_amount"] == "0.00"
        assert result[2]["order_amount"] == "0.00"

    def test_dedup_removes_duplicates(self):
        """去重规则应按业务键去重保留最新。"""
        ds = DedupConfigService()
        data = [
            {"order_id": "O001", "order_amount": "100"},
            {"order_id": "O001", "order_amount": "200"},
            {"order_id": "O002", "order_amount": "50"},
        ]
        result = ds.apply_dedup(1, data)
        assert len(result) == 2
        keys = {r["order_id"] for r in result}
        assert keys == {"O001", "O002"}

    def test_dwd_build_from_ods(self):
        """DWD构建应消费ODS数据并生成模拟表和血缘。"""
        dwd = DwdLayerService()
        # 先给ODS表注入数据
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "order_amount": "100.50", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O002", "order_amount": "200.00", "order_status": "待支付", "dt": "2026-11-01"},
            {"order_id": "O001", "order_amount": "150.00", "order_status": "已完成", "dt": "2026-11-01"},
        ])
        result = dwd.build_dwd_from_ods(1)
        assert result["ok"] is True
        dwd_data = store.table_rows("dwd_order_detail")
        assert len(dwd_data) >= 1
        # 验证血缘已生成
        edges = store.get_lineage_edges()
        assert any(e["上游表名"] == "ods_orders" and e["下游表名"] == "dwd_order_detail" for e in edges)


class TestDedupDataset:
    """去重明细数据集：业务键唯一、可追溯、分页越界拒绝、重跑幂等。"""

    def setup_method(self):
        reset_store()

    def _seed_duplicates(self):
        rows = [
            {"order_id": "O001", "order_amount": "100", "dt": "2026-11-01"},
            {"order_id": "O001", "order_amount": "200", "dt": "2026-11-03"},
            {"order_id": "O001", "order_amount": "150", "dt": "2026-11-03"},
            {"order_id": "O002", "order_amount": "50", "dt": "2026-11-02"},
            {"order_id": "O003", "order_amount": "70", "dt": "2026-11-02"},
        ]
        store.set_table_rows("ods_orders", rows)
        return rows

    def test_dataset_keeps_one_traceable_record_per_key(self):
        """每条业务键只保留一条，被淘汰的重复记录带原因进入明细。"""
        ds = DedupConfigService()
        kept, dataset = ds.deduplicate(1, self._seed_duplicates(), batch_id="RAW-1")
        assert len(kept) == 3
        assert dataset["key_count"] == len({row["order_id"] for row in kept}) == 3
        assert dataset["retained_count"] == 3
        assert dataset["dropped_count"] == 2
        item = ds.business_key_detail(1, "O001")
        assert item["retained"]["dt"] == "2026-11-03"
        assert sorted(item["retained"].items()) == sorted({"order_id": "O001", "order_amount": "200", "dt": "2026-11-03"}.items())
        assert [record["sort_value"] for record in item["duplicates"]] == ["2026-11-01", "2026-11-03"]
        assert {record["reason"] for record in item["duplicates"]} == {"相同排序时间", "保留策略淘汰"}

    def test_same_sort_time_yields_stable_tie(self):
        """排序时间相同的重复记录必须产生稳定终态。"""
        ds = DedupConfigService()
        rows = self._seed_duplicates()
        first, _ = ds.deduplicate(1, list(rows), batch_id="RAW-1")
        again, _ = ds.deduplicate(1, list(reversed(rows)), batch_id="RAW-1")
        # 输入顺序变化后保留记录与业务键集合都不变，终态稳定
        assert [row["order_id"] for row in first] == ["O001", "O002", "O003"]
        assert first == again
        assert {row["order_id"] for row in again} == {"O001", "O002", "O003"}
        item = ds.business_key_detail(1, "O001")
        assert item["conflict"] is True
        assert any("相同排序时间" in warning for warning in item["warnings"])

    def test_rerun_keeps_dataset_version_and_watermark(self):
        """重复运行同一批次后业务键数量、保留记录与去重水位均不变。"""
        ds = DedupConfigService()
        rows = self._seed_duplicates()
        _, first = ds.deduplicate(1, list(rows), batch_id="RAW-1")
        _, second = ds.deduplicate(1, list(rows), batch_id="RAW-1")
        assert second["dataset_version"] == first["dataset_version"] == 1
        assert second["watermark"] == first["watermark"]
        assert second["key_count"] == first["key_count"]
        assert [item["retained"] for item in second["items"]] == [item["retained"] for item in first["items"]]

    def test_dataset_page_out_of_range_rejected(self):
        """页码越界时拒绝请求，返回可读说明。"""
        import pytest
        from app.services.dedup_config import DatasetPageOutOfRange

        ds = DedupConfigService()
        ds.deduplicate(1, self._seed_duplicates(), batch_id="RAW-1")
        assert ds.dataset_page(1, page=2, size=2)["page_count"] == 2
        with pytest.raises(DatasetPageOutOfRange):
            ds.dataset_page(1, page=3, size=2)
        with pytest.raises(DatasetPageOutOfRange):
            ds.dataset_page(1, page=0, size=2)

    def test_dataset_survives_config_upgrade_with_original_values(self):
        """配置升级后数据集仍按原版本展示，只标记过期。"""
        ds = DedupConfigService()
        ds.deduplicate(1, self._seed_duplicates(), batch_id="RAW-1")
        entry, message = ds.update_dedup_version(1, {"时间窗口": "7"})
        assert entry["配置版本"] == "v2", message
        page = ds.dataset_page(1, page=1, size=20)
        assert page["stale"] is True
        assert page["config_version"] == "v1"
        assert page["window_days"] == 30
        assert page["watermark"] == "2026-11-03"

    def test_status_actions_step_only(self):
        """去重配置只允许按顺序推进，不回退也不跳级。"""
        ds = DedupConfigService()
        assert ds.run_action(3, "标记生效")[0] is None
        entry, message = ds.run_action(3, "提交校验")
        assert entry is not None and entry["status"] == "待校验", message
        entry, message = ds.run_action(3, "标记生效")
        assert entry is not None and entry["status"] == "已生效", message
        entry, message = ds.run_action(3, "停用配置")
        assert entry is not None and entry["status"] == "已停用", message
        assert ds.run_action(3, "提交校验")[0] is None

    def test_dwd_build_persists_dataset_summary(self):
        """构建明细表时落数据集，列表与详情读到同一份业务键结果。"""
        from app.services.dwd_layer import DwdLayerService

        ds = DedupConfigService()
        rows = self._seed_duplicates()
        store.set_table_rows("ods_orders", rows)
        dwd = DwdLayerService()
        result = dwd.build_dwd_from_ods(1)
        assert result["ok"] is True
        assert result["dedup_dataset"]["key_count"] == 3

        config = ds.dataset_summary(ds.get_entry(1))
        assert config["业务键数量"] == 3
        assert config["去重水位"] == "2026-11-03"

        detail = dwd.get_entry_detail(1)
        assert detail["业务键数量"] == 3
        assert detail["去重配置"] == "DED-ORD-001"
        assert store.table_rows("dwd_order_detail") and len(store.table_rows("dwd_order_detail")) == 3

    def test_dwd_build_is_repeatable(self):
        """重复构建同一批次，明细行数、业务键数量与去重水位都不变。"""
        from app.services.dwd_layer import DwdLayerService

        store.set_table_rows("ods_orders", self._seed_duplicates())
        dwd = DwdLayerService()
        first = dwd.build_dwd_from_ods(1)
        snapshot = [dict(row) for row in store.table_rows("dwd_order_detail")]
        second = dwd.build_dwd_from_ods(1)
        assert second["dedup_dataset"] == first["dedup_dataset"]
        assert store.table_rows("dwd_order_detail") == snapshot

    def test_keep_first_policy(self):
        """保留策略为保留最先时，取最早一条并保留其余记录进明细。"""
        ds = DedupConfigService()
        ds.update_dedup_version(1, {"保留策略": "保留最先"})
        kept, dataset = ds.deduplicate(1, self._seed_duplicates(), batch_id="RAW-1")
        item = ds.business_key_detail(1, "O001")
        assert item["retained"]["dt"] == "2026-11-01"
        assert item["duplicate_count"] == 2
        assert len(kept) == 3

    def test_cross_window_duplicate_reason(self):
        """时间窗口外的重复记录以跨窗口到达记入明细。"""
        ds = DedupConfigService()
        rows = [
            {"order_id": "O1", "dt": "2026-09-01"},
            {"order_id": "O1", "dt": "2026-11-10"},
            {"order_id": "O2", "dt": "2026-11-10"},
        ]
        _, dataset = ds.deduplicate(1, rows, batch_id="RAW-1")
        item = dataset["items"][0]
        assert item["retained"]["dt"] == "2026-11-10"
        assert item["cross_window"] is True
        assert item["duplicates"][0]["reason"] == "跨窗口到达"
        assert dataset["cross_window_count"] == 1
        assert any("跨窗口到达" in warning for warning in item["warnings"])
