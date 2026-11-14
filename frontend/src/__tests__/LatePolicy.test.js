import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import LatePolicy from '../pages/LatePolicy.vue'

const BASE = 'http://127.0.0.1:10400'

async function api(path, opts) {
  const r = await realFetch(BASE + path, opts)
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json()
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.__realFetch = realFetch
const fetchImpl = (url, opts) => {
  const target = String(url).startsWith('/api/') ? BASE + String(url) : url
  return realFetch(target, opts)
}

async function waitFor(predicate, { timeout = 4000, label = 'condition' } = {}) {
  const started = Date.now()
  for (;;) {
    await flushPromises()
    if (predicate()) return
    if (Date.now() - started > timeout) {
      throw new Error(`waitFor timed out: ${label}`)
    }
    await new Promise((res) => setTimeout(res, 25))
  }
}

async function settle(times = 6) {
  for (let i = 0; i < times; i += 1) {
    await flushPromises()
    await new Promise((res) => setTimeout(res, 25))
  }
}

async function mountPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/late-policy', component: LatePolicy }],
  })
  router.push('/late-policy')
  await router.isReady()
  const wrapper = mount(LatePolicy, { global: { plugins: [router] } })
  await waitFor(() => wrapper.find('button.policy').exists(), { label: 'policy panel' })
  await waitFor(
    () => wrapper.text().includes('主水位 seq'),
    { label: 'stream state loaded' },
  )
  return wrapper
}

function button(wrapper, text, { exact = false } = {}) {
  const found = wrapper.findAll('button').filter((b) =>
    exact ? b.text() === text : b.text().includes(text),
  )
  if (found.length !== 1) {
    throw new Error(`expected one button containing "${text}", got ${found.length}`)
  }
  return found[0]
}

async function clickAndSettle(wrapper, text, expected, opts = {}) {
  await button(wrapper, text, opts).trigger('click')
  if (expected) await waitFor(expected, { label: text })
  else await waitFor(
    () => !wrapper.findAll('button').some((b) => b.text() === '投影中…'),
    { label: 'not busy after ' + text },
  )
}

describe('LatePolicy page against a real backend', () => {
  const streamId = 1

  beforeEach(async () => {
    vi.stubGlobal('fetch', fetchImpl)
    await api('/api/streams/1/reset', { method: 'POST' })
    await api('/api/streams/1/late-policy', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ late_policy: 'reject' }),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows the next wave as pending and the watermark', async () => {
    const wrapper = await mountPage()
    const text = wrapper.text()
    expect(text).toContain('迟到事件策略')
    expect(text).toContain('第 1 波')
    expect(text).toContain('待投递')
    expect(text).toContain('主水位 seq')
  })

  it('bypass: wave 2 late events are adjudicated per entity and the watermark only advances', async () => {
    const wrapper = await mountPage()
    await clickAndSettle(wrapper, '迟到旁路批', () => wrapper.text().includes('当前策略 bypass'))
    await clickAndSettle(wrapper, '投递第 1 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 2 波')))
    await clickAndSettle(wrapper, '投递第 2 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 3 波')))

    const text = wrapper.text()
    expect(text).toContain('旁路批')
    expect(text).toContain('LC-')
    expect(text).toContain('迟到留痕')
    expect(text).toContain('late_applied')
    expect(text).toContain('late_ineffective')
    expect(text).toContain('互证检查')
    expect(text).not.toContain('失败')

    const state = await api(`/api/streams/${streamId}/state`)
    expect(state.stream.watermark_seq).toBe(9)
    // the late seq=4 event is above M1001's seq 3 -> applied on the bypass
    expect(state.projection.find((r) => r.entity_key === 'M1001').applied_seq).toBe(4)
    // the late seq=6 event equals M1003's seq 6 -> ineffective
    expect(state.projection.find((r) => r.entity_key === 'M1003').applied_seq).toBe(6)
    expect(state.totals.late_seen).toBe(2)
    expect(state.totals.late_applied).toBe(1)
    expect(state.totals.late_ineffective).toBe(1)
    // bypass batch exists and is separately numbered
    expect(state.late_batches.every((b) => b.batch_no.startsWith('LC-'))).toBe(true)
    expect(state.main_batches.every((b) => b.batch_no.startsWith('PB-'))).toBe(true)
    for (const r of state.projection) {
      expect(r.applied_seq).toBeLessThanOrEqual(state.stream.watermark_seq)
    }
    for (const c of state.checks) expect(c.ok).toBe(true)
  })

  it('reject: late events are counted, projection and watermark untouched', async () => {
    const wrapper = await mountPage()
    await clickAndSettle(wrapper, '拒绝迟到', () => wrapper.text().includes('当前策略 reject'))
    await clickAndSettle(wrapper, '投递第 1 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 2 波')))

    const before = await api(`/api/streams/${streamId}/state`)
    const m1001 = before.projection.find((r) => r.entity_key === 'M1001')
    const wmBefore = before.stream.watermark_seq

    await clickAndSettle(wrapper, '投递第 2 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 3 波')))

    const state = await api(`/api/streams/${streamId}/state`)
    expect(state.stream.watermark_seq).toBe(9)
    expect(state.stream.watermark_seq).toBeGreaterThan(wmBefore)
    expect(state.totals.late_rejected).toBe(2)
    expect(state.totals.late_applied).toBe(0)
    const after = state.projection.find((r) => r.entity_key === 'M1001')
    expect(after.applied_seq).toBe(m1001.applied_seq)
    expect(after.payload).toEqual(m1001.payload)
    expect(state.late_events.every((e) => e.result === 'late_rejected')).toBe(true)
    expect(state.late_batches.every((b) => b.late_applied === 0)).toBe(true)
    for (const c of state.checks) expect(c.ok).toBe(true)
    expect(wrapper.text()).not.toContain('失败')
  })

  it('manual late drop uses the configured policy and never moves the watermark', async () => {
    const wrapper = await mountPage()
    await clickAndSettle(wrapper, '迟到旁路批', () => wrapper.text().includes('当前策略 bypass'))
    await clickAndSettle(wrapper, '投递第 1 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 2 波')))
    await clickAndSettle(wrapper, '投递第 2 波', () => wrapper.findAll('button').some((b) => b.text().includes('投递第 3 波')))

    const stateBefore = await api(`/api/streams/${streamId}/state`)
    const wm = stateBefore.stream.watermark_seq

    const seqInput = wrapper.find('input[type="number"]')
    await seqInput.setValue('3')
    const textInputs = wrapper
      .findAll('input')
      .filter((i) => (i.attributes('type') || 'text') === 'text')
    // manual block order: event_id, entity_key, payload
    await textInputs[textInputs.length - 3].setValue('manual-seq-3')
    await textInputs[textInputs.length - 2].setValue('M1001')
    await flushPromises()

    await clickAndSettle(wrapper, '投递', () =>
      wrapper.text().includes('本趟无主路径事件') || wrapper.text().includes('manual'),
      { exact: true })

    const state = await api(`/api/streams/${streamId}/state`)
    expect(state.stream.watermark_seq).toBe(wm)
    const late = state.late_events.find((e) => e.event_id === 'manual-seq-3')
    expect(late).toBeTruthy()
    // M1001 applied_seq is 4, so a late seq 3 event is ineffective
    expect(late.result).toBe('late_ineffective')
    expect(state.projection.find((r) => r.entity_key === 'M1001').applied_seq).toBe(4)
  })
})
