/*
 * The fleet, drawn.
 *
 * This file has swung twice and the second swing was the wrong one.
 *
 * V1 gave all four agents the same head-on-a-stand and differentiated them
 * with small props. On a 720p recording that reads as four identical robots.
 *
 * V2 overcorrected: each agent became its instrument and nothing else — the
 * diagnostician was a bare magnifier, the verifier a bare gauge. That fixed
 * legibility and broke the more important thing. A viewer scanning the row saw
 * a robot, a magnifier, a box, a dial, a person, a screen and a warning sign,
 * and had no way to tell which of those seven are agents. The row exists to
 * make exactly one point — that two of the seven stages have nobody from the
 * fleet in them — and it cannot make that point if the fleet is not a visibly
 * single species.
 *
 * So: one shared chassis, and the instrument carried above it.
 *
 *   chassis         head, visor, neck, stand. Identical across all four.
 *                   This is what says "agent" before anything else is read.
 *   triage          antennae — it listens to everything before anyone acts
 *   diagnostician   a lens — it looks, and looking is all it can do
 *   remediator      a page — offered for review, not applied
 *   verifier        a readout — a metric that dipped and came back
 *
 * The three non-agents are drawn to be obviously not this species. None has a
 * head, a visor or a held instrument: the alert is a sign on a post, the
 * reviewer is the only round head with a face-free front, and CI is a rack
 * with a change descending into it. They stand on the same baseline because
 * they are in the same pipeline, and they look nothing alike because they are
 * not agents.
 *
 * State lives in one colour and one motion, never in a badge alone: `working`
 * breathes, `blocked` flashes, `done` goes green and still.
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

/*
 * The shared body. Centred on x=36 for all four so the heads line up across
 * the row even when the instruments hang off different sides.
 */
function Chassis() {
  return (
    <>
      <rect className="fig-shell" x="22" y="26" width="28" height="22" rx="6.5" />
      <rect className="fig-lit" x="27" y="32" width="18" height="7" rx="3.5" />
      <rect className="fig-stroke" x="31" y="48" width="10" height="8" rx="3" />
      <Base />
    </>
  )
}

/*
 * Every instrument rides a mast above the head, on the same centre line.
 *
 * Two earlier attempts held the instrument off to one side, first touching the
 * head and then near it. Both failed the same way: at this size two shapes a
 * few units apart stop being two shapes. The lens became a doughnut welded to
 * a box, and the document floated with no visible connection to anything.
 *
 * Triage was the figure that always worked, and the reason is worth copying
 * rather than admiring — its antennae rise from the head with real air under
 * them, so the head stays a head and the prongs stay prongs. So each agent now
 * carries its instrument the same way: one mast, straight up, clear of the
 * head, instrument on top.
 *
 *   triage          two prongs, splayed    it listens before anyone acts
 *   diagnostician   one lens               it looks, and looking is all it does
 *   remediator      a page, portrait       offered for review, not applied
 *   verifier        a readout, landscape   a metric that dipped and came back
 *
 * Every figure is symmetric about x=36, so the row lines up on one spine and
 * the instruments all top out at the same height.
 */

/* Head to instrument. Drawn after the chassis: drawn before, the filled head
 * paints over it and the instrument floats unattached. */
function Mast() {
  return <line className="fig-live" x1="36" y1="26" x2="36" y2="20" />
}

function Triage() {
  return (
    <>
      {/* Drawn before the chassis so the stubs disappear under the head. */}
      <line className="fig-live" x1="28" y1="30" x2="16" y2="11" />
      <line className="fig-live" x1="44" y1="30" x2="56" y2="11" />
      <circle className="fig-lit" cx="15" cy="9" r="3.6" />
      <circle className="fig-lit" cx="57" cy="9" r="3.6" />
      <Chassis />
    </>
  )
}

function Diagnostician() {
  return (
    <>
      <Chassis />
      {/* No Mast: the handle is the mast, tilted. A ring sitting straight up on
          a vertical stalk reads as a second head, not as a lens. */}
      <line className="fig-handle" x1="35" y1="16" x2="43" y2="25" />
      <circle className="fig-ring" cx="29" cy="10" r="8.5" fill="none" />
      <circle className="fig-glint" cx="25" cy="6.5" r="3" />
    </>
  )
}

function Remediator() {
  return (
    <>
      <Chassis />
      <Mast />
      <rect className="fig-doc" x="27" y="1" width="18" height="19" rx="2.5" />
      <line className="fig-line" x1="31" y1="7" x2="41" y2="7" />
      <line className="fig-line" x1="31" y1="11" x2="41" y2="11" />
      <line className="fig-line" x1="31" y1="15" x2="37" y2="15" />
    </>
  )
}

function Verifier() {
  return (
    <>
      <Chassis />
      <Mast />
      <rect className="fig-doc" x="20" y="2" width="32" height="18" rx="3" />
      {/* The shape of a recovery: level, a fall, and back to level. */}
      <polyline className="fig-live" points="24,8 29,8 33,16 40,16 44,8 48,8" fill="none" />
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

export function AgentFigure({ name, state = 'idle', size = 88 }) {
  const Shape = AGENTS[name] ?? Triage
  return (
    <Svg state={state} size={size} label={`${name}, ${LABELS[state] ?? state}`}>
      <Shape />
    </Svg>
  )
}

/* ---- the three actors that are not agents ---- */

export function AlertFigure({ state = 'idle', size = 88 }) {
  return (
    <Svg state={state} size={size} label={`alert, ${LABELS[state] ?? state}`}>
      {/* A sign on a post. No head, no visor, nothing held. */}
      <path className="fig-shell" d="M36 10 L60 49 L12 49 Z" />
      <line className="fig-live" x1="36" y1="26" x2="36" y2="36" />
      <circle className="fig-lit" cx="36" cy="42" r="2.6" />
      <rect className="fig-stroke" x="33" y="49" width="6" height="13" rx="2" />
      <Base />
    </Svg>
  )
}

export function HumanFigure({ state = 'idle', size = 88 }) {
  return (
    <Svg state={state} size={size} label={`human reviewer, ${LABELS[state] ?? state}`}>
      {/* The only fully round head in the row, and the only one with no visor. */}
      <circle className="fig-shell" cx="36" cy="22" r="13" />
      <path className="fig-shell" d="M17 55 A19 18 0 0 1 55 55 Z" />
      <Base />
    </Svg>
  )
}

export function CiFigure({ state = 'idle', size = 88 }) {
  return (
    <Svg state={state} size={size} label={`CI applier, ${LABELS[state] ?? state}`}>
      {/* A rack with a merged change descending into it. Machinery, not a face:
          this stage is the one no agent can occupy, and it should not look
          like one of them wearing a different hat. */}
      <line className="fig-live" x1="36" y1="4" x2="36" y2="14" />
      <polyline className="fig-live" points="31,10 36,15.5 41,10" fill="none" />
      <rect className="fig-shell" x="13" y="20" width="46" height="14" rx="3" />
      <rect className="fig-shell" x="13" y="38" width="46" height="14" rx="3" />
      <circle className="fig-lit" cx="20" cy="27" r="2.3" />
      <circle className="fig-lit" cx="20" cy="45" r="2.3" />
      <line className="fig-line" x1="27" y1="27" x2="52" y2="27" />
      <line className="fig-line" x1="27" y1="45" x2="52" y2="45" />
      <rect className="fig-stroke" x="33" y="52" width="6" height="10" rx="2" />
      <Base />
    </Svg>
  )
}

export { LABELS }
