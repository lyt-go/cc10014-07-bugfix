<template>
  <section class="page" data-module="dedup_config">
    <header class="page-head">
      <div>
        <h2>去重配置管理</h2>
        <p class="page-desc">维护去重配置的输入、执行约束与结果记录，为重复消除提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记去重配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出去重配置清单</button>
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
              :data-testid="`dataset-${row.id}`"
              @click="openDataset(row)"
            >
              查看去重明细数据集
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + datasetColumns.length + 1" class="empty-state">暂无去重配置数据，可先登记去重配置</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记去重配置</h3>
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

    <div v-if="dataset" class="detail-panel" data-testid="dataset-panel">
      <h3>去重明细数据集：{{ dataset.config_no }}（{{ dataset.config_version }}）</h3>
      <p class="page-desc">
        每条业务键只保留一条可追溯记录；被淘汰的重复记录连同原因一起进入明细。
        共 {{ dataset.key_count }} 个业务键、{{ dataset.retained_count }} 条保留记录，
        去重水位 {{ dataset.watermark || '—' }}，窗口起点 {{ dataset.window_start || '—' }}，
        回退 {{ dataset.unkeyed_count }} 行无业务键记录。
      </p>
      <p v-if="dataset.stale" class="error-text" data-testid="dataset-stale">
        配置已升级至 {{ dataset.current_config_version }}，此处仍展示数据集落地时的 {{ dataset.config_version }} 结果；
        刷新数据集后才会切换到新口径。
      </p>
      <div class="page-actions">
        <button class="btn" data-testid="dataset-refresh" type="button" @click="refreshDataset">刷新去重明细数据集</button>
        <label class="filter-item">
          <span>业务键回查</span>
          <input v-model="keyQuery" data-testid="dataset-key-input" placeholder="按业务键回查" @keyup.enter="lookupKey" />
        </label>
        <button class="btn ghost" data-testid="dataset-key-search" type="button" @click="lookupKey">回查业务键</button>
        <button class="btn ghost" data-testid="dataset-close" type="button" @click="dataset = null">关闭</button>
      </div>
      <div v-if="keyDetail" class="detail-panel small" data-testid="dataset-key-detail">
        <h4>业务键 {{ keyDetail.business_key }}：保留 {{ describeRecord(keyDetail.retained) }}</h4>
        <p class="page-desc">
          排序值 {{ keyDetail.sort_value ?? '—' }}，窗口 {{ keyDetail.window }}，
          重复 {{ keyDetail.duplicate_count }} 条{{ keyDetail.conflict ? '，存在相同排序时间冲突' : '' }}。
        </p>
        <ul class="dataset-warnings">
          <li v-for="(record, idx) in keyDetail.duplicates" :key="idx">
            源行 {{ record.source_index + 1 }} · 排序值 {{ record.sort_value ?? '—' }} · {{ record.reason }} ·
            {{ describeRecord(record.record) }}
          </li>
          <li v-if="!keyDetail.duplicates.length">没有重复记录</li>
        </ul>
      </div>
      <table class="data-table small">
        <thead>
          <tr><th>#</th><th>业务键</th><th>排序值</th><th>保留记录</th><th>重复数</th><th>冲突</th><th>窗口</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in dataset.items" :key="item.business_key" :data-testid="`dataset-key-${item.business_key}`">
            <td>{{ item.seq }}</td>
            <td>{{ item.business_key }}</td>
            <td>{{ item.sort_value ?? '—' }}</td>
            <td>{{ describeRecord(item.retained) }}</td>
            <td>{{ item.duplicate_count }}</td>
            <td>{{ item.conflict ? '相同时间' : '—' }}</td>
            <td>{{ item.window }}</td>
          </tr>
          <tr v-if="!dataset.items.length">
            <td colspan="7" class="empty-state">该数据集当前没有业务键记录</td>
          </tr>
        </tbody>
      </table>
      <div v-if="dataset.warnings?.length" class="dataset-warnings" data-testid="dataset-warnings">
        <h4>裁决说明</h4>
        <ul><li v-for="(warning, idx) in dataset.warnings" :key="idx">{{ warning }}</li></ul>
      </div>
      <div class="page-foot">
        <span>第 {{ dataset.page }} / {{ dataset.page_count }} 页，共 {{ dataset.total }} 个业务键</span>
        <span class="row-actions">
          <button class="link" data-testid="dataset-prev" type="button" :disabled="dataset.page <= 1" @click="datasetPage(dataset.page - 1)">上一页</button>
          <input
            v-model="datasetPageInput"
            class="page-input"
            data-testid="dataset-page-input"
            :placeholder="`1-${dataset.page_count}`"
            @keyup.enter="jumpDatasetPage"
          />
          <button class="link" data-testid="dataset-jump" type="button" @click="jumpDatasetPage">跳转</button>
          <button class="link" data-testid="dataset-next" type="button" :disabled="dataset.page >= dataset.page_count" @click="datasetPage(dataset.page + 1)">下一页</button>
        </span>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条去重配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type DatasetKey = {
  seq: number
  business_key: string
  sort_value: string | number | null
  retained: Record<string, unknown>
  duplicate_count: number
  conflict: boolean
  window: string
}

