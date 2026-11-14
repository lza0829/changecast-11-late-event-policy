<script setup>
import { onMounted, ref, watch } from 'vue'
import { getJSON } from '../api'
const rows = ref([])
const kind = ref('')
const err = ref('')

async function refresh() {
  const q = new URLSearchParams({ limit: '100' })
  if (kind.value) q.set('kind', kind.value)
  rows.value = await getJSON('/api/batches?' + q.toString())
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})

watch(kind, async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>应用批次</h1>
    <p class="muted">
      主批次（PB-）只含 seq 大于水位的事件；迟到批（LC-）只含迟到事件，按策略分为拒绝批与旁路批。编号独立、计数分列。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <label>
        车道
        <select v-model="kind" style="display:block;margin-top:0.25rem">
          <option value="">全部</option>
          <option value="main">主批次</option>
          <option value="late">迟到批</option>
        </select>
      </label>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>批次</th><th>车道</th><th>投影流</th>
            <th>水位前→后</th>
            <th>源事件</th><th>写入</th><th>删除</th><th>跳过</th>
            <th>迟到</th><th>拒绝</th><th>进旁路</th><th>盖写</th><th>无效</th>
            <th>存活</th><th>已删</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in rows" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td>
              <span class="tag" :class="b.batch_kind === 'late' ? 'warn' : 'ok'">
                {{ b.batch_kind === 'late' ? (b.late_policy === 'reject' ? '拒绝' : '旁路') : '主' }}
              </span>
            </td>
            <td>{{ b.stream_title }}</td>
            <td class="mono">{{ b.watermark_before }} → {{ b.watermark_after }}</td>
            <td>{{ b.source_events }}</td>
            <td>{{ b.applied_upsert }}</td>
            <td>{{ b.applied_delete }}</td>
            <td>{{ b.skipped_duplicate }}</td>
            <td>{{ b.late_seen }}</td>
            <td>{{ b.late_rejected }}</td>
            <td>{{ b.late_routed }}</td>
            <td>{{ b.late_applied }}</td>
            <td>{{ b.late_ineffective }}</td>
            <td>{{ b.projected_alive }}</td>
            <td>{{ b.projected_deleted }}</td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="16" class="muted">尚无批次。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
