<template>
  <section class="page" data-module="dwd_layer">
    <header class="page-head">
      <div>
        <h2>明细层管理管理</h2>
        <p class="page-desc">维护明细层DWD表的输入、执行约束与结果记录，为明细层管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记明细表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出明细层管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in [...columns, ...datasetColumns]" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td v-for="column in datasetColumns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`build-${row.id}`"
              :disabled="buildingId === row.id"
              @click="build(row)"
            >
              {{ buildingId === row.id ? '构建中…' : '构建明细' }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`dedup-${row.id}`"
              @click="openDedup(row)"
            >
              回查去重明细数据集
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + datasetColumns.length + 1" class="empty-state">暂无明细层管理数据，可先登记明细表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记明细表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="dedup" class="detail-panel" data-testid="dedup-panel">
      <h3>去重明细数据集：{{ dedup.config_no }}（{{ dedup.config_version }}）</h3>
      <p class="page-desc">
        每条业务键只保留一条可追溯记录；去重水位 {{ dedup.watermark || '—' }}，
        业务键 {{ dedup.key_count }} 个，保留记录 {{ dedup.retained_count }} 条。
      </p>
      <p v-if="dedup.stale" class="error-text" data-testid="dedup-stale">
        配置已升级至 {{ dedup.current_config_version }}，数据集仍按 {{ dedup.config_version }} 落地结果展示。
      </p>
      <div class="page-actions">
        <button class="btn ghost" data-testid="dedup-close" type="button" @click="dedup = null">关闭</button>
      </div>
      <table class="data-table small">
        <thead>
          <tr><th>#</th><th>业务键</th><th>排序值</th><th>保留记录</th><th>重复数</th><th>冲突</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in dedup.items" :key="item.business_key">
            <td>{{ item.seq }}</td>
            <td>{{ item.business_key }}</td>
            <td>{{ item.sort_value ?? '—' }}</td>
            <td>{{ describeRecord(item.retained) }}</td>
            <td>{{ item.duplicate_count }}</td>
            <td>{{ item.conflict ? '相同时间' : '—' }}</td>
          </tr>
          <tr v-if="!dedup.items.length">
            <td colspan="6" class="empty-state">该明细表尚未生成去重明细数据集，可先执行构建明细</td>
          </tr>
        </tbody>
      </table>
      <div class="page-foot">
        <span>第 {{ dedup.page }} / {{ dedup.page_count }} 页，共 {{ dedup.total }} 个业务键</span>
        <span class="row-actions">
          <button class="link" data-testid="dedup-prev" type="button" :disabled="dedup.page <= 1" @click="dedupPage(dedup.page - 1)">上一页</button>
          <button class="link" data-testid="dedup-next" type="button" :disabled="dedup.page >= dedup.page_count" @click="dedupPage(dedup.page + 1)">下一页</button>
        </span>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条明细层管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type DedupKey = {
  seq: number
  business_key: string
  sort_value: string | number | null
  retained: Record<string, unknown>
  duplicate_count: number
  conflict: boolean
}

type DedupDataset = {
  config_no: string
  config_version: string
  current_config_version: string
  stale: boolean
  watermark: string
  key_count: number
  retained_count: number
  items: DedupKey[]
  total: number
  page: number
  page_count: number
}

const ENDPOINT = '/api/dwd_layer'
const columns = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
const datasetColumns = ["去重水位", "业务键数量", "去重明细数据集版本"]
const actions = ["提交校验", "确认上线", "标记下线"]
const statuses = ["草稿", "待校验", "已上线", "已下线"]
const metricNames = ["明细表总数", "待校验表", "已上线表"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const dedup = ref<DedupDataset | null>(null)
const dedupEntryId = ref<number | null>(null)
const buildingId = ref<number | null>(null)
const dedupPageSize = 10
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dwd_layer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '明细层管理动作未生效，请稍后重试')
    }
    await reload()
    if (dedupEntryId.value === Number(row.id)) await loadDedup(dedup.value?.page ?? 1)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理操作失败'
  }
}

function describeRecord(record: Record<string, unknown> | undefined): string {
  if (!record) return '—'
  return Object.entries(record)
    .map(([key, value]) => `${key}=${value ?? ''}`)
    .join('，')
}

async function build(row: Row) {
  buildingId.value = Number(row.id)
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/build`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '构建失败')
    await reload()
    if (dedupEntryId.value === Number(row.id)) await loadDedup(dedup.value?.page ?? 1)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '构建失败'
  } finally {
    buildingId.value = null
  }
}

async function openDedup(row: Row) {
  dedupEntryId.value = Number(row.id)
  await loadDedup(1)
}

async function dedupPage(page: number) {
  await loadDedup(page)
}

async function loadDedup(page: number) {
  if (dedupEntryId.value === null) return
  errorMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/${dedupEntryId.value}/dedup?page=${page}&size=${dedupPageSize}`,
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '去重明细数据集读取失败')
    dedup.value = payload as DedupDataset
  } catch (error) {
    dedup.value = null
    errorMessage.value = error instanceof Error ? error.message : '去重明细数据集读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('明细表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理列表读取失败'
  }
}

onMounted(reload)
</script>
