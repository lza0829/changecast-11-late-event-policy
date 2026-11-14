<script setup>
import { onMounted, ref } from 'vue'
import { getJSON } from '../api'
const stats = ref(null)
const err = ref('')
onMounted(async () => {
  try { stats.value = await getJSON('/api/dashboard') } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>总览</h1>
    <p class="muted">
      变更日志投影：主路径只按 seq 前进；水位推过之后的小 seq 事件走迟到策略，拒绝计数或进旁路批。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel" v-if="stats">
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ stats.stream_count }}</div><div class="stat-label">投影流</div></div>
        <div class="stat"><div class="stat-num">{{ stats.enabled_streams }}</div><div class="stat-label">已启用</div></div>
        <div class="stat"><div class="stat-num">{{ stats.batch_count }}</div><div class="stat-label">主批次</div></div>
        <div class="stat"><div class="stat-num">{{ stats.late_batch_count }}</div><div class="stat-label">迟到批</div></div>
        <div class="stat"><div class="stat-num">{{ stats.projection_alive }}</div><div class="stat-label">存活投影</div></div>
        <div class="stat"><div class="stat-num">{{ stats.projection_deleted }}</div><div class="stat-label">已删投影</div></div>
      </div>
      <div class="stat-grid" style="margin-top:0.65rem">
        <div class="stat"><div class="stat-num">{{ stats.late_rejected }}</div><div class="stat-label">迟到拒绝</div></div>
        <div class="stat"><div class="stat-num">{{ stats.late_applied }}</div><div class="stat-label">旁路盖写</div></div>
        <div class="stat"><div class="stat-num">{{ stats.late_ineffective }}</div><div class="stat-label">无效迟到</div></div>
        <div class="stat"><div class="stat-num">{{ stats.watermark_max }}</div><div class="stat-label">最高主水位</div></div>
      </div>
      <p style="margin-top:1rem">
        默认应用序 <span class="tag">{{ stats.apply_order }}</span>（按事件 seq 升序）
      </p>
      <p>
        <router-link to="/late-policy">去迟到策略演示 →</router-link>
        ·
        <router-link to="/run">去投影台跑一趟 →</router-link>
      </p>
    </div>
  </div>
</template>
