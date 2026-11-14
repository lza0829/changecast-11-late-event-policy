<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getJSON, postJSON, putJSON } from '../api'

const streams = ref([])
const streamId = ref(null)
const state = ref(null)
const err = ref('')
const busy = ref(false)
const lastRun = ref(null)

const manual = ref({ event_id: 'late-manual-1', entity_key: '', op: 'upsert', seq: '', payload: '' })

const stream = computed(() => state.value?.stream || null)
const nextWave = computed(() => state.value?.waves.find((w) => w.status === 'pending') || null)
const pendingLate = computed(() => {
  const s = state.value
  if (!s || !nextWave.value) return []
  const wm = s.stream.watermark_seq
  return nextWave.value.events.filter((e) => e.seq <= wm)
})

function lateLaneLabel(batch) {
  if (!batch) return '迟到批'
  return batch.late_policy === 'reject' ? '迟到拒绝批' : '迟到旁路批'
}

const entitySeqs = computed(() => {
  const map = {}
  for (const row of state.value?.projection || []) map[row.entity_key] = row
  return map
})

async function refresh() {
  if (streamId.value == null) return
  state.value = await getJSON(`/api/streams/${streamId.value}/state`)
  if (!manual.value.entity_key && state.value.projection.length) {
    manual.value.entity_key = state.value.projection[0].entity_key
  }
}

