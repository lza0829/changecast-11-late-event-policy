<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'

const streams = ref([])
const batches = ref([])
const streamId = ref(null)
const note = ref('')
const last = ref(null)
const busy = ref(false)
const err = ref('')

async function refresh() {
  streams.value = await getJSON('/api/streams')
  batches.value = await getJSON('/api/batches?limit=20')
  if (streamId.value == null && streams.value.length) streamId.value = streams.value[0].id
}

async function run() {
  err.value = ''
  busy.value = true
  try {
    last.value = await postJSON('/api/project', {
      stream_id: Number(streamId.value),
      note: note.value || null,
    })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

const selected = () => streams.value.find((s) => s.id === streamId.value) || null

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>投影台</h1>
    <p class="muted">
      每次投递夹具里的下一波到达事件：seq 大于主水位的走普通增量并推进水位；
      seq 不大于主水位的按所选迟到策略处理。迟到事件不会混进主批次。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel">
      <h3 style="margin-top:0">发起一趟</h3>
      <div class="controls">
        <label>
          投影流
          <select v-model.number="streamId" style="display:block;min-width:18rem;margin-top:0.25rem">
            <option v-for="s in streams" :key="s.id" :value="s.id">{{ s.title }}</option>
          </select>
        </label>
        <label>
          备注（可选）
          <input v-model="note" style="display:block;width:16rem;margin-top:0.25rem" placeholder="本趟说明" />
        </label>
        <button :disabled="busy || !streamId" @click="run">{{ busy ? '投影中…' : '投递下一波' }}</button>
      </div>
      <p v-if="selected()" class="muted" style="margin-bottom:0">
        当前水位 <span class="mono">{{ selected().watermark_seq }}</span>
        · 已投递第 <span class="mono">{{ selected().arrival_wave }}</span> 波
        · 迟到策略 <span class="tag">{{ selected().late_policy }}</span>
      </p>
    </div>

    <div class="panel" v-if="last">
      <h3 style="margin-top:0">最近一趟</h3>
      <p>
        <template v-if="last.batch_no">
          主批次 <span class="mono">{{ last.batch_no }}</span>
        </template>
        <template v-else>
          <span class="tag warn">本趟主路径无事件</span>
        </template>
        · {{ last.stream_title }} ·
        <span class="tag ok">{{ last.status }}</span>
        · 水位 <span class="mono">{{ last.watermark_before }} → {{ last.watermark_after }}</span>
      </p>
      <p v-if="last.late_batch">
        旁路批 <span class="mono">{{ last.late_batch.batch_no }}</span> ·
        拒绝 {{ last.late.counts.late_rejected }} ·
        进旁路 {{ last.late.counts.late_routed }} ·
        盖写 {{ last.late.counts.late_applied }} ·
        无效 {{ last.late.counts.late_ineffective }}
      </p>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ last.main.counts.source_events }}</div><div class="stat-label">主路径源事件</div></div>
        <div class="stat"><div class="stat-num">{{ last.main.counts.applied_upsert }}</div><div class="stat-label">写入</div></div>
        <div class="stat"><div class="stat-num">{{ last.main.counts.applied_delete }}</div><div class="stat-label">删除</div></div>
        <div class="stat"><div class="stat-num">{{ last.main.counts.skipped_duplicate }}</div><div class="stat-label">跳过重复</div></div>
        <div class="stat"><div class="stat-num">{{ last.late.counts.late_seen }}</div><div class="stat-label">迟到事件</div></div>
        <div class="stat"><div class="stat-num">{{ last.main.counts.projected_alive }}</div><div class="stat-label">存活</div></div>
      </div>
      <table style="margin-top:0.75rem">
        <thead>
          <tr><th>event_id</th><th>实体键</th><th>seq</th><th>车道</th><th>结果</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in [...last.main.log, ...last.late.log]" :key="'r-' + e.event_id">
            <td class="mono">{{ e.event_id }}</td>
            <td class="mono">{{ e.entity_key }}</td>
            <td class="mono">{{ e.seq }}</td>
            <td><span class="tag" :class="e.is_late ? 'warn' : 'ok'">{{ e.is_late ? '迟到' : '主' }}</span></td>
            <td><span class="tag">{{ e.result }}</span></td>
            <td class="muted">{{ e.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p style="margin-top:0.75rem">
        <router-link :to="{ path: '/projections', query: { stream_id: last.stream_id } }">查看投影快照 →</router-link>
        ·
        <router-link v-if="last.main_batch_id" :to="{ path: '/events', query: { batch_id: last.main_batch_id } }">主批次留痕 →</router-link>
        <template v-if="last.late_batch">
          ·
          <router-link :to="{ path: '/events', query: { batch_id: last.late_batch.id } }">旁路批留痕 →</router-link>
        </template>
        ·
        <router-link to="/late-policy">迟到策略演示 →</router-link>
      </p>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">历史批次</h3>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>车道</th><th>投影流</th>
            <th>水位前→后</th><th>源事件</th><th>写入</th><th>删除</th>
            <th>迟到</th><th>拒绝</th><th>进旁路</th><th>盖写</th><th>无效</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td><span class="tag" :class="b.batch_kind === 'late' ? 'warn' : 'ok'">{{ b.batch_kind === 'late' ? '旁路' : '主' }}</span></td>
            <td>{{ b.stream_title }}</td>
            <td class="mono">{{ b.watermark_before }} → {{ b.watermark_after }}</td>
            <td>{{ b.source_events }}</td>
            <td>{{ b.applied_upsert }}</td>
            <td>{{ b.applied_delete }}</td>
            <td>{{ b.late_seen }}</td>
            <td>{{ b.late_rejected }}</td>
            <td>{{ b.late_routed }}</td>
            <td>{{ b.late_applied }}</td>
            <td>{{ b.late_ineffective }}</td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
          <tr v-if="!batches.length"><td colspan="13" class="muted">还没有批次，先投递一波。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
