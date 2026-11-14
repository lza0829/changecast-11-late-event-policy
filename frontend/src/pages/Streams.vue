<script setup>
import { onMounted, ref } from 'vue'
import { getJSON } from '../api'
const rows = ref([])
const policies = ref([])
const err = ref('')
onMounted(async () => {
  try {
    rows.value = await getJSON('/api/streams')
    policies.value = await getJSON('/api/late-policies')
  } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>投影流</h1>
    <p class="muted">每条流绑定 CDC 夹具、实体键名与载荷字段，并各自持有一条主水位与一条迟到策略。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>编码</th><th>标题</th><th>业务域</th>
            <th>实体键</th><th>载荷字段</th><th>已投递波次</th>
            <th>主水位 seq</th><th>迟到策略</th><th>启用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in rows" :key="s.id">
            <td>{{ s.id }}</td>
            <td class="mono">{{ s.code }}</td>
            <td>{{ s.title }}</td>
            <td>{{ s.domain }}</td>
            <td class="mono">{{ s.entity_key_name }}</td>
            <td class="mono">{{ s.payload_fields.join(', ') }}</td>
            <td class="mono">{{ s.arrival_wave }}</td>
            <td class="mono">{{ s.watermark_seq }}</td>
            <td><span class="tag" :class="s.late_policy === 'bypass' ? 'warn' : ''">{{ s.late_policy }}</span></td>
            <td><span class="tag" :class="s.enabled ? 'ok' : 'danger'">{{ s.enabled ? '是' : '否' }}</span></td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="margin-bottom:0">
        迟到策略在
        <router-link to="/late-policy">迟到策略</router-link>
        页切换。
      </p>
    </div>
    <div class="panel">
      <h3 style="margin-top:0">可选的迟到策略</h3>
      <table>
        <thead><tr><th>编码</th><th>名称</th><th>语义</th></tr></thead>
        <tbody>
          <tr v-for="p in policies" :key="p.code">
            <td class="mono">{{ p.code }}</td>
            <td>{{ p.title }}</td>
            <td class="muted">{{ p.summary }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
