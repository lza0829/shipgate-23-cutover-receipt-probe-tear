import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { OutcomeBadge, GapMark, FailCount } from '../components/Badge'

function Receipt({ r }) {
  const rc = r.放行回执 || {}
  const gapClass = rc.结局 === '探针全绿' ? 'receipt gap-green'
    : rc.结局 === '红灯超标' || rc.结局 === '无实例挂接' ? 'receipt gap-red'
    : 'receipt gap-amber'
  return (
    <div className={gapClass}>
      <h3>放行回执 · {rc.回执号}</h3>
      <div className="kv">
        <span>发版单 <b>{rc.发版单号}</b></span>
        <span>状态 <b>{rc.状态}</b></span>
        <span>目标环境 <b>{rc.目标环境}</b></span>
        {rc.名册产版本 && <span>名册产版本 <b>{rc.名册产版本}</b></span>}
        <span>统一结局 <OutcomeBadge value={rc.结局} /></span>
        <span>缺口标 <GapMark value={rc.缺口标} /></span>
        <span>连续失败/阈值 <FailCount count={rc.失败次数} threshold={rc.阈值} /></span>
        <span>实例 匹配{rc.实例总数} · 绿{rc.绿数} · 红{rc.红灯数} · 待报{rc.待报数}</span>
      </div>
      {rc.缺口标 && (
        <p className="err" style={{ marginBottom: 0 }}>
          已按「允许先切流 · 缺口明示」放行：缺口已同步到名册、发版单、总览、对账与审计，不会被抹平成成功。
        </p>
      )}
    </div>
  )
}

const surface_panel = true
export default function Promote() {
  const [rows, setRows] = useState([])
  const [rid, setRid] = useState('')
  const [env, setEnv] = useState('预')
  const [err, setErr] = useState('')
  const [receipt, setReceipt] = useState(null)
  const load = async () => {
    const all = await api('/发版单')
    const active = all.filter((r) => ['已入闸', '晋升中'].includes(r.状态))
    setRows(active)
    if (!rid && active[0]) setRid(String(active[0].编号))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const go = async () => {
    setErr(''); setReceipt(null)
    try {
      const data = await api(`/发版单/${rid}/晋升`, { method: 'POST', body: JSON.stringify({ 目标环境: env }) })
      setReceipt(data)
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="panel">
      <h2>环境晋升 / 放行入口</h2>
      <p className="policy-line">目标环境选「产」即生产切流。本站贯彻：允许先切流、探针红不阻塞，但结局与缺口全站挂账。</p>
      {err && <p className="err">{err}</p>}
      {receipt && <Receipt r={receipt} />}
      <div className="row">
        <label>发版单
          <select value={rid} onChange={(e) => setRid(e.target.value)}>
            {rows.map((r) => <option key={r.编号} value={r.编号}>{r.发版单号} · {r.应用} · {r.当前环境}</option>)}
          </select>
        </label>
        <label>目标环境
          <select value={env} onChange={(e) => setEnv(e.target.value)}>
            <option value="测">测</option>
            <option value="预">预</option>
            <option value="产">产</option>
          </select>
        </label>
        <button type="button" onClick={go}>晋升 / 放行</button>
        <button type="button" className="secondary" onClick={() => load().catch((e) => setErr(e.message))}>刷新</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>应用</th><th>目标版本</th><th>状态</th><th>当前环境</th><th>统一结局</th><th>缺口标</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号}>
                <td>{r.发版单号}</td><td>{r.应用}</td><td>{r.目标版本}</td><td>{r.状态}</td><td>{r.当前环境}</td>
                <td><OutcomeBadge value={r.结局} /></td><td><GapMark value={r.缺口标} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