async function guard(fn) {
  err.value = ''
  busy.value = true
  try {
    await fn()
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function runNextWave() {
  guard(async () => {
    lastRun.value = await postJSON('/api/project', {
      stream_id: Number(streamId.value),
      note: `演示：投递第 ${nextWave.value?.wave} 波`,
    })
  })
}

function switchPolicy(policy) {
  if (!stream.value || stream.value.late_policy === policy) return
  guard(async () => {
    await putJSON(`/api/streams/${streamId.value}/late-policy`, { late_policy: policy })
  })
}

function reset() {
  guard(async () => {
    lastRun.value = null
    await postJSON(`/api/streams/${streamId.value}/reset`)
  })
}

function dropLate() {
  guard(async () => {
    let payload = null
    if (manual.value.payload.trim()) {
      try {
        payload = JSON.parse(manual.value.payload)
      } catch {
        throw new Error('载荷不是合法 JSON')
      }
    }
    lastRun.value = await postJSON('/api/late-drop', {
      stream_id: Number(streamId.value),
      events: [
        {
          event_id: manual.value.event_id,
          entity_key: manual.value.entity_key,
          op: manual.value.op,
          seq: Number(manual.value.seq),
          payload,
        },
      ],
      note: '演示：手工投递迟到事件',
    })
  })
}

function fillFromLate(event) {
  manual.value = {
    event_id: `late-${event.event_id}-retry`,
    entity_key: event.entity_key,
    op: event.op,
    seq: String(event.seq),
    payload: '',
  }
}

onMounted(async () => {
  try {
    streams.value = await getJSON('/api/streams')
    if (streams.value.length) {
      streamId.value = streams.value[0].id
      await refresh()
    }
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(streamId, async () => {
  lastRun.value = null
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div class="page">
    <h1>迟到事件策略</h1>
    <p class="muted">
      水位已推过之后再来的小 seq 事件就是迟到。它既不混进普通增量，也不静默丢弃：
      两种互斥策略，一种拒绝并计数，一种进旁路批并按每实体 applied_seq 裁决盖写。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel">
      <h3 style="margin-top:0">演示流</h3>
      <div class="controls">
        <label>
          投影流
          <select v-model.number="streamId" style="display:block;min-width:16rem;margin-top:0.25rem">
            <option v-for="s in streams" :key="s.id" :value="s.id">{{ s.title }}</option>
          </select>
        </label>
        <div v-if="stream" class="wm-block">
          <div class="stat-label">主水位 seq</div>
          <div class="stat-num">{{ stream.watermark_seq }}</div>
        </div>
        <div v-if="stream" class="wm-block">
          <div class="stat-label">已投递波次</div>
          <div class="stat-num">{{ stream.arrival_wave }}</div>
        </div>
        <button :disabled="busy || !nextWave" @click="runNextWave">
          {{ nextWave ? `投递第 ${nextWave.wave} 波` : '全部波次已投递' }}
        </button>
        <button class="ghost" :disabled="busy" @click="reset">重置本流</button>
      </div>
      <p v-if="nextWave" class="muted" style="margin-bottom:0">
        下一波 {{ nextWave.events.length }} 条事件；
        其中 seq ≤ 主水位 {{ stream.watermark_seq }} 的迟到候选
        <span class="tag warn">{{ pendingLate.length }}</span> 条。
      </p>
    </div>

    <div class="panel" v-if="stream">
      <h3 style="margin-top:0">迟到策略（互斥）</h3>
      <div class="policy-grid">
        <button
          class="policy"
          :class="{ active: stream.late_policy === 'reject' }"
          :disabled="busy"
          @click="switchPolicy('reject')"
        >
          <span class="policy-name">拒绝迟到</span>
          <span class="policy-desc">拒绝纳入常规应用，记迟到拒绝次数，投影与主水位不动</span>
        </button>
        <button
          class="policy"
          :class="{ active: stream.late_policy === 'bypass' }"
          :disabled="busy"
          @click="switchPolicy('bypass')"
        >
          <span class="policy-name">迟到旁路批</span>
          <span class="policy-desc">旁路单独编号，可按规则修正投影，主水位不回拨，计数分列</span>
        </button>
      </div>
      <p class="muted" style="margin-bottom:0">
        当前策略 <span class="tag">{{ stream.late_policy }}</span>。旁路批始终遵守终态裁决：
        仅当迟到 seq &gt; 该实体当前 applied_seq 才允许盖写。
      </p>
    </div>

    <div class="panel" v-if="state">
      <h3 style="margin-top:0">主水位与两批计数</h3>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ state.stream.watermark_seq }}</div><div class="stat-label">当前主水位</div></div>
        <div class="stat"><div class="stat-num">{{ state.main_batches.length }}</div><div class="stat-label">主批次</div></div>
        <div class="stat"><div class="stat-num">{{ state.late_batches.length }}</div><div class="stat-label">迟到批</div></div>
        <div class="stat"><div class="stat-num">{{ state.totals.late_seen }}</div><div class="stat-label">迟到事件</div></div>
        <div class="stat"><div class="stat-num">{{ state.totals.late_rejected }}</div><div class="stat-label">拒绝</div></div>
        <div class="stat"><div class="stat-num">{{ state.totals.late_routed }}</div><div class="stat-label">进旁路</div></div>
        <div class="stat"><div class="stat-num">{{ state.totals.late_applied }}</div><div class="stat-label">旁路盖写</div></div>
        <div class="stat"><div class="stat-num">{{ state.totals.late_ineffective }}</div><div class="stat-label">无效迟到</div></div>
      </div>
    </div>

    <div class="panel" v-if="lastRun">
      <h3 style="margin-top:0">最近一趟</h3>
      <p>
        <template v-if="lastRun.batch_no">
          主批次 <span class="mono">{{ lastRun.batch_no }}</span>
          <span class="tag ok">主</span>
        </template>
        <template v-else>
          <span class="tag warn">本趟无主路径事件</span>
        </template>
        · 水位 <span class="mono">{{ lastRun.watermark_before }} → {{ lastRun.watermark_after }}</span>
        <template v-if="lastRun.late_batch">
          · <span class="mono">{{ lastRun.late_batch.batch_no }}</span>
          <span class="tag warn">{{ lateLaneLabel(lastRun.late_batch) }}</span>
        </template>
      </p>
      <p class="muted" style="margin-bottom:0">
        本趟主路径 {{ lastRun.main.counts.applied_upsert }} 写 /
        {{ lastRun.main.counts.applied_delete }} 删；
        迟到 {{ lastRun.late.counts.late_seen }} 条，拒绝
        {{ lastRun.late.counts.late_rejected }}，进旁路 {{ lastRun.late.counts.late_routed }}，
        盖写 {{ lastRun.late.counts.late_applied }}，无效 {{ lastRun.late.counts.late_ineffective }}。
      </p>
    </div>

    <div class="panel" v-if="state">
      <h3 style="margin-top:0">到达波次</h3>
      <div class="wave-list">
        <div v-for="w in state.waves" :key="w.wave" class="wave" :class="w.status">
          <div class="wave-head">
            第 {{ w.wave }} 波
            <span class="tag" :class="w.status === 'done' ? 'ok' : 'warn'">
              {{ w.status === 'done' ? '已投递' : '待投递' }}
            </span>
          </div>
          <div class="wave-events">
            <span
              v-for="e in w.events"
              :key="e.event_id"
              class="chip"
              :class="{ late: w.status === 'pending' && e.seq <= state.stream.watermark_seq }"
              :title="`${e.entity_key} · ${e.op}`"
            >
              {{ e.event_id }} · seq {{ e.seq }} · {{ e.entity_key }}
            </span>
          </div>
        </div>
      </div>
      <p class="muted" style="margin-bottom:0">
        高亮的事件在下一次投递时 seq 已不大于主水位，会被判为迟到。
      </p>
    </div>

    <div class="panel" v-if="state && state.projection.length">
      <h3 style="margin-top:0">投影行与逐实体 applied_seq</h3>
      <table>
        <thead>
          <tr>
            <th>实体键</th><th>applied_seq</th><th>applied_event_id</th>
            <th>来源车道</th><th>状态</th><th>载荷</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in state.projection" :key="r.id">
            <td class="mono">{{ r.entity_key }}</td>
            <td class="mono">{{ r.applied_seq }}</td>
            <td class="mono">{{ r.applied_event_id }}</td>
            <td>
              <span class="tag" :class="r.applied_lane === 'late' ? 'warn' : ''">
                {{ r.applied_lane === 'late' ? '旁路批' : r.applied_lane === 'main' ? '主批次' : '—' }}
              </span>
            </td>
            <td><span class="tag" :class="r.deleted ? 'danger' : 'ok'">{{ r.deleted ? '已删' : '存活' }}</span></td>
            <td class="mono">{{ r.payload == null ? '—' : JSON.stringify(r.payload) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="margin-bottom:0">
        旁路批盖写过的行会显示「旁路批」来源，但 applied_seq 依然不超过主水位。
      </p>
    </div>

    <div class="panel" v-if="state && state.late_events.length">
      <h3 style="margin-top:0">迟到留痕</h3>
      <table>
        <thead>
          <tr>
            <th>event_id</th><th>实体键</th><th>seq</th>
            <th>实体原 applied_seq</th><th>裁决</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in state.late_events" :key="e.id">
            <td class="mono">{{ e.event_id }}</td>
            <td class="mono">{{ e.entity_key }}</td>
            <td class="mono">{{ e.seq }}</td>
            <td class="mono">{{ e.entity_seq_before ?? '—' }}</td>
            <td>
              <span
                class="tag"
                :class="e.result === 'late_rejected' ? 'danger' : e.result === 'late_applied' ? 'warn' : ''"
              >
                {{ e.result }}
              </span>
            </td>
            <td class="muted">{{ e.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="margin-bottom:0">
        点击任一行可把它的实体键与 seq 填进下面的手工投递，用于验证无效迟到不盖写。
      </p>
      <p style="margin-bottom:0">
        <button
          v-for="e in state.late_events.slice(0, 3)"
          :key="'fill-' + e.id"
          class="ghost small"
          @click="fillFromLate(e)"
        >
          复投 {{ e.event_id }}
        </button>
      </p>
    </div>

    <div class="panel" v-if="state">
      <h3 style="margin-top:0">手工投递一条到达事件</h3>
      <p class="muted">
        用来在水位已前进后现场投一条小 seq 事件，观察它走拒绝还是旁路。
      </p>
      <div class="controls">
        <label>
          event_id
          <input v-model="manual.event_id" style="display:block;width:12rem;margin-top:0.25rem" />
        </label>
        <label>
          实体键
          <input v-model="manual.entity_key" style="display:block;width:8rem;margin-top:0.25rem" />
        </label>
        <label>
          op
          <select v-model="manual.op" style="display:block;margin-top:0.25rem">
            <option value="upsert">upsert</option>
            <option value="delete">delete</option>
          </select>
        </label>
        <label>
          seq
          <input v-model="manual.seq" type="number" style="display:block;width:6rem;margin-top:0.25rem" />
        </label>
        <label style="flex:1;min-width:16rem">
          载荷 JSON（可选）
          <input v-model="manual.payload" placeholder='{"tier":"gold"}' style="display:block;width:100%;margin-top:0.25rem" />
        </label>
        <button :disabled="busy || !manual.event_id || !manual.entity_key || manual.seq === ''" @click="dropLate">
          投递
        </button>
      </div>
      <p class="muted" style="margin-bottom:0">
        当前实体 applied_seq：
        <span v-if="entitySeqs[manual.entity_key]" class="mono">
          {{ entitySeqs[manual.entity_key].applied_seq }}
        </span>
        <span v-else class="mono">无投影行（视为 0）</span>
      </p>
    </div>

    <div class="panel" v-if="state">
      <h3 style="margin-top:0">互证检查</h3>
      <table>
        <thead><tr><th>不变量</th><th>结果</th><th>依据</th></tr></thead>
        <tbody>
          <tr v-for="c in state.checks" :key="c.key">
            <td>{{ c.label }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '通过' : '失败' }}</span></td>
            <td class="muted">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="state && state.main_batches.length">
      <h3 style="margin-top:0">批次分列</h3>
      <h4 style="margin-bottom:0.4rem">主批次（PB-）</h4>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>水位前→后</th><th>源事件</th><th>写</th><th>删</th>
            <th>迟到</th><th>存活</th><th>已删</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in state.main_batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td class="mono">{{ b.watermark_before }} → {{ b.watermark_after }}</td>
            <td>{{ b.source_events }}</td>
            <td>{{ b.applied_upsert }}</td>
            <td>{{ b.applied_delete }}</td>
            <td>{{ b.late_seen }}</td>
            <td>{{ b.projected_alive }}</td>
            <td>{{ b.projected_deleted }}</td>
          </tr>
        </tbody>
      </table>
      <h4 style="margin:0.9rem 0 0.4rem">迟到批（LC-，按策略为拒绝批或旁路批）</h4>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>策略</th><th>水位前→后</th><th>迟到</th>
            <th>拒绝</th><th>进旁路</th><th>盖写</th><th>无效</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in state.late_batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td><span class="tag" :class="b.late_policy === 'bypass' ? 'warn' : 'danger'">{{ lateLaneLabel(b) }}</span></td>
            <td class="mono">{{ b.watermark_before }} → {{ b.watermark_after }}</td>
            <td>{{ b.late_seen }}</td>
            <td>{{ b.late_rejected }}</td>
            <td>{{ b.late_routed }}</td>
            <td>{{ b.late_applied }}</td>
            <td>{{ b.late_ineffective }}</td>
          </tr>
          <tr v-if="!state.late_batches.length">
            <td colspan="8" class="muted">还没有迟到批。</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="margin-bottom:0">
        主批次只含 seq &gt; 水位的事件；迟到批只含迟到事件。两者编号独立、计数分列。
      </p>
    </div>
  </div>
</template>
