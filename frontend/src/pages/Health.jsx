import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { OutcomeBadge, GapMark, FailCount } from '../components/Badge'

const surface_panel = true
export default function Health() {
  const [rows, setRows] = useState([])
  const [apps, setApps] = useState([])
  const [err, setErr] = useState('')
  const [appId, setAppId] = useState('')
  const [env, setEnv] = useState('产')
  const [ver, setVer] = useState('')
  const [health, setHealth] = useState('探针红')
  const [failN, setFailN] = useState('3')

  const load = () => api('/实例').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => {
    load()
    api('/应用').then((a) => { setApps(a); if (!appId && a[0]) setAppId(String(a[0].编号)) }).catch(() => {})
  }, [])

  const setH = async (id, h) => {
    setErr('')
    try {
      await api(`/实例/${id}/健康`, { method: 'POST', body: JSON.stringify({ 健康: h }) })
      load()
    } catch (e) { setErr(e.message) }
  }

  const register = async () => {
    setErr('')
    try {
      await api('/实例/登记', {
        method: 'POST',
        body: JSON.stringify({
          应用编号: Number(appId), 环境: env, 版本: ver,
          健康: health, 失败次数: failN === '' ? null : Number(failN),
        }),
      })
      setVer('')
      load()
    } catch (e) { setErr(e.message) }
  }

  const threshold = rows[0]?.阈值 ?? 3

  return (
    <div className="panel">
      <h2>健康探针 · 实例册</h2>
      <p className="policy-line">
        失败次数为连续打红累计，转绿清零；达到阈值 {threshold} 次升级为「红灯超标」。
        每行的结局/缺口标与名册、放行回执、总览、对账、审计共用同一口径。
      </p>
      {err && <p className="err">{err}</p>}

      <div className="mini-form">
        <label>应用
          <select value={appId} onChange={(e) => setAppId(e.target.value)}>
            {apps.map((a) => <option key={a.编号} value={a.编号}>{a.名称}</option>)}
          </select>
        </label>
        <label>环境
          <select value={env} onChange={(e) => setEnv(e.target.value)}>
            <option>测</option><option>预</option><option>产</option>
          </select>
        </label>
        <label>版本<input value={ver} onChange={(e) => setVer(e.target.value)} placeholder="2.4.0" /></label>
        <label>初始探针
          <select value={health} onChange={(e) => setHealth(e.target.value)}>
            <option>探针红</option><option>探针绿</option><option>探针待报</option>
          </select>
        </label>
        <label>失败次数<input style={{ width: 90 }} value={failN} onChange={(e) => setFailN(e.target.value)} /></label>
        <button type="button" onClick={register}>登记实例</button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>应用</th><th>环境</th><th>版本</th><th>健康</th>
              <th>连续失败/阈值</th><th>统一结局</th><th>槽位缺口标</th>
              <th>发版单</th><th>补挂</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号}>
                <td>{r.应用}</td>
                <td>{r.环境}</td>
                <td>{r.版本}</td>
                <td><OutcomeBadge value={r.健康 === '探针绿' ? '探针全绿' : r.健康 === '探针待报' ? '探针待报' : (r.阈值超标 ? '红灯超标' : '红灯计数中')} /></td>
                <td><FailCount count={r.失败次数} threshold={r.阈值} /></td>
                <td><OutcomeBadge value={r.结局} /></td>
                <td><GapMark value={r.缺口标} /></td>
                <td>{r.发版单编号 || '-'}</td>
                <td>{r.挂接补登 ? '对账补挂' : '-'}</td>
                <td>
                  <button type="button" onClick={() => setH(r.编号, '探针绿')}>探针绿</button>{' '}
                  <button type="button" className="danger" onClick={() => setH(r.编号, '探针红')}>探针红</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
