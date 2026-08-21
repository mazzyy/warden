import { useCallback, useEffect, useState } from 'react'

const usd = (n) => '$' + (n ?? 0).toFixed(4)
const num = (n) => (n ?? 0).toLocaleString()

async function get(path) {
  const res = await fetch(path)
  if (!res.ok && res.status !== 503) throw new Error(`${path} → ${res.status}`)
  return res.json()
}

export default function App() {
  const [view, setView] = useState('fleet')
  const [fleet, setFleet] = useState(null)
  const [incidents, setIncidents] = useState([])
  const [detail, setDetail] = useState(null)
  const [matrix, setMatrix] = useState(null)
  const [health, setHealth] = useState(null)
  const [err, setErr] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [f, i, h] = await Promise.all([
        get('/api/fleet'),
        get('/api/incidents'),
        get('/healthz'),
      ])
      setFleet(f)
      setIncidents(i.incidents)
      setHealth(h)
      setErr(null)
    } catch (e) {
      setErr(e.message)
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10000)
    return () => clearInterval(t)
  }, [refresh])

  useEffect(() => {
    if (view === 'policy' && !matrix) get('/api/policy-matrix').then(setMatrix).catch(() => {})
  }, [view, matrix])

  async function openIncident(id) {
    setDetail(await get(`/api/incidents/${id}`))
    setView('incident')
  }

  async function toggleKill() {
    const engaged = !fleet?.killSwitch
    await fetch('/api/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ engaged }),
    })
    refresh()
  }

  const totals = fleet?.totals ?? {}
  const denials = incidents.reduce((a, i) => a + (i.denials ?? 0), 0)

  return (
    <div className="page">
      <header className="top">
        <div className="brand">
          <h1>Warden</h1>
          <span className="tag">governed remediation fleet · Fortified Enterprise Fleet</span>
        </div>
        <p className="claim">
          No agent holds production write credentials. Their only write primitive is opening a pull request.
        </p>
        <div className="kpis">
          <div className="kpi"><div className="n">{fleet?.agents?.length ?? '—'}</div><div className="l">Agents</div></div>
          <div className="kpi"><div className="n">{incidents.length}</div><div className="l">Incidents</div></div>
          <div className="kpi"><div className="n">{num(totals.tokens)}</div><div className="l">Tokens</div></div>
          <div className="kpi ok"><div className="n">{usd(totals.costUsd)}</div><div className="l">Spend</div></div>
          <div className="kpi red"><div className="n">{denials}</div><div className="l">Denied calls</div></div>
        </div>
      </header>

      {err && <div className="banner red"><b>API unreachable.</b> {err} — is the server running?</div>}

      {fleet?.killSwitch && (
        <div className="banner red">
          <b>Kill switch engaged.</b> The policy proxy is refusing every tool dispatch, fleet-wide.
        </div>
      )}

      {health?.status === 'degraded' && !fleet?.killSwitch && (
        <div className="banner red">
          <b>Health degraded.</b>{' '}
          {Object.entries(health.checks)
            .filter(([, c]) => !c.ok)
            .map(([k, c]) => `${k}: ${c.detail}`)
            .join(' · ')}
        </div>
      )}

      <nav>
        <button className={view === 'fleet' ? 'on' : ''} onClick={() => setView('fleet')}>Fleet</button>
        <button className={view === 'incidents' || view === 'incident' ? 'on' : ''} onClick={() => setView('incidents')}>Incidents</button>
        <button className={view === 'policy' ? 'on' : ''} onClick={() => setView('policy')}>Policy matrix</button>
        <span style={{ flex: 1 }} />
        <button className={`danger ${fleet?.killSwitch ? 'armed' : ''}`} onClick={toggleKill} disabled={!fleet}>
          {fleet?.killSwitch ? 'Release kill switch' : 'Kill switch'}
        </button>
      </nav>

      {view === 'fleet' && <Fleet fleet={fleet} health={health} />}
      {view === 'incidents' && <Incidents incidents={incidents} onOpen={openIncident} />}
      {view === 'incident' && <Detail detail={detail} onBack={() => setView('incidents')} />}
      {view === 'policy' && <Matrix matrix={matrix} />}
    </div>
  )
}

