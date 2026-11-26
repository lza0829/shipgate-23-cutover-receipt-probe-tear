import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { OutcomeBadge, GapMark, FailCount } from '../components/Badge'

function GapTable({ rows }) {
  if (!rows || rows.length === 0) return null
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>应用</th><th>环境</th><th>名册已切版本</th><th>结局</th><th>缺口标</th><th>红灯数</th><th>失败/阈值</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.应用}</td><td>{r.环境}</td><td><b>{r.名册版本}</b></td>
              <td><OutcomeBadge value={r.结局} /></td>
              <td><GapMark value={r.缺口标} /></td>
              <td>{r.红灯数 ?? '-'}</td>
              <td><FailCount count={r.失败次数} threshold={r.阈值} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const surface_panel = true
export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/总览').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中</div>
  const d = data.分布 || {}
  const red = data.红灯已切流 || []
  const attachGaps = data.挂接缺口 || []
  return (
    <div className="panel">
      <h2>总览</h2>
      <p className="policy-line">本站口径：{data.策略}；探针失败阈值 {data.阈值} 次。</p>
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat"><div className="n" style={{ color: red.length ? 'var(--danger)' : undefined }}>{red.length}</div><div className="l">红灯已切流</div></div>
        <div className="stat"><div className="n" style={{ color: attachGaps.length ? '#9a6a08' : undefined }}>{attachGaps.length}</div><div className="l">挂接/待首探缺口</div></div>
      </div>

      <div className={red.length ? 'exposure' : 'exposure clear'}>
        <h3>红灯已切流（名册已切到该版本 · 当前探针红）</h3>
        {red.length
          ? <GapTable rows={red} />
          : <p className="muted" style={{ margin: 0 }}>当前没有「红灯仍算切流且未挂缺口」的生产/环境记录；切流版本探针齐绿。</p>}
      </div>

      {attachGaps.length > 0 && (
        <div className="exposure amber">
          <h3>挂接缺口（已切流但无同版本实例可探 / 待首探）</h3>
          <GapTable rows={attachGaps} />
          <p className="muted" style={{ marginBottom: 0 }}>
            半截「已放行却无实例」会在对账页点「重放结局/补挂实例」后幂等补登为探针待报，重启也会自动收敛。
          </p>
        </div>
      )}

      <h2>在途发版</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>应用</th><th>目标版本</th><th>状态</th><th>当前环境</th><th>流水线</th><th>统一结局</th><th>缺口标</th></tr></thead>
          <tbody>
            {(data.在途 || []).map((r) => (
              <tr key={r.编号}>
                <td>{r.发版单号}</td><td>{r.应用}</td><td>{r.目标版本}</td><td>{r.状态}</td>
                <td>{r.当前环境 || '-'}</td><td>{r.流水线}</td>
                <td><OutcomeBadge value={r.结局} /></td><td><GapMark value={r.缺口标} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>各环境版本名册</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>环境</th><th>版本</th><th>统一结局</th><th>缺口标</th><th>红灯/实例</th><th>更新时间</th></tr></thead>
          <tbody>
            {(data.环境版本 || []).map((v, i) => (
              <tr key={i}>
                <td>{v.应用}</td><td>{v.环境}</td><td>{v.版本 || '-'}</td>
                <td><OutcomeBadge value={v.结局} /></td>
                <td><GapMark value={v.缺口标} /></td>
                <td>{v.红灯数 ?? 0} / {v.匹配实例数 ?? 0}</td>
                <td>{v.更新时间}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
