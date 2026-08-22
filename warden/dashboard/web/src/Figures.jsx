/*
 * The fleet, drawn.
 *
 * The first version of this file gave all four agents the same head-on-a-stand
 * body and differentiated them with small props. On a 720p recording that
 * reads as four identical robots — nobody is looking at the label. So each
 * agent now has a genuinely different silhouette, and the silhouette is what
 * it does:
 *
 *   triage          antennae on a tower — listening to everything first
 *   diagnostician   a magnifier — a lens and a handle, no body at all
 *   remediator      a box holding a document out to one side, deliberately
 *                   asymmetric: it is offering the patch, not applying it
 *   verifier        a gauge — a dial and a needle watching a number return
 *
 * State lives in one colour and one motion, never in a badge alone: `working`
 * breathes, `blocked` flashes, `done` goes green and still.
 *
 * The three non-agent actors are drawn in the same language on purpose. They
 * stand in the same row as the agents, and the whole point of the picture is
 * that two of the seven stages have nobody from the fleet in them.
 */

const VB = '0 0 72 78'

/* Every actor rests on the same stand, so the row shares a baseline. */
function Base() {
  return (
    <>
      <rect className="fig-stroke" x="27" y="62" width="18" height="4" rx="2" />
      <rect className="fig-stroke" x="17" y="68" width="38" height="5" rx="2.5" />
    </>
  )
}

function Triage() {
  return (
    <>
      <line className="fig-ant" x1="25" y1="20" x2="16" y2="5" />
      <line className="fig-ant" x1="47" y1="20" x2="56" y2="5" />
      <circle className="fig-tip" cx="16" cy="4" r="3" />
      <circle className="fig-tip" cx="56" cy="4" r="3" />
      <rect className="fig-shell" x="16" y="18" width="40" height="27" rx="7" />
      <rect className="fig-visor" x="22" y="26" width="28" height="9" rx="4.5" />
      <rect className="fig-stroke" x="30" y="46" width="12" height="9" rx="3" />
      <Base />
    </>
  )
}

function Diagnostician() {
  return (
    <>
      <line className="fig-handle" x1="43" y1="38" x2="57" y2="54" />
      <circle className="fig-ring" cx="31" cy="26" r="17" />
      <circle className="fig-visor-c" cx="31" cy="26" r="13" />
      <circle className="fig-glint" cx="26" cy="21" r="3.5" />
      <Base />
    </>
  )
}

function Remediator() {
  return (
    <>
      <rect className="fig-shell" x="8" y="16" width="32" height="28" rx="7" />
      <rect className="fig-visor" x="14" y="25" width="20" height="8" rx="4" />
      <line className="fig-arm" x1="40" y1="30" x2="51" y2="30" />
      <rect className="fig-doc" x="49" y="19" width="19" height="23" rx="2.5" />
      <line className="fig-doc-line" x1="53" y1="26" x2="64" y2="26" />
      <line className="fig-doc-line" x1="53" y1="31" x2="64" y2="31" />
      <line className="fig-doc-line" x1="53" y1="36" x2="59" y2="36" />
      <rect className="fig-stroke" x="18" y="46" width="12" height="9" rx="3" />
      <Base />
    </>
  )
}

function Verifier() {
  return (
    <>
      <path className="fig-dial" d="M12 38 A24 24 0 0 1 60 38" fill="none" />
      <line className="fig-tick" x1="15" y1="30" x2="20" y2="31" />
      <line className="fig-tick" x1="36" y1="14" x2="36" y2="19" />
      <line className="fig-tick" x1="57" y1="30" x2="52" y2="31" />
      <line className="fig-needle" x1="36" y1="38" x2="48" y2="24" />
      <circle className="fig-hub" cx="36" cy="38" r="4.5" />
      <rect className="fig-shell" x="22" y="42" width="28" height="14" rx="4" />
      <Base />
    </>
  )
}

const AGENTS = {
  triage: Triage,
  diagnostician: Diagnostician,
  remediator: Remediator,
  verifier: Verifier,
}

const LABELS = {
  idle: 'waiting',
  working: 'working',
  done: 'done',
  blocked: 'held',
  failed: 'failed',
}

function Svg({ state, label, children, size }) {
  return (
    <svg
      className={`fig fig-${state}`}
      viewBox={VB}
      width={size}
      height={(size * 78) / 72}
      role="img"
      aria-label={label}
    >
      {children}
    </svg>
  )
}

export function AgentFigure({ name, state = 'idle', size = 78 }) {
  const Shape = AGENTS[name] ?? Triage
  return (
    <Svg state={state} size={size} label={`${name}, ${LABELS[state] ?? state}`}>
      <Shape />
    </Svg>
  )
}

/* ---- the three actors that are not agents ---- */

export function AlertFigure({ state = 'idle', size = 78 }) {
  return (
    <Svg state={state} size={size} label={`alert, ${LABELS[state] ?? state}`}>
      {/* a sign on a post, so it stands on the same baseline as the rest */}
      <path className="fig-shell" d="M36 8 L61 48 L11 48 Z" />
      <line className="fig-bang" x1="36" y1="24" x2="36" y2="35" />
      <circle className="fig-tip" cx="36" cy="41" r="2.6" />
      <rect className="fig-stroke" x="33" y="48" width="6" height="14" rx="2" />
      <Base />
    </Svg>
  )
}

export function HumanFigure({ state = 'idle', size = 78 }) {
  return (
    <Svg state={state} size={size} label={`human reviewer, ${LABELS[state] ?? state}`}>
      {/* the only fully round head in the row, and the only one with no visor */}
      <circle className="fig-shell" cx="36" cy="22" r="13" />
      <path className="fig-shell" d="M17 55 A19 18 0 0 1 55 55 Z" />
      <Base />
    </Svg>
  )
}

export function CiFigure({ state = 'idle', size = 78 }) {
  return (
    <Svg state={state} size={size} label={`CI applier, ${LABELS[state] ?? state}`}>
      <rect className="fig-shell" x="13" y="14" width="46" height="34" rx="4" />
      <line className="fig-doc-line" x1="20" y1="24" x2="38" y2="24" />
      <line className="fig-doc-line" x1="20" y1="31" x2="47" y2="31" />
      <line className="fig-doc-line" x1="20" y1="38" x2="33" y2="38" />
      <rect className="fig-stroke" x="33" y="48" width="6" height="14" rx="2" />
      <Base />
    </Svg>
  )
}

export { LABELS }
