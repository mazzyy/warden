/*
 * The operations screen.
 *
 * One idea drives the layout: the incident is a track of seven stages, and the
 * fleet only occupies four of them. Alert, human review and CI apply sit in
 * the same row, drawn in the same style, with no agent in them. You are meant
 * to be able to point at the screen and say "nothing the agents do reaches the
 * cluster without passing through those two columns."
 *
 * Everything visible comes from one /api/live poll, so the whole frame is a
 * single consistent moment rather than four endpoints disagreeing about which
 * moment it is.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { AgentFigure, AlertFigure, CiFigure, HumanFigure } from './Figures.jsx'

const usd = (n) => '$' + (n ?? 0).toFixed(4)
const num = (n) => (n ?? 0).toLocaleString()
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : '')

async function get(path) {
  const res = await fetch(path)
  if (!res.ok && res.status !== 503) throw new Error(`${path} → ${res.status}`)
  return res.json()
}

const AGENT_ROLE = {
  triage: 'Is this real, and is it new?',
  diagnostician: 'What is the root cause?',
  remediator: 'What is the smallest fix?',
  verifier: 'Did it actually recover?',
}

const NON_AGENT = {
  // `kind` is the tag in the stage header. The drawings carry this too — one
  // shared chassis for the fleet, three deliberately unlike shapes for the rest
  // — but a viewer seeing the row for the first time should not have to infer
  // it from a silhouette. Three of seven stages have nobody from the fleet in
  // them, and that is the whole argument.
  alert: { Figure: AlertFigure, role: 'Something broke', who: 'Cloud Monitoring', kind: 'signal' },
  review: { Figure: HumanFigure, role: 'Approve, or do not', who: 'A person', kind: 'human' },
  apply: { Figure: CiFigure, role: 'Roll the merged change out', who: 'warden-sync', kind: 'ci' },
}

/* -------------------------------------------------------------------- */

export default function App() {
  const [live, setLive] = useState(null)
  const [matrix, setMatrix] = useState(null)
  const [view, setView] = useState('ops')
  const [picked, setPicked] = useState(null)
  const [err, setErr] = useState(null)
  const [flash, setFlash] = useState({})
  const seen = useRef(new Set())

  const refresh = useCallback(async () => {
    try {
      const d = await get('/api/live')
      setLive(d)
      setErr(null)

      // Flash a column when something new happens in it. Without this a poll
      // interval of one second reads as a static page that occasionally
      // rearranges itself; the eye needs to be told where to look.
      const fresh = {}
      for (const c of d.feed ?? []) {
        const key = `${c.agent}:${c.tool}:${c.ts}`
        if (!seen.current.has(key)) {
          seen.current.add(key)
          if (seen.current.size > 1) fresh[c.agent] = c.decision
        }
      }
      if (Object.keys(fresh).length) {
        setFlash(fresh)
        setTimeout(() => setFlash({}), 900)
      }
    } catch (e) {
      setErr(e.message)
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 1200)
    return () => clearInterval(t)
  }, [refresh])

  useEffect(() => {
    if (view === 'policy' && !matrix) get('/api/policy-matrix').then(setMatrix).catch(() => {})
  }, [view, matrix])

  async function toggleKill() {
    await fetch('/api/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ engaged: !live?.killSwitch }),
    })
    refresh()
  }

  if (!live) {
    return (
      <div className="boot">
        <div className="boot-in">{err ? `cannot reach the API — ${err}` : 'connecting…'}</div>
      </div>
    )
  }

  const agentsBy = Object.fromEntries((live.agents ?? []).map((a) => [a.stage, a]))
  const selected = picked
    ? live.agents.find((a) => a.name === picked)
    : live.agents.find((a) => a.state === 'working') ||
      live.agents.find((a) => a.denials > 0) ||
      live.agents.find((a) => a.state === 'done')

  return (
    <div className={`app${live.killSwitch ? ' killed' : ''}`}>
      <Header live={live} view={view} setView={setView} onKill={toggleKill} err={err} />

      {/* For someone arriving cold. This screen is an operations console: it
          assumes you already know what Warden is, and a judge opening the
          hosted URL does not. One line of thesis and two links cost nothing
          and are the difference between "an empty dashboard" and "oh, that is
          the whole argument". */}
      <div className="thesis">
        <b>No agent here holds a production write credential.</b> Their only write
        primitive is opening a pull request — a human reviews it, and a CI identity
        no agent holds applies the merge.
        <span className="thesis-links">
          <a href="https://github.com/mazzyy/warden" target="_blank" rel="noreferrer">Source</a>
          <a href="https://github.com/mazzyy/estate-gitops" target="_blank" rel="noreferrer">The estate</a>
        </span>
      </div>

      {live.demoData && (
        <div className="banner">
          <b>Sample data.</b> No incident has run against this store yet. Start one with{' '}
          <code>make demo-live</code> and this screen fills with the real thing.
        </div>
      )}

      {view === 'ops' && (
        <>
          <Track live={live} agentsBy={agentsBy} flash={flash} picked={picked} onPick={setPicked} />
          <div className="split">
            <Detail agent={selected} live={live} />
            <Feed feed={live.feed} />
          </div>
          <IncidentBar incident={live.incident} />
        </>
      )}

      {view === 'policy' && <Matrix matrix={matrix} />}
    </div>
  )
}

