<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>搁置区</h1>
    <p class="muted">
      脏击点 / 冲突组搁置落库后在此可见。双占搁置（hit_id_double_claim）写明
      hit_id、占用会话与冲突批次，只读核证见每条搁置键。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!rows.length" class="muted">暂无搁置记录。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>ID</th>
          <th>来源</th>
          <th>访客</th>
          <th>原因</th>
          <th>hit_id</th>
          <th>占用会话 → 冲突会话</th>
          <th>批次 / 搁置键</th>
          <th>载荷</th>
          <th>时刻</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.source ? `${r.source}-${r.id}` : r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.source || 'quarantine' }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.reason }}</td>
          <td class="mono">{{ r.hit_id || '—' }}</td>
          <td class="mono">
            <template v-if="r.claim_session_id">
              {{ r.claim_session_id }} → {{ r.conflict_session_id }}
            </template>
            <template v-else>—</template>
          </td>
          <td class="mono">
            <template v-if="r.batch_id">{{ r.batch_id }}<br />{{ r.hold_key }}</template>
            <template v-else>—</template>
          </td>
          <td class="mono">{{ r.member_json }}</td>
          <td class="mono">{{ r.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
