<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <LoadRecoveryPanel
      v-if="meta.key === 'load_window'"
      ref="recoveryPanel"
      :rows="rows"
      @changed="reload"
    />

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload()">
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
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th v-if="meta.key === 'load_window'">失败恢复/接手</th>
          <th v-if="meta.key === 'reprocess_task'">责任人/来源窗口</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td v-if="meta.key === 'load_window'" data-testid="row-recovery" class="recovery-cell">
            <span data-testid="row-failure-count">
              连续失败 {{ row['连续失败次数'] ?? 0 }}/{{ row['班组门槛'] ?? 3 }}
            </span>
            <span v-if="row['接手人']" data-testid="row-owner">接手：{{ row['接手人'] }}</span>
            <span v-if="row['重跑任务编号']" data-testid="row-task">任务：{{ row['重跑任务编号'] }}</span>
            <span :data-testid="`row-continue-${row.id}`">{{ row['可否继续推进'] ?? '未达门槛可继续' }}</span>
          </td>
          <td v-if="meta.key === 'reprocess_task'" data-testid="row-owner-scope" class="recovery-cell">
            <span data-testid="reprocess-owner">责任人：{{ row['责任人'] || '待接班人认领' }}</span>
            <span>来源：{{ row['来源窗口'] || row['依赖批次'] || '—' }}</span>
            <span v-if="row['排期人']">排期人：{{ row['排期人'] }}</span>
            <span v-if="row['有效'] === false" class="error-text">已合并/无效：{{ row['重复合并到'] }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'load_window'"
              class="link handoff-link"
              type="button"
              :data-testid="`action-${row.id}-发起转交`"
              @click="runAction('发起转交', row)"
            >
              发起转交
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td
            :colspan="meta.fields.length + 2 + (meta.key === 'load_window' || meta.key === 'reprocess_task' ? 1 : 0)"
            class="empty-state"
          >
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
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

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="noticeMessage" class="success-text" data-testid="action-message">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import LoadRecoveryPanel from './LoadRecoveryPanel.vue'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const recoveryPanel = ref<InstanceType<typeof LoadRecoveryPanel> | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const context = meta.key === 'load_window'
      ? {
          operator: '林岚（值班员）',
          successor: '陈澈（接班人）',
          scheduler: '周敏（排期员）',
        }
      : meta.key === 'reprocess_task'
        ? { scheduler: '陈澈（接班人）', operator: '陈澈（接班人）' }
        : {}
    const result = await applyAction(meta.key, Number(row.id), action, context)
    if (result.ok) {
      noticeMessage.value = result.message
    } else {
      // 领域失败（例如连续确认失败）也已被后端接收并生成回执，需要刷新计数。
      errorMessage.value = result.message
    }
    await reload(true)
    if (meta.key === 'load_window') {
      await recoveryPanel.value?.ingestActionResult(result)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
    await reload(true)
  }
}

async function reload(preserveError = false) {
  if (!preserveError) errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
