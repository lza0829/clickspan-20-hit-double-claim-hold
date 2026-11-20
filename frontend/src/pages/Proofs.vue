<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const claims = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.proofs()
    claims.value = await api.doubleClaims()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>会话核证</h1>
    <p class="muted">会话击点集合与起止须与织机同源可核。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>

    <h2 style="margin-top: 24px">双占核证（只读）</h2>
    <p class="muted">
      hit_id 全局只属于一个会话成员集合；二次争用立即搁置，原会话成员不变。
      字段：hit_id / 占用会话 / 冲突批次 / 搁置键。
    </p>
    <p v-if="!claims.length" class="muted">暂无双占搁置。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>搁置键</th>
          <th>hit_id</th>
          <th>占用会话</th>
          <th>冲突会话</th>
          <th>冲突批次</th>
          <th>状态</th>
          <th>时刻</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in claims" :key="c.id">
          <td class="mono">{{ c.hold_key }}</td>
          <td class="mono">{{ c.hit_id }}</td>
          <td class="mono">{{ c.claim_session_id }}</td>
          <td class="mono">{{ c.conflict_session_id || '—' }}</td>
          <td class="mono">{{ c.batch_id }}</td>
          <td>{{ c.status }}</td>
          <td class="mono">{{ c.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