/* -------------------------------------------------------------------- */

function Header({ live, view, setView, onKill, err }) {
  const inc = live.incident
  return (
    <header className="hdr">
      <div className="hdr-l">
        <div className="mark" aria-hidden="true" />
        <div>
          <div className="hdr-name">Warden</div>
          <div className="hdr-sub">governed SRE fleet</div>
        </div>
      </div>

      <div className="hdr-c">
        {inc ? (
          <>
            <span className={`sev sev-${inc.severity}`}>{inc.severity}</span>
            <span className="hdr-title">{inc.title}</span>
            <span className="hdr-id">{inc.id}</span>
          </>
        ) : (
          <span className="hdr-title dim">no active incident</span>
        )}
      </div>

      <div className="hdr-r">
        {err && <span className="stale">reconnecting</span>}
        <nav className="tabs">
          <button className={view === 'ops' ? 'on' : ''} onClick={() => setView('ops')}>Operations</button>
          <button className={view === 'policy' ? 'on' : ''} onClick={() => setView('policy')}>Policy</button>
        </nav>
        <button className={`kill${live.killSwitch ? ' on' : ''}`} onClick={onKill}>
          {live.killSwitch ? 'Fleet drained' : 'Drain fleet'}
        </button>
      </div>
    </header>
  )
}

/* -------------------------------------------------------------------- */

