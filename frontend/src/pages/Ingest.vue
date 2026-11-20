<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const err = ref('')
const busy = ref(false)

async function run() {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h1>收流击点</h1>
    <p class="muted">读取夹具 ingest_batch.json，灌入正式通道（基座不做空闲切窗）。</p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '收流中…' : '跑一批收流' }}</button>
    <p v-if="err" class="err">{{ err }}</p>
    <pre v-if="result" class="mono">{{ result }}</pre>
    <p v-if="result?.report?.held" class="err">
      本批 {{ result.report.held }} 个击点因 hit_id 双占被搁置，正式通道未写入、原会话成员不变，详见「搁置 / 核证」。
    </p>
  </section>
</template>
