import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { OutcomeBadge, GapMark, FailCount } from '../components/Badge'

export default function Apps() {
  const [rows, setRows] = useState([])
  const [roster, setRoster] = useState([])
  const [name, setName] = useState('')
  const [owner, setOwner] = useState('')
  const [err, setErr] = useState('')
  const load = () => Promise.all([api('/应用'), api('/名册')]).then(([a, m]) => {
    setRows(a); setRoster(m)
  }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  const create = async () => {
    setErr('')
    try {
      await api('/应用', { method: 'POST', body: JSON.stringify({ 名称: name, 负责人: owner, 状态: '启用' }) })
      setName(''); setOwner(''); load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="panel">
      <h2>应用名册</h2>
      {err && <p className="err">{err}</p>}
      <div className="row">
        <label>名称<input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>负责人<input value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
        <button type="button" onClick={create}>新建应用</button>
        <button type="button" className="secondary" onClick={load}>刷新</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>名称</th><th>负责人</th><th>状态</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号}><td>{r.编号}</td><td>{r.名称}</td><td>{r.负责人}</td><td>{r.状态}</td></tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="section-gap">环境版本名册（产版本与统一结局）</h2>
      <p className="policy-line">
        这里的「名册版本」是切流登记的当前指向；若它指向的版本探针红，结局/缺口标与总览、对账、放行回执一致。
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>应用</th><th>环境</th><th>名册版本</th><th>统一结局</th><th>缺口标</th>
              <th>匹配实例</th><th>红灯</th><th>连续失败/阈值</th><th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {roster.map((r, i) => (
              <tr key={i}>
                <td>{r.应用}</td>
                <td>{r.环境}</td>
                <td><b>{r.名册版本 || '-'}</b></td>
                <td><OutcomeBadge value={r.结局} /></td>
                <td><GapMark value={r.缺口标} /></td>
                <td>{r.匹配实例数}</td>
                <td>{r.红灯数}</td>
                <td><FailCount count={r.失败次数} threshold={r.阈值} /></td>
                <td>{r.名册更新时间 || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
