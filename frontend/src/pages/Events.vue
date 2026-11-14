<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getJSON } from '../api'

const route = useRoute()
const rows = ref([])
const batchId = ref(null)
const onlyLate = ref(false)
const err = ref('')

async function refresh() {
  const q = new URLSearchParams()
  if (batchId.value) q.set('batch_id', String(batchId.value))
  rows.value = await getJSON('/api/events?' + q.toString())
}

onMounted(async () => {
  try {
    if (route.query.batch_id) batchId.value = Number(route.query.batch_id)
    if (route.query.late === '1') onlyLate.value = true
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch([batchId, onlyLate], async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>事件留痕</h1>
    <p class="muted">
      已处理事件：主路径写入 / 删除 / 跳过重复；迟到则分拒绝、旁路盖写、无效迟到三类。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel controls">
      <label>
        批次 ID（可选）
        <input v-model.number="batchId" type="number" style="display:block;margin-top:0.25rem;width:8rem" placeholder="全部" />
      </label>
      <label style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.15rem">
        <input type="checkbox" v-model="onlyLate" />
        只迟到留痕
      </label>
      <button class="ghost" @click="batchId = null; refresh()">看全部</button>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>批次</th><th>车道</th><th>event_id</th><th>实体键</th><th>op</th>
            <th>seq</th><th>实体原 seq</th><th>结果</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows.filter((x) => !onlyLate || x.is_late)" :key="e.id">
            <td>{{ e.batch_id }}</td>
            <td>
              <span class="tag" :class="e.is_late ? 'warn' : 'ok'">{{ e.is_late ? '迟到' : '主' }}</span>
            </td>
            <td class="mono">{{ e.event_id }}</td>
            <td class="mono">{{ e.entity_key }}</td>
            <td class="mono">{{ e.op }}</td>
            <td class="mono">{{ e.seq }}</td>
            <td class="mono">{{ e.entity_seq_before ?? '—' }}</td>
            <td><span class="tag">{{ e.result }}</span></td>
            <td class="muted">{{ e.detail || '—' }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9" class="muted">暂无留痕。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