function Fleet({ fleet, health }) {
  if (!fleet) return <section className="panel"><div className="empty">loading…</div></section>
  return (
    <>
      <section className="panel">
        <h2>Fleet</h2>
        <p className="cap">
          Every agent is declared in a YAML manifest versioned in git and loaded into the registry.
          Changing what an agent can do is a commit, not a code change.
        </p>
        <div className="agents">
          {fleet.agents.map((a) => (
            <div className="agent" key={a.name}>
              <h3>{a.name}<span className="model">{a.model}</span></h3>
              <p className="desc">{a.description}</p>
              <dl>
                <dt>tools</dt>
                <dd>{a.tools.map((t) => <span className="chip" key={t}>{t}</span>)}</dd>
                <dt>scopes</dt>
                <dd>{a.scopes.map((s) => (
                  <span className={`chip ${s.includes('write') ? 'w' : ''}`} key={s}>{s}</span>
                ))}</dd>
                <dt>blast radius</dt>
                <dd>{a.blastRadius.namespace} · ≤{a.blastRadius.maxFilesPerPatch} files</dd>
                <dt>budget</dt>
                <dd>{num(a.budget.maxTokensPerRun)} tok · {a.budget.maxToolCalls} calls</dd>
                <dt>used</dt>
                <dd>{a.runs} runs · {num(a.tokens)} tok · {usd(a.costUsd)}</dd>
              </dl>
            </div>
          ))}
        </div>
      </section>

      {health && (
        <section className="panel">
          <h2>Fleet health</h2>
          <p className="cap">
            Live probes, not expiry dates — probing catches revocation, rotation, quota exhaustion
            and expiry at once. The same endpoint sits behind an uptime check, so it guards the
            hosted URL through judging.
          </p>
          {Object.entries(health.checks).map(([name, c]) => (
            <div className={`call ${c.ok ? 'allow' : 'deny'}`} key={name}>
              <span className="mk">{c.ok ? '✓' : '✗'}</span>
              <span><span className="tool">{name}</span> — {c.detail}</span>
              {!c.critical && <span className="lat">non-critical</span>}
            </div>
          ))}
        </section>
      )}
    </>
  )
}

function Incidents({ incidents, onOpen }) {
  return (
    <section className="panel">
      <h2>Incidents</h2>
      <p className="cap">Signal in, pull request out. Click one to see every tool call it made.</p>
      {incidents.length === 0 && <div className="empty">No incidents yet.</div>}
      {incidents.map((i) => (
        <button className="row" key={i.id} onClick={() => onOpen(i.id)}>
          <span className="id mono">{i.id}</span>
          <span className="title">{i.title}</span>
          {i.denials > 0 && <span className="pill deny">{i.denials} denied</span>}
          <span className={`pill ${i.status}`}>{i.status.replace('_', ' ')}</span>
          <span className="meta">{i.toolCalls} calls · {num(i.tokens)} tok</span>
        </button>
      ))}
    </section>
  )
}

function Detail({ detail, onBack }) {
  if (!detail) return <section className="panel"><div className="empty">loading…</div></section>
  const { incident, runs } = detail
  return (
    <section className="panel">
      <button className="back" onClick={onBack}>← all incidents</button>
      <h2>{incident.title}</h2>
      <p className="cap">
        <span className="mono">{incident.id}</span> · {incident.signature} · severity {incident.severity}
        {incident.pr_url && <> · <a href={incident.pr_url} target="_blank" rel="noreferrer">pull request</a></>}
      </p>
      {runs.map((r) => (
        <div className="run" key={r.id}>
          <div className="rh">
            <span className="rn">{r.agent}</span>
            <span className="rm">{r.model}</span>
            <span className="rs">{num(r.total_tokens)} tok · {r.tool_calls} calls · {usd(r.costUsd)}</span>
          </div>
          {r.calls.map((c) => (
            <div className={`call ${c.decision}`} key={c.id}>
              <span className="mk">{c.decision === 'allow' ? '✓' : '✗'}</span>
              <span>
                <span className="tool">{c.tool}</span>
                {c.decision === 'deny' && <b> — DENIED BY POLICY</b>}
                {c.decision === 'deny' && <div className="reason">{c.reason}</div>}
              </span>
              <span className="lat">{c.latency_ms}ms</span>
            </div>
          ))}
          {r.outcome && <div className="reason" style={{ color: 'var(--mut)', marginTop: 6 }}>{r.outcome}</div>}
        </div>
      ))}
    </section>
  )
}

function Matrix({ matrix }) {
  if (!matrix) return <section className="panel"><div className="empty">loading…</div></section>
  const clusterWrites = matrix.rows.filter(
    (r) => r.mutating && Object.values(r.cells).some((c) => c.allowed && r.tool.includes('workload'))
  )
  return (
    <section className="panel">
      <h2>Policy matrix</h2>
      <p className="cap">
        Every agent against every tool in the catalog. This is the honest way to show the governance
        layer — you cannot reliably make a well-prompted model attempt a tool it was told it lacks,
        so ask the policy engine directly.
      </p>
      <div style={{ overflowX: 'auto' }}>
        <table className="mx">
          <thead>
            <tr>
              <th>tool</th>
              {matrix.agents.map((a) => <th key={a}>{a}</th>)}
            </tr>
          </thead>
          <tbody>
            {matrix.rows.map((r) => (
              <tr key={r.tool}>
                <td>{r.tool} {r.mutating && <span className="mut-flag" title="mutating">⚠</span>}</td>
                {matrix.agents.map((a) => (
                  <td key={a} title={r.cells[a].reason}>
                    <span className={`cell ${r.cells[a].allowed ? 'a' : 'd'}`}>
                      {r.cells[a].allowed ? 'allow' : 'deny'}
                    </span>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={`banner ${clusterWrites.length ? 'red' : 'green'}`} style={{ marginTop: 14, marginBottom: 0 }}>
        {clusterWrites.length
          ? <><b>Policy violation.</b> An agent can call a cluster-mutating tool.</>
          : <><b>No agent in the fleet can write to the cluster.</b> The only write primitive anywhere in this system is opening a pull request.</>}
      </div>
    </section>
  )
}