type Dataset = {
  config_id: number
  config_no: string
  config_version: string
  current_config_version: string
  stale: boolean
  watermark: string
  window_start: string
  key_count: number
  retained_count: number
  unkeyed_count: number
  items: DatasetKey[]
  warnings: string[]
  total: number
  page: number
  page_count: number
  size: number
}

const ENDPOINT = '/api/dedup_config'
const columns = ["配置编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值", "配置版本", "目标数据集"]
const datasetColumns = ["去重水位", "业务键数量", "保留记录数", "去重明细数据集版本"]
const actions = ["提交校验", "标记生效", "停用配置"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["去重配置总数", "待校验配置", "已生效配置"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const dataset = ref<Dataset | null>(null)
const datasetConfigId = ref<number | null>(null)
const datasetPageInput = ref('')
const datasetPageSize = 10
const keyQuery = ref('')
const keyDetail = ref<Record<string, any> | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
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
    link.download = 'dedup_config.csv'
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
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '去重配置动作未生效，请稍后重试')
    }
    await reload()
    if (datasetConfigId.value === Number(row.id)) {
      await loadDataset(dataset.value?.page ?? 1)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '去重配置操作失败'
  }
}

function describeRecord(record: Record<string, unknown> | undefined): string {
  if (!record) return '—'
  return Object.entries(record)
    .map(([key, value]) => `${key}=${value ?? ''}`)
    .join('，')
}

async function openDataset(row: Row) {
  datasetConfigId.value = Number(row.id)
  keyDetail.value = null
  keyQuery.value = ''
  await loadDataset(1)
}

async function lookupKey() {
  if (datasetConfigId.value === null || !keyQuery.value.trim()) {
    errorMessage.value = '请先选择去重配置并填写业务键'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/${datasetConfigId.value}/dataset/keys/${encodeURIComponent(keyQuery.value.trim())}`,
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '业务键回查失败')
    keyDetail.value = payload
  } catch (error) {
    keyDetail.value = null
    errorMessage.value = error instanceof Error ? error.message : '业务键回查失败'
  }
}

async function datasetPage(page: number) {
  await loadDataset(page)
}

function jumpDatasetPage() {
  const page = Number(datasetPageInput.value)
  datasetPageInput.value = ''
  if (!Number.isInteger(page) || page < 1) {
    errorMessage.value = '页码需为正整数'
    return
  }
  // 越界页码交给后端裁决，页面按返回的说明展示拒绝原因
  void datasetPage(page)
}

async function loadDataset(page: number) {
  if (datasetConfigId.value === null) return
  errorMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/${datasetConfigId.value}/dataset?page=${page}&size=${datasetPageSize}`,
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '去重明细数据集读取失败')
    dataset.value = payload as Dataset
  } catch (error) {
    dataset.value = null
    errorMessage.value = error instanceof Error ? error.message : '去重明细数据集读取失败'
  }
}

async function refreshDataset() {
  if (datasetConfigId.value === null) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${datasetConfigId.value}/dataset/refresh`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '刷新失败')
    await loadDataset(1)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('去重配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '去重配置列表读取失败'
  }
}

onMounted(reload)
</script>
