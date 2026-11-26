import React from 'react'

// 全站唯一的结局/缺口展示口径：文案必须与后端 outcomes.py 的规范值完全一致，
// 不允许各页面自行改词或只重绘灯色。
export function outcomeClass(value) {
  if (value === '探针全绿') return 'badge badge-green'
  if (value === '红灯超标' || value === '无实例挂接') return 'badge badge-red'
  if (value === '红灯计数中' || value === '探针待报') return 'badge badge-amber'
  return 'badge badge-muted'
}

export function OutcomeBadge({ value }) {
  if (!value) return <span className="muted">-</span>
  return <span className={outcomeClass(value)}>{value}</span>
}

export function GapMark({ value }) {
  if (!value) return <span className="ok-text">无缺口</span>
  return <span className="gap-mark">{value}</span>
}

export function FailCount({ count, threshold }) {
  if (count === undefined || count === null) return <span className="muted">-</span>
  const cls = count <= 0 ? 'fail-n' : count >= threshold ? 'fail-n over' : 'fail-n counting'
  return (
    <span className={cls} title={`连续失败 ${count} 次 / 阈值 ${threshold}`}>
      {count} / {threshold}
    </span>
  )
}
