import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { OutcomeBadge, GapMark, FailCount } from '../components/Badge'

const surface_panel = true
export default function Reconcile() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [replay, setReplay] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = () => api('/对账').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const doReplay = async () => {
    setErr(''); setBusy(true)
    try {
      const rep = await api('/对账/重放', { method: 'POST', body: '{}' })
      setReplay(rep)
      await load()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  if (err && !data) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中</div>
  const s = data.发版统计 || {}
  return (
    <div className="panel">
      <h2>发布对账</h2>
      <p className="policy-line">{data.策略}；探针失败阈值 {data.阈值}。对账的「已放行」是切流事实，缺口另由统一结局/缺口标挂账，二者不互相抹平。</p>
      {err && <p className="err">{err}</p>}
      <div className="grid">
        {Object.entries(s).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat"><div className="n">{data.晋升事件数}</div><div className="l">晋升事件数</div></div>
        <div className="stat"><div className="n">{data.审计切流记录数}</div><div className="l">审计切流记录</div></div>
        <div className="stat"><div className="n" style={{ color: (data.缺口台账 || []).length ? 'var(--danger)' : undefined }}>{(data.缺口台账 || []).length}</div><div className="l">缺口台账</div></div>
      </div>

      <div className="toolbar">
        <button type="button" onClick={doReplay} disabled={busy}>{busy ? '重放中…' : '重放结局 / 补挂实例'}</button>
        <button type="button" className="secondary" onClick={load}>刷新对账</button>
        <span className="muted">幂等：可重复点击；半截无实例挂接会补登为「探针待报」，不会重复造行。</span>
      </div>
      {replay && (
        <pre className="replay-report">
{`补挂接 ${replay.补挂接数} 行；重放台账 ${replay.重放台账数} 条；剩余缺口 ${replay.缺口数} 项
${(replay.补挂接 || []).map((x) => `  + 补挂 ${x.应用编号}/${x.环境编号} ${x.版本} 实例#${x.实例编号} -> ${x.初始健康}`).join('\n')}`}
        </pre>
      )}

      <h2 className="section-gap">缺口台账（明示可核，不静默抹平）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>环境</th><th>名册版本</th><th>结局</th><th>缺口标</th><th>红灯已切流</th><th>匹配实例</th><th>红灯</th><th>失败/阈值</th></tr></thead>
          <tbody>
            {(data.缺口台账 || []).length === 0 && (
              <tr><td colSpan={9} className="ok-text">无缺口：所有已切流名册版本均有同版本实例且探针齐绿。</td></tr>
            )}
            {(data.缺口台账 || []).map((r, i) => (
              <tr key={i}>
                <td>{r.应用}</td><td>{r.环境}</td><td><b>{r.名册版本}</b></td>
                <td><OutcomeBadge value={r.结局} /></td><td><GapMark value={r.缺口标} /></td>
                <td>{r.红灯已切流 ? <span className="gap-mark">是</span> : '否'}</td>
                <td>{r.匹配实例数}</td><td>{r.红灯数}</td>
                <td><FailCount count={r.失败次数} threshold={r.阈值} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="section-gap">名册与实例</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>应用</th><th>环境</th><th>名册版本</th><th>实例版本们</th>
              <th>统一结局</th><th>缺口标</th><th>对齐</th>
              <th>匹配/绿/红/待报</th><th>失败/阈值</th>
            </tr>
          </thead>
          <tbody>
            {(data.名册与实例 || []).map((r, i) => (
              <tr key={i}>
                <td>{r.应用}</td><td>{r.环境}</td><td><b>{r.名册版本}</b></td>
                <td>{r.实例版本们 || '-'}</td>
                <td><OutcomeBadge value={r.结局} /></td>
                <td><GapMark value={r.缺口标} /></td>
                <td>{r.对齐 ? <span className="ok-text">对齐</span> : <span className="gap-mark">有缺口</span>}</td>
                <td>{r.匹配实例数 ?? '-'} / {r.绿数 ?? 0} / {r.红灯数 ?? 0} / {r.待报数 ?? 0}</td>
                <td><FailCount count={r.失败次数} threshold={r.阈值} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
