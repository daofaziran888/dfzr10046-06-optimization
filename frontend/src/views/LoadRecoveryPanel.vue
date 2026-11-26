<template>
  <section
    class="recovery-panel"
    data-testid="load-recovery-panel"
    data-module="load_window_recovery"
  >
    <header class="recovery-head">
      <div>
        <h3>连续失败门槛与跨模块转交</h3>
        <p>
          窗口和事实表就是阈值统计范围：其他窗口的失败不可计入。转交判定使用班组实际设置的上限。
          当前班组：<strong data-testid="team-name">{{ state?.team ?? '数据工程组' }}</strong>，
          班组设定门槛：<strong data-testid="threshold-value">{{ currentThreshold }}</strong> 次。
          未达门槛仍兼容既有单笔确认装载；转交成功后生成重跑待办，接班人才能排期。
        </p>
      </div>
      <div class="policy-editor">
        <label>
          班组上限
          <input
            v-model.number="thresholdInput"
            data-testid="threshold-input"
            type="number"
            min="1"
            max="20"
          />
        </label>
        <button class="btn" data-testid="save-threshold" type="button" @click="saveThreshold">
          保存班组设置
        </button>
        <button class="btn ghost" data-testid="prepare-demo" type="button" @click="prepareDemo">
          准备门槛三次演示
        </button>
      </div>
    </header>

    <div class="recovery-grid">
      <form class="recovery-form" @submit.prevent>
        <label class="full">
          选择装载窗口
          <select
            v-model="selectedCode"
            data-testid="recovery-window-select"
            @change="preferredPk = null"
          >
            <option value="">请选择窗口</option>
            <option
              v-for="row in windowOptions"
              :key="String(row.id)"
              :value="String(row['窗口编号'] ?? '')"
            >
              {{ row['窗口编号'] }}｜{{ row['所属事实表'] }}｜{{ row.status }}
            </option>
          </select>
        </label>

        <div class="scope-box" data-testid="recovery-scope">
          <div>事实表：<strong>{{ selectedRow?.['所属事实表'] ?? '—' }}</strong></div>
          <div>窗口：<strong>{{ selectedCode || '—' }}</strong></div>
          <div>
            连续失败：
            <strong data-testid="failure-count">{{ scopeFailureCount }}</strong>
            / {{ currentThreshold }}
          </div>
          <div>其他窗口失败已排除：<strong>{{ persistedOtherExcluded }}</strong> 次</div>
        </div>

        <label>
          本次实际行数
          <input v-model.number="actualRows" data-testid="actual-rows-input" type="number" />
        </label>
        <label>
          值班员
          <input v-model="operator" data-testid="operator-input" />
        </label>
        <label>
          接班人
          <input v-model="successor" data-testid="successor-input" />
        </label>
        <label>
          并发排期员
          <input v-model="scheduler" data-testid="scheduler-input" />
        </label>

        <div class="button-row full">
          <button
            class="btn"
            data-testid="record-failure"
            type="button"
            :disabled="busy || !selectedCode"
            @click="recordFailure"
          >
            记录一次确认失败
          </button>
          <button
            class="btn primary"
            data-testid="confirm-load"
            type="button"
            :disabled="busy || !selectedCode"
            @click="confirmLoad"
          >
            确认装载
          </button>
          <button
            class="btn"
            data-testid="confirm-recovery"
            type="button"
            :disabled="busy || !selectedCode"
            @click="confirmRecovery"
          >
            纠正后确认成功
          </button>
          <button
            class="btn primary"
            data-testid="submit-handoff"
            type="button"
            :disabled="busy || !selectedCode || !scopeThresholdReached"
            :title="scopeThresholdReached ? '提交跨模块转交' : `连续失败达到 ${currentThreshold} 次后可转交`"
            @click="submitHandoff"
          >
            发起跨模块转交
          </button>
          <button
            class="btn"
            data-testid="race-handoff"
            type="button"
            :disabled="busy || !selectedCode"
            @click="runRace"
          >
            第三次失败与他人并发排期
          </button>
        </div>

        <div class="demo-row full" data-testid="demo-sequence-panel">
          <span>门槛三次演示：</span>
          <button
            class="btn ghost"
            data-testid="demo-recover-sequence"
            type="button"
            :disabled="busy"
            @click="runDemo('recover')"
          >
            二次失败仍可正常确认
          </button>
          <button
            class="btn ghost"
            data-testid="demo-handoff-sequence"
            type="button"
            :disabled="busy"
            @click="runDemo('handoff')"
          >
            第三次失败出现转交
          </button>
          <button
            class="btn ghost"
            data-testid="demo-race-sequence"
            type="button"
            :disabled="busy"
            @click="runDemo('race')"
          >
            并发排期只确认一条任务
          </button>
        </div>
        <p v-if="actionError" class="error-text full" data-testid="recovery-error">{{ actionError }}</p>
      </form>

      <div class="receipt-column">
        <article
          v-if="displayReceipt"
          class="receipt-card"
          data-testid="recovery-receipt"
          aria-live="polite"
        >
          <div data-testid="handoff-receipt">
            <h4>
              同一份转交判定和回执
              <span data-testid="receipt-id">{{ displayReceipt.receipt_id }}</span>
            </h4>
            <p
              v-if="displayReceipt['第三次失败已接收']"
              class="accepted-badge"
              data-testid="third-failure-accepted"
            >
              第三次失败已被接收
            </p>
            <p class="receipt-message" data-testid="receipt-message">{{ displayReceipt.message }}</p>
            <dl class="receipt-grid">
              <div>
                <dt>统计范围</dt>
                <dd data-testid="receipt-scope">
                  {{ displayReceipt.fact_table }} / {{ displayReceipt.window_code }}
                </dd>
              </div>
              <div>
                <dt>门槛计数</dt>
                <dd data-testid="receipt-count">
                  {{ displayReceipt.failure_count }}/{{ displayReceipt.threshold }}
                  ；其他窗口 {{ displayReceipt.other_window_failure_count_excluded }} 次不计入
                </dd>
              </div>
              <div>
                <dt>值班员还能否确认装载</dt>
                <dd data-testid="receipt-can-confirm">
                  {{ displayReceipt.can_confirm_load ? '能' : '不能' }}
                  <span class="hint">（{{ displayReceipt['还能否确认装载'] }}）</span>
                </dd>
              </div>
              <div>
                <dt>窗口恢复后能否继续推进</dt>
                <dd data-testid="receipt-can-continue">{{ displayReceipt['可否继续推进'] }}</dd>
              </div>
              <div>
                <dt>排期员手里的待办是否继续有效</dt>
                <dd data-testid="receipt-task-valid">
                  <template v-if="displayReceipt.task?.status === '已完成'">
                    重跑已完成，任务回执保留
                  </template>
                  <template v-else-if="displayReceipt.task && displayReceipt['排期员待办有效']">
                    现有任务已并入本判定：待办有效；若已开始，重跑继续
                  </template>
                  <template v-else>尚未生成有效待办</template>
                </dd>
              </div>
              <div>
                <dt>任务与责任人</dt>
                <dd data-testid="receipt-task-owner">
                  <template v-if="displayReceipt.task">
                    <span data-testid="receipt-task-id">{{ displayReceipt.task.task_id }}</span>
                    ｜状态：{{ displayReceipt.task.status }}
                    ｜责任人：<strong>{{ displayReceipt.task['责任人'] }}</strong>
                    <template v-if="displayReceipt.task['排期人']">
                      ｜排期人：{{ displayReceipt.task['排期人'] }}
                    </template>
                  </template>
                  <template v-else>—</template>
                </dd>
              </div>
              <div v-if="displayReceipt.only_one_valid_task !== undefined">
                <dt>并发收敛</dt>
                <dd data-testid="receipt-race-count">
                  有效任务数：{{ displayReceipt.valid_task_count }}；
                  {{ displayReceipt.only_one_valid_task ? '只确认一条有效任务' : '存在多条任务' }}
                </dd>
              </div>
            </dl>
            <p class="conflict-note" data-testid="receipt-conflict">
              {{ displayReceipt['冲突结论'] || displayReceipt.next_step }}
            </p>
            <RouterLink class="btn" to="/reprocess_task" data-testid="goto-reprocess">
              前往重跑任务排期
            </RouterLink>
          </div>
        </article>
        <div v-else class="receipt-empty" data-testid="receipt-empty">
          操作后在此显示同一份转交判定和回执；关页再进也会恢复最近判定。
        </div>
      </div>
    </div>

    <div class="recovery-bottom">
      <div class="ledger-card">
        <h4>跨模块转交判定（关页再进可查谁接手）</h4>
        <table class="data-table" data-testid="handoff-list">
          <thead>
            <tr>
              <th>转交编号</th>
              <th>窗口/事实表</th>
              <th>失败/门槛</th>
              <th>值班员</th>
              <th>接手人</th>
              <th>重跑任务</th>
              <th>责任人/状态</th>
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in handoffs" :key="String(item['转交编号'])">
              <td>{{ item['转交编号'] }}</td>
              <td>{{ item.window_code }}<br />{{ item.fact_table }}</td>
              <td>{{ item.failure_count }}/{{ item.threshold }}</td>
              <td>{{ item['值班员'] }}</td>
              <td data-testid="handoff-owner">{{ item['责任人'] || item.task_owner || item['接班人'] }}</td>
              <td>{{ item.task_id }}</td>
              <td data-testid="handoff-task-owner">{{ item['责任人'] }}<br />{{ item.task_status }}</td>
              <td>{{ item.deduplicated ? '复用现有任务和责任人' : '生成新的重跑待办' }}</td>
            </tr>
            <tr v-if="!handoffs.length">
              <td colspan="8" class="empty-state">尚未发起转交</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="ledger-card">
        <h4>窗口推进状态（哪些窗口还能继续）</h4>
        <table class="data-table" data-testid="window-continuity-list">
          <thead>
            <tr>
              <th>窗口</th>
              <th>事实表</th>
              <th>状态</th>
              <th>连续失败</th>
              <th>接手人</th>
              <th>能否继续推进</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in continuityWindows" :key="`${item['窗口编号']}-${item.id}`">
              <td>{{ item['窗口编号'] }}</td>
              <td>{{ item['所属事实表'] }}</td>
              <td>{{ item.status }}</td>
              <td>{{ item['连续失败次数'] ?? 0 }}/{{ item['班组门槛'] ?? currentThreshold }}</td>
              <td data-testid="continuity-owner">{{ item['接手人'] || '本班值班员' }}</td>
              <td data-testid="continuity-result">{{ item['可否继续推进'] || defaultContinuity(item) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { recoveryPost, recoveryState } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type AnyRecord = Record<string, any>

const props = defineProps<{ rows: EntryRow[] }>()
const emit = defineEmits<{ (event: 'changed'): void }>()

const state = ref<AnyRecord | null>(null)
const selectedCode = ref('')
const preferredPk = ref<number | string | null>(null)
const actualRows = ref<number>(500)
const operator = ref('林岚（值班员）')
const successor = ref('陈澈（接班人）')
const scheduler = ref('周敏（排期员）')
const thresholdInput = ref(3)
const receipt = ref<AnyRecord | null>(null)
const actionError = ref('')
const busy = ref(false)

const windowOptions = computed(() => props.rows)
const currentThreshold = computed(() => Number(state.value?.threshold ?? thresholdInput.value ?? 3))
const selectedRow = computed(() =>
  props.rows.find((row) => String(row['窗口编号'] ?? '') === selectedCode.value) ?? null,
)
const selectedScope = computed(() =>
  (state.value?.scopes ?? []).find((item: AnyRecord) => item.window_code === selectedCode.value),
)
const scopeFailureCount = computed(() =>
  Number(selectedScope.value?.consecutive_failures ?? selectedRow.value?.['连续失败次数'] ?? 0),
)
const scopeThresholdReached = computed(() =>
  Boolean(selectedScope.value?.threshold_reached ?? selectedRow.value?.['门槛已达'] ?? scopeFailureCount.value >= currentThreshold.value),
)
const persistedReceipt = computed<AnyRecord | null>(() => {
  if (receipt.value) return receipt.value
  const receipts = (state.value?.receipts ?? []) as AnyRecord[]
  for (let index = receipts.length - 1; index >= 0; index -= 1) {
    if (receipts[index]?.window_code === selectedCode.value) return receipts[index]
  }
  return (selectedScope.value?.latest_receipt as AnyRecord | undefined) ?? null
})
const displayReceipt = computed(() => persistedReceipt.value)
const persistedOtherExcluded = computed(() => {
  const receiptForScope = persistedReceipt.value
  if (receiptForScope) return Number(receiptForScope.other_window_failure_count_excluded ?? 0)
  const events = (state.value?.events ?? []) as AnyRecord[]
  const fact = selectedRow.value?.['所属事实表']
  return events.filter(
    (event) => event.outcome === 'failure'
      && event.fact_table === fact
      && event.window_code !== selectedCode.value,
  ).length
})
const handoffs = computed<AnyRecord[]>(() =>
  [...((state.value?.handoffs ?? []) as AnyRecord[])].reverse(),
)
const continuityWindows = computed<AnyRecord[]>(() =>
  ((state.value?.windows ?? []) as AnyRecord[]).filter((item) => item['窗口编号']),
)

function chooseDefaultWindow() {
  if (!props.rows.length) return
  if (selectedCode.value && props.rows.some(
    (row) => String(row['窗口编号'] ?? '') === selectedCode.value,
  )) {
    return
  }

  const handoffs = (state.value?.handoffs ?? []) as AnyRecord[]
  const latestHandoffCode = handoffs.length
    ? String(handoffs[handoffs.length - 1]?.window_code ?? '')
    : ''
  const latestHandoffRow = latestHandoffCode
    ? props.rows.find((row) => String(row['窗口编号']) === latestHandoffCode)
    : undefined
  const handedRow = latestHandoffRow
    ?? [...props.rows].reverse().find((row) => row['转交单编号'])
  const abnormal = props.rows.find((row) => row.abnormal || row.status === '装载中断')
  selectedCode.value = String(
    handedRow?.['窗口编号'] ?? abnormal?.['窗口编号'] ?? props.rows[0]?.['窗口编号'] ?? '',
  )
}

watch(
  () => props.rows,
  () => chooseDefaultWindow(),
  { immediate: true },
)

watch(selectedCode, () => {
  receipt.value = null
  actionError.value = ''
  const expected = Number(selectedRow.value?.['预期行数'] ?? 1000)
  if (selectedRow.value && Number(selectedRow.value['实际行数']) > 0) {
    actualRows.value = Number(selectedRow.value['实际行数'])
  } else {
    actualRows.value = expected ? Math.round(expected / 2) : 500
  }
})

async function refreshState() {
  state.value = await recoveryState<AnyRecord>()
  thresholdInput.value = currentThreshold.value
  chooseDefaultWindow()
}

async function runPost(path: string, body: Record<string, unknown>) {
  busy.value = true
  actionError.value = ''
  try {
    const data = await recoveryPost<AnyRecord>(path, body)
    if (data.receipt) receipt.value = data.receipt
    if (data.ok === false && !data.receipt?.threshold_reached) {
      actionError.value = String(data.message ?? '操作未成功')
    }
    await refreshState()
    emit('changed')
    return data
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '操作失败'
    return null
  } finally {
    busy.value = false
  }
}

function scopeBody(extra: Record<string, unknown> = {}) {
  return {
    window_pk: preferredPk.value ?? selectedRow.value?.id,
    window_code: selectedCode.value,
    fact_table: selectedRow.value?.['所属事实表'],
    operator: operator.value,
    successor: successor.value,
    scheduler: scheduler.value,
    ...extra,
  }
}

async function saveThreshold() {
  await runPost('policy', {
    team: state.value?.team ?? '数据工程组',
    failure_threshold: Number(thresholdInput.value),
  })
}

async function prepareDemo() {
  const data = await runPost('prepare-demo', {})
  if (data?.ok) {
    preferredPk.value = null
    selectedCode.value = 'LW-DEMO-HANDOFF'
  }
}

async function recordFailure() {
  await runPost('attempt', scopeBody({
    outcome: 'failure',
    actual_rows: Number(actualRows.value),
  }))
}

async function confirmLoad() {
  await runPost('attempt', scopeBody({
    outcome: 'auto',
    actual_rows: Number(actualRows.value),
  }))
}

async function confirmRecovery() {
  const expected = Number(selectedRow.value?.['预期行数'] ?? 1000)
  actualRows.value = expected
  await runPost('attempt', scopeBody({
    outcome: 'success',
    actual_rows: expected,
  }))
}

async function submitHandoff() {
  await runPost('handoff', scopeBody())
}

async function runRace() {
  // 按钮表达的是“第三次失败 + 并发排期”；若值班员尚未点满失败次数，先补齐到门槛。
  const missing = Math.max(0, currentThreshold.value - scopeFailureCount.value)
  for (let index = 0; index < missing; index += 1) {
    await runPost('attempt', scopeBody({
      outcome: 'failure',
      actual_rows: Number(actualRows.value),
    }))
  }
  await runPost('race', scopeBody())
}

async function runDemo(kind: 'recover' | 'handoff' | 'race') {
  const data = await runPost('demo-sequence', { kind })
  preferredPk.value = null
  const code = data?.receipt?.window_code
    ?? (kind === 'recover' ? 'LW-DEMO-RECOVER' : kind === 'race' ? 'LW-DEMO-RACE' : 'LW-DEMO-HANDOFF')
  selectedCode.value = String(code)
}

function defaultContinuity(item: AnyRecord): string {
  if (item.status === '已装载') return '可以继续推进'
  if (item['门槛已达'] || item['转交单编号']) return '需接手/恢复后推进'
  return '纠正后可继续'
}

defineExpose({
  async ingestActionResult(result: AnyRecord) {
    if (result?.receipt) {
      receipt.value = result.receipt
      selectedCode.value = String(result.receipt.window_code ?? selectedCode.value)
      preferredPk.value = result.entry?.id ?? result.receipt.entry_id ?? null
    }
    await refreshState()
  },
  refreshState,
})

onMounted(refreshState)
</script>

<style scoped>
.recovery-panel {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  margin: 12px 0 16px;
}
.recovery-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.recovery-head h3 { margin: 0 0 6px; font-size: 17px; }
.recovery-head p { margin: 0; color: var(--muted); font-size: 13px; max-width: 760px; }
.policy-editor { display: flex; gap: 8px; align-items: end; flex-shrink: 0; }
.policy-editor label, .recovery-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.policy-editor input, .recovery-form input, .recovery-form select {
  border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; font-size: 13px; min-width: 130px;
}
.recovery-grid { display: grid; grid-template-columns: minmax(360px, 0.9fr) minmax(420px, 1.1fr); gap: 12px; margin-top: 12px; }
.recovery-form { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.full { grid-column: 1 / -1; }
.scope-box { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; background: #eef6ff; border: 1px solid #cfe3ff; border-radius: 8px; padding: 10px; font-size: 13px; }
.button-row { display: flex; flex-wrap: wrap; gap: 8px; }
.demo-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-top: 8px; border-top: 1px dashed var(--border); font-size: 13px; color: var(--muted); }
.receipt-card { background: #fff; border: 1px solid #b7d4ff; border-left: 4px solid var(--brand); border-radius: 8px; padding: 12px; }
.receipt-card h4 { margin: 0 0 8px; display: flex; justify-content: space-between; gap: 8px; }
.receipt-message { font-weight: 600; font-size: 13px; margin: 0 0 10px; }
.accepted-badge { display: inline-block; margin: 0 0 8px; padding: 3px 8px; border-radius: 999px; background: #fef3c7; color: #92400e; font-size: 12px; font-weight: 700; }
.receipt-grid { display: grid; grid-template-columns: 1fr; gap: 8px; margin: 0; }
.receipt-grid dt { font-size: 12px; color: var(--muted); }
.receipt-grid dd { margin: 2px 0 0; font-size: 13px; }
.hint { color: var(--muted); font-weight: 400; }
.conflict-note { background: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; padding: 8px; font-size: 13px; }
.receipt-empty { min-height: 180px; display: grid; place-items: center; color: var(--muted); font-size: 13px; border: 1px dashed var(--border); border-radius: 8px; padding: 16px; text-align: center; }
.recovery-bottom { display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }
.ledger-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px; overflow-x: auto; }
.ledger-card h4 { margin: 0 0 8px; font-size: 14px; }
.error-text { margin: 0; }
@media (max-width: 1100px) { .recovery-grid { grid-template-columns: 1fr; } .recovery-head { flex-direction: column; } }
</style>