function Track({ live, agentsBy, flash, picked, onPick }) {
  return (
    <section className="track" aria-label="incident pipeline">
      <div className="track-rail" aria-hidden="true">
        {live.pipeline.map((p) => (
          <div key={p.key} className={`rail-seg rail-${p.state}`} />
        ))}
      </div>

      <div className="track-cols">
        {live.pipeline.map((p, i) => {
          const agent = agentsBy[p.key]
          const meta = NON_AGENT[p.key]
          const fl = agent ? flash[agent.name] : null
          const on = agent && picked === agent.name
          return (
            <div
              key={p.key}
              className={[
                'col',
                `col-${p.state}`,
                agent ? 'col-agent' : 'col-fixed',
                on ? 'col-picked' : '',
                fl ? `col-flash-${fl}` : '',
              ].join(' ')}
              onClick={agent ? () => onPick(picked === agent.name ? null : agent.name) : undefined}
              role={agent ? 'button' : undefined}
              tabIndex={agent ? 0 : undefined}
              onKeyDown={
                agent
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onPick(picked === agent.name ? null : agent.name)
                      }
                    }
                  : undefined
              }
            >
              <div className="col-stage">
                <span className="col-num">{i + 1}</span>
                {p.label}
                <span className={`col-kind kind-${agent ? 'agent' : meta.kind}`}>
                  {agent ? 'agent' : meta.kind}
                </span>
              </div>

              <div className="col-fig">
                {agent ? (
                  <AgentFigure name={agent.name} state={agent.state} />
                ) : (
                  <meta.Figure state={p.state === 'active' ? 'working' : p.state === 'done' ? 'done' : 'idle'} />
                )}
                {agent?.denials > 0 && <span className="deny-badge" title={agent.lastDenial}>{agent.denials} denied</span>}
              </div>

              <div className="col-who">{agent ? agent.name : meta.who}</div>
              <div className="col-role">{agent ? AGENT_ROLE[agent.name] : meta.role}</div>

              {agent ? (
                <div className="col-live">
                  <div className={`chip chip-${agent.state}`}>
                    {agent.state === 'working' && agent.currentTool
                      ? agent.currentTool
                      : agent.state === 'idle'
                        ? 'waiting'
                        : agent.state}
                  </div>
                  <div className="bar" title={`${num(agent.tokens)} of ${num(agent.budget.maxTokens)} tokens`}>
                    <span style={{ width: `${Math.min(agent.tokenPct, 100)}%` }} />
                  </div>
                  <div className="col-nums">
                    <span>{agent.toolCalls} calls</span>
                    <span>{num(agent.tokens)} tok</span>
                  </div>
                </div>
              ) : (
                <div className="col-live">
                  <div className={`chip chip-${p.state === 'active' ? 'working' : p.state}`}>
                    {p.state === 'active' ? 'in progress' : p.state}
                  </div>
                  <div className="col-note">{p.key === 'review' ? 'no agent can do this' : p.detail}</div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

/* -------------------------------------------------------------------- */

function Detail({ agent, live }) {
  if (!agent) {
    return (
      <div className="panel">
        <div className="panel-hd"><h2>Agent detail</h2></div>
        <div className="empty">Nothing has run yet.</div>
      </div>
    )
  }
  return (
    <div className="panel">
      <div className="panel-hd">
        <h2>{agent.name}</h2>
        <span className={`chip chip-${agent.state}`}>{agent.state}</span>
        <span className="mono dim">{agent.model}</span>
      </div>

      <p className="panel-desc">{agent.description}</p>

      <div className="kv">
        <div><b>{agent.toolCalls}</b><span>tool calls</span></div>
        <div><b>{num(agent.tokens)}</b><span>tokens</span></div>
        <div><b>{usd(agent.costUsd)}</b><span>cost</span></div>
        <div className={agent.denials ? 'bad' : ''}><b>{agent.denials}</b><span>denied</span></div>
      </div>

      {agent.outcome && <div className="outcome">{agent.outcome}</div>}

      <div className="panel-sub">Granted tools</div>
      <div className="tools">
        {agent.granted.map((t) => {
          const used = agent.calls.filter((c) => c.tool === t).length
          return (
            <span key={t} className={`tool${used ? ' used' : ''}`}>
              {t}
              {used > 0 && <i>{used}</i>}
            </span>
          )
        })}
      </div>

      <div className="panel-sub">
        What it did
        <span className="hint">click a call to see what came back</span>
      </div>
      <ol className="calls">
        {agent.calls.length === 0 && <li className="empty">no calls yet</li>}
        {agent.calls.map((c, i) => (
          <Call key={`${agent.name}-${i}`} call={c} n={i + 1} />
        ))}
      </ol>

      {live.killSwitch && (
        <div className="drained">
          Fleet drained. Every dispatch is refused, including read-only ones.
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------- */

/*
 * One tool call, openable.
 *
 * The list used to be tool names and latencies, which told you a call was
 * allowed but not what it returned — so a diagnosis could not be checked
 * against its own evidence, which is the one thing an audit trail for an agent
 * most needs to support. Open a row and you see the arguments it was called
 * with and the result it got back.
 */
function Call({ call, n }) {
  const [open, setOpen] = useState(false)
  const denied = call.decision === 'deny'
  const hasBody = denied || call.result || Object.keys(call.args ?? {}).length > 0

  return (
    <li className={`${denied ? 'deny' : ''}${open ? ' open' : ''}`}>
      <button
        className="call-hd"
        onClick={() => hasBody && setOpen(!open)}
        aria-expanded={open}
        disabled={!hasBody}
      >
        <span className="call-n">{n}</span>
        <span className="mono call-tool">{call.tool}</span>
        {hasBody && <span className="call-caret">{open ? '▾' : '▸'}</span>}
        <span className="call-lat">{call.latencyMs}ms</span>
      </button>

      {denied && <div className="call-reason">{call.reason}</div>}

      {open && (
        <div className="call-body">
          {Object.keys(call.args ?? {}).length > 0 && (
            <>
              <div className="call-label">called with</div>
              <pre className="mono">{JSON.stringify(call.args, null, 2)}</pre>
            </>
          )}
          {call.result ? (
            <>
              <div className="call-label">
                returned
                {call.resultTruncated && <em> — truncated</em>}
              </div>
              <pre className="mono">{call.result}</pre>
            </>
          ) : (
            !denied && <div className="call-label">no result recorded</div>
          )}
        </div>
      )}
    </li>
  )
}

/* -------------------------------------------------------------------- */

function Feed({ feed }) {
  const rows = [...(feed ?? [])].reverse()
  return (
    <div className="panel">
      <div className="panel-hd">
        <h2>Every call, as it happens</h2>
        <span className="dim">{rows.length}</span>
      </div>
      <div className="feed">
        {rows.length === 0 && <div className="empty">nothing yet</div>}
        {rows.map((c, i) => (
          <div key={i} className={`fr${c.decision === 'deny' ? ' fr-deny' : ''}`}>
            <span className="fr-mark">{c.decision === 'deny' ? '✕' : '✓'}</span>
            <span className="fr-agent">{c.agent}</span>
            <span className="mono fr-tool">{c.tool}</span>
            <span className="fr-t">{clock(c.ts)}</span>
            {c.decision === 'deny' && <div className="fr-reason">{c.reason}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------- */

function IncidentBar({ incident }) {
  if (!incident) return null
  return (
    <section className="incbar">
      <div className="inc-l">
        <span className="mono dim">{incident.signature}</span>
        {incident.workload && (
          <span className="mono dim">
            {incident.workload.namespace}/{incident.workload.name}
          </span>
        )}
      </div>
      <div className="inc-r">
        <span><b>{incident.toolCalls}</b> calls</span>
        <span className={incident.denials ? 'bad' : ''}><b>{incident.denials}</b> denied</span>
        <span><b>{num(incident.tokens)}</b> tokens</span>
        <span><b>{usd(incident.costUsd)}</b></span>
        {incident.pr_url && (
          <a className="pr" href={incident.pr_url} target="_blank" rel="noreferrer">
            open pull request ↗
          </a>
        )}
      </div>
    </section>
  )
}

/* -------------------------------------------------------------------- */

function Matrix({ matrix }) {
  if (!matrix) return <div className="panel"><div className="empty">loading…</div></div>
  const agents = matrix.agents ?? []
  const rows = matrix.rows ?? []
  return (
    <div className="panel wide">
      <div className="panel-hd">
        <h2>What every agent may call</h2>
        <span className="dim">evaluated by the live policy engine, not a copy of it</span>
      </div>
      <div className="mscroll">
        <table className="mtx">
          <thead>
            <tr>
              <th>Tool</th>
              {agents.map((a) => <th key={a} className="mono">{a}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const nobody = agents.every((a) => !r.cells[a]?.allowed)
              return (
                <tr key={r.tool} className={nobody ? 'nobody' : ''}>
                  <td className="mono">
                    {r.tool}
                    {r.mutating && <i className="mut" title="mutating">▲</i>}
                  </td>
                  {agents.map((a) => {
                    const cell = r.cells[a]
                    return (
                      <td key={a} className={`c ${cell?.allowed ? 'y' : 'n'}`} title={cell?.reason}>
                        {cell?.allowed ? '●' : '·'}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="legend">
        <span><b className="y">●</b> granted</span>
        <span><b className="n">·</b> refused before dispatch</span>
        <span><b className="mut">▲</b> mutating</span>
        <span className="dim">rows granted to nobody exist so the refusal is real</span>
      </div>
    </div>
  )
}
