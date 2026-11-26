import React, { useEffect, useState } from 'react'
import { api } from '../api'

const KINDS = ['', '切流', '晋升', '探针', '对账', '实例', '发版单', '应用', '设置']

const surface_panel = true
export default function Audit() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')
  const [kind, setKind] = useState('')
  const [kw, setKw] = useState('')

  const load = (k = kind, word = kw) => {
    const params = new URLSearchParams()
    if (k) params.set('类别', k)
    if (word) params.set('关键词', word)
    const qs = params.toString()
    return api(`/审计${qs ? `?${qs}` : ''}`).then(setRows).catch((e) => setErr(e.message))
  }
  useEffect(() => { load('', '').catch(() => {}) }, [])

  return (
    <div className="panel">
      <h2>审计流水 · 切流记录可查</h2>
      {err && <p className="err">{err}</p>}
      <div className="toolbar">
        <label>类别
          <select value={kind} onChange={(e) => { setKind(e.target.value); load(e.target.value, kw) }}>
            {KINDS.map((k) => <option key={k} value={k}>{k || '全部'}</option>)}
          </select>
        </label>
        <input value={kw} onChange={(e) => setKw(e.target.value)} placeholder="关键词，如 切流登记 / 超标 / 发单号" style={{ minWidth: 260 }}
          onKeyDown={(e) => { if (e.key === 'Enter') load() }} />
        <button type="button" onClick={() => load()}>查询</button>
        <button type="button" className="secondary" onClick={() => { setKind(''); setKw(''); load('', '') }}>重置</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>类别</th><th>动作</th><th>关联</th><th>备注</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号}>
                <td>{r.时间}</td><td>{r.类别}</td><td>{r.动作}</td><td>{r.关联编号 || '-'}</td><td>{r.备注}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={() => load()}>刷新</button></div>
    </div>
  )
}
