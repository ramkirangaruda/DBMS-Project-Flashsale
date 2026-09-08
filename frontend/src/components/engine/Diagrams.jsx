import { EngineDefs, MovingDot, PulseRect } from './Motion.jsx'

/** Shared canvas size for every diagram in the Engine Room. */
const VB = '0 0 400 170'

const INK = '#e7e5e0'
const DIM = '#8b8a85'
const LINE = '#3a3936'
const AMBER = '#f7a13a'
const SKY = '#4ea1f0'
const RED = '#ef5b5b'
const GREEN = '#38c774'

function Frame({ children }) {
  return (
    <svg viewBox={VB} className="h-full w-full" role="img" aria-hidden>
      <EngineDefs />
      {children}
    </svg>
  )
}

/* ------------------------------------------------------------------ *
 * 1. The problem -- lost update
 * ------------------------------------------------------------------ */
export function LostUpdateDiagram() {
  return (
    <Frame>
      <rect x={160} y={62} width={80} height={46} rx={10} fill="#201f1c" stroke={LINE} />
      <text x={200} y={54} textAnchor="middle" fill={DIM} fontSize="11" fontWeight="700" letterSpacing=".08em">
        STOCK
      </text>
      <text x={200} y={94} textAnchor="middle" fontSize="22" fontWeight="900" fill={RED} opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.45;0.5;0.95;1" dur="2.2s" repeatCount="indefinite" />
        -1
      </text>
      <text x={200} y={94} textAnchor="middle" fontSize="22" fontWeight="900" fill={INK}>
        <animate attributeName="opacity" values="1;1;0;0;1" keyTimes="0;0.45;0.5;0.95;1" dur="2.2s" repeatCount="indefinite" />
        1
      </text>

      <MovingDot path="M40,26 L184,72" dur="1.1s" color={AMBER} glow />
      <MovingDot path="M360,26 L216,72" dur="1.1s" color={SKY} glow />
      <text x={40} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER A
      </text>
      <text x={360} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER B
      </text>

      <text x={200} y={140} textAnchor="middle" fill={DIM} fontSize="11">
        Both read <tspan fill={INK}>1</tspan>. Both decide there's stock. Both write.
      </text>
      <text x={200} y={156} textAnchor="middle" fill={RED} fontSize="11" fontWeight="700">
        One unit sold twice -- nothing stopped them from colliding.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 2. Pessimistic locking -- SELECT ... FOR UPDATE
 * ------------------------------------------------------------------ */
export function PessimisticDiagram() {
  return (
    <Frame>
      <PulseRect x={160} y={64} w={80} h={44} fill="#201f1c" dur="2.4s" />
      <rect x={160} y={64} width={80} height={44} rx={10} fill="none" stroke={SKY} strokeWidth="1.5" />
      {/* padlock */}
      <path d="M188,58 a12,12 0 0 1 24,0 v6 h-24 z" fill="none" stroke={AMBER} strokeWidth="3" className="origin-center animate-lock-clamp" />
      <rect x={184} y={64} width={32} height={20} rx={4} fill={AMBER} className="origin-center animate-lock-clamp" />
      <text x={200} y={94} textAnchor="middle" fontSize="11" fontWeight="800" fill="#201f1c">
        ROW
      </text>

      <MovingDot path="M40,26 L184,66" dur="1.6s" color={AMBER} glow />
      <text x={40} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER A
      </text>

      <MovingDot path="M360,26 L266,26 L266,26" dur="1.6s" begin="0s" color={SKY} glow />
      <MovingDot path="M266,26 L216,66" dur="1.1s" begin="1.9s" color={SKY} glow />
      <text x={360} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER B
      </text>
      <text x={266} y={18} textAnchor="middle" fill={DIM} fontSize="9">
        waits
      </text>

      <text x={200} y={140} textAnchor="middle" fill={DIM} fontSize="11">
        A locks the row and finishes first. B physically cannot read it
      </text>
      <text x={200} y={156} textAnchor="middle" fill={GREEN} fontSize="11" fontWeight="700">
        until A commits -- correct, but B pays for the wait.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 3. Optimistic concurrency control -- version column
 * ------------------------------------------------------------------ */
export function OptimisticDiagram() {
  return (
    <Frame>
      <rect x={155} y={60} width={90} height={48} rx={10} fill="#201f1c" stroke={LINE} />
      <text x={200} y={78} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700" letterSpacing=".08em">
        VERSION
      </text>
      <text x={200} y={99} textAnchor="middle" fontSize="18" fontWeight="900" fill={GREEN} opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.4;0.46;0.92;1" dur="2.6s" repeatCount="indefinite" />
        v2
      </text>
      <text x={200} y={99} textAnchor="middle" fontSize="18" fontWeight="900" fill={INK}>
        <animate attributeName="opacity" values="1;1;0;0;1" keyTimes="0;0.4;0.46;0.92;1" dur="2.6s" repeatCount="indefinite" />
        v1
      </text>

      <MovingDot path="M40,26 L192,62" dur="1.3s" begin="0s" color={AMBER} glow />
      <text x={40} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER A
      </text>

      <MovingDot path="M360,26 L208,62 L360,26" dur="2.6s" begin="0s" color={SKY} glow />
      <text x={360} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700">
        BUYER B
      </text>

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.42;0.5;0.62;1" dur="2.6s" repeatCount="indefinite" />
        <text x={280} y={62} fontSize="16" fontWeight="900" fill={RED}>
          ✕
        </text>
      </g>

      <text x={200} y={140} textAnchor="middle" fill={DIM} fontSize="11">
        No lock. Both race in -- but B's UPDATE ... WHERE version=v1 matches
      </text>
      <text x={200} y={156} textAnchor="middle" fill={AMBER} fontSize="11" fontWeight="700">
        nothing anymore. It fails fast and retries, cheaply, until contention drops.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 4. Redis atomic DECR fast path
 * ------------------------------------------------------------------ */
export function RedisDiagram() {
  const digits = ['5', '4', '3', '2', '1', '0']
  const n = digits.length
  return (
    <Frame>
      <rect x={140} y={50} width={120} height={60} rx={12} fill="#1c1410" stroke="#7a3a12" />
      <text x={200} y={44} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700" letterSpacing=".08em">
        stock:{'{sale_id}'}
      </text>
      <g textAnchor="middle">
        {digits.map((d, i) => {
          const kt = digits.map((_, j) => (j / n).toFixed(3)).concat('1')
          const values = digits.map((_, j) => (j === i ? 1 : 0)).concat(i === 0 ? 1 : 0)
          return (
            <text key={d} x={200} y={90} fontSize="30" fontWeight="900" fill={AMBER}>
              {d}
              <animate attributeName="opacity" values={values.join(';')} keyTimes={kt.join(';')} dur="1.8s" repeatCount="indefinite" />
            </text>
          )
        })}
      </g>
      <text x={200} y={106} textAnchor="middle" fill={DIM} fontSize="9">
        DECR -- one instruction, no lock, no query planner
      </text>

      <text x={200} y={26} textAnchor="middle" fontSize="16" className="animate-blink">
        ⚡
      </text>

      <g transform="translate(60,120)">
        <rect x={-30} y={-14} width={60} height={28} rx={6} fill="#141a1f" stroke="#274a63" />
        <text x={0} y={4} textAnchor="middle" fill={SKY} fontSize="9" fontWeight="700">
          Postgres
        </text>
      </g>
      <path d="M110,120 L170,102" stroke={LINE} strokeWidth="1.5" strokeDasharray="3 3" markerEnd="url(#arrow)" />
      <text x={140} y={155} textAnchor="middle" fill={DIM} fontSize="10">
        order record only -- Redis alone decides sold-out, in microseconds
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 5. Benchmark showdown -- real numbers from demo_5_benchmark.py
 * ------------------------------------------------------------------ */
const BARS = [
  { label: 'Pessimistic', value: 27.3, color: SKY },
  { label: 'Optimistic', value: 81.9, color: AMBER },
  { label: 'Redis', value: 938.1, color: GREEN },
]
export function BenchmarkChart() {
  const max = 938.1
  const chartH = 96
  const baseY = 128
  return (
    <Frame>
      <text x={200} y={18} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700" letterSpacing=".08em">
        REQUESTS / SECOND -- 40 BUYERS, 5 UNITS
      </text>
      {BARS.map((b, i) => {
        const x = 70 + i * 110
        // sqrt scale so all three bars stay legible on one axis -- Redis is
        // ~34x Pessimistic, and a linear scale would flatten the other two
        // to slivers.
        const h = Math.max(6, Math.sqrt(b.value / max) * chartH)
        return (
          <g key={b.label}>
            <rect x={x} y={baseY} width={44} height={h} rx={4} fill={b.color} opacity="0.9">
              <animate attributeName="height" from="0" to={h} dur="1.1s" begin={`${i * 0.15}s`} fill="freeze" calcMode="spline" keySplines="0.2 0.8 0.2 1" />
              <animate attributeName="y" from={baseY} to={baseY - h} dur="1.1s" begin={`${i * 0.15}s`} fill="freeze" calcMode="spline" keySplines="0.2 0.8 0.2 1" />
            </rect>
            <text x={x + 22} y={baseY - h - 8} textAnchor="middle" fill={INK} fontSize="12" fontWeight="900" opacity="0">
              <animate attributeName="opacity" from="0" to="1" begin={`${i * 0.15 + 0.9}s`} dur="0.4s" fill="freeze" />
              {b.value}
            </text>
            <text x={x + 22} y={baseY + 18} textAnchor="middle" fill={DIM} fontSize="9" fontWeight="700">
              {b.label}
            </text>
          </g>
        )
      })}
      <line x1={40} y1={baseY} x2={360} y2={baseY} stroke={LINE} />
      <text x={200} y={158} textAnchor="middle" fill={DIM} fontSize="10">
        All three sold exactly 5 -- zero oversold. The difference is entirely speed.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 6. Deadlock detection + wound-wait
 * ------------------------------------------------------------------ */
export function DeadlockDiagram() {
  const cyclePath = 'M120,50 L280,50 L280,120 L120,120 Z'
  return (
    <Frame>
      <g fontSize="11" fontWeight="800" textAnchor="middle">
        <circle cx={120} cy={50} r={20} fill="#201f1c" stroke={AMBER} strokeWidth="1.5" />
        <text x={120} y={54} fill={AMBER}>T1</text>
        <circle cx={280} cy={50} r={20} fill="#201f1c" stroke={SKY} strokeWidth="1.5" />
        <text x={280} y={54} fill={SKY}>T2</text>
        <rect x={100} y={106} width={40} height={28} rx={6} fill="#141a1f" stroke={LINE} />
        <text x={120} y={124} fill={INK} fontSize="10">R1</text>
        <rect x={260} y={106} width={40} height={28} rx={6} fill="#141a1f" stroke={LINE} />
        <text x={280} y={124} fill={INK} fontSize="10">R2</text>
      </g>

      <path d="M140,50 L260,50" stroke={LINE} strokeWidth="1.5" markerEnd="url(#arrow)" />
      <path d="M280,70 L280,106" stroke={LINE} strokeWidth="1.5" markerEnd="url(#arrow)" />
      <path d="M260,120 L140,120" stroke={LINE} strokeWidth="1.5" markerEnd="url(#arrow)" />
      <path d="M120,106 L120,70" stroke={LINE} strokeWidth="1.5" markerEnd="url(#arrow)" />
      <text x={200} y={44} textAnchor="middle" fill={DIM} fontSize="9">waits for</text>
      <text x={200} y={134} textAnchor="middle" fill={DIM} fontSize="9">waits for</text>

      <MovingDot path={cyclePath} dur="2.4s" color={GREEN} r={5} glow />

      <circle cx={280} cy={50} r={20} fill={RED} opacity="0">
        <animate attributeName="opacity" values="0;0;0.85;0" keyTimes="0;0.82;0.9;1" dur="3.6s" repeatCount="indefinite" />
      </circle>
      <text x={280} y={54} textAnchor="middle" fontSize="11" fontWeight="900" fill="#1c0a0a" opacity="0">
        <animate attributeName="opacity" values="0;0;1;0" keyTimes="0;0.82;0.9;1" dur="3.6s" repeatCount="indefinite" />
        ✕
      </text>

      <text x={200} y={150} textAnchor="middle" fill={DIM} fontSize="10">
        Postgres finds the cycle and picks a victim -- the younger transaction
      </text>
      <text x={200} y={164} textAnchor="middle" fill={RED} fontSize="10" fontWeight="700">
        (wound-wait) is aborted, T1 proceeds, nobody waits forever.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 7. Timestamp ordering + Thomas's Write Rule
 * ------------------------------------------------------------------ */
export function TimestampDiagram() {
  return (
    <Frame>
      <line x1={40} y1={80} x2={360} y2={80} stroke={LINE} strokeWidth="1.5" />
      <g fontSize="10" fontWeight="800" textAnchor="middle">
        <circle cx={110} cy={80} r={16} fill="#201f1c" stroke={SKY} strokeWidth="1.5" />
        <text x={110} y={84} fill={SKY}>T@5</text>
        <circle cx={230} cy={80} r={16} fill="#201f1c" stroke={AMBER} strokeWidth="1.5" />
        <text x={230} y={84} fill={AMBER}>T@12</text>
      </g>
      <text x={340} y={84} fill={DIM} fontSize="9">time →</text>

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.05;0.45;0.5;1" dur="4.4s" repeatCount="indefinite" />
        <path d="M110,64 L226,64" stroke={RED} strokeWidth="1.5" markerEnd="url(#arrow)" strokeDasharray="4 3" />
        <text x={168} y={54} textAnchor="middle" fill={RED} fontSize="10" fontWeight="700">
          late write, TS(T)&lt;W-TS -- ROLLBACK
        </text>
      </g>

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.55;0.95;0.98;1" dur="4.4s" repeatCount="indefinite" />
        <path d="M110,64 L226,64" stroke={AMBER} strokeWidth="1.5" markerEnd="url(#arrow)" strokeDasharray="4 3" />
        <text x={168} y={54} textAnchor="middle" fill={AMBER} fontSize="10" fontWeight="700">
          same case, but the write is already obsolete
        </text>
        <text x={168} y={120} textAnchor="middle" fill={GREEN} fontSize="10" fontWeight="700">
          Thomas's Write Rule: silently SKIP -- no rollback needed
        </text>
      </g>

      <text x={200} y={156} textAnchor="middle" fill={DIM} fontSize="10">
        Every transaction gets a birth timestamp; order is decided up front,
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 8. Multigranularity locking -- IS/IX/S/X up and down the tree
 * ------------------------------------------------------------------ */
export function GranularityDiagram() {
  return (
    <Frame>
      <g textAnchor="middle" fontSize="10" fontWeight="800">
        <rect x={160} y={20} width={80} height={26} rx={6} fill="#201f1c" stroke={LINE} />
        <text x={200} y={37} fill={INK}>database</text>

        <rect x={130} y={70} width={140} height={26} rx={6} fill="#201f1c" stroke={LINE} />
        <text x={200} y={87} fill={INK}>orders (table)</text>

        <rect x={155} y={120} width={90} height={26} rx={6} fill="#201f1c" stroke={LINE} />
        <text x={200} y={137} fill={INK}>row · sale_id=…</text>
      </g>

      <path d="M200,46 L200,70" stroke={LINE} markerEnd="url(#arrow)" />
      <path d="M200,96 L200,120" stroke={LINE} markerEnd="url(#arrow)" />

      <g fontSize="9" fontWeight="900" textAnchor="middle">
        <rect x={244} y={16} width={30} height={18} rx={4} fill={SKY} opacity="0">
          <animate attributeName="opacity" from="0" to="0.95" begin="0.1s" dur="0.3s" fill="freeze" />
        </rect>
        <text x={259} y={29} fill="#04202f" opacity="0">
          <animate attributeName="opacity" from="0" to="1" begin="0.1s" dur="0.3s" fill="freeze" />
          IS
        </text>

        <rect x={274} y={66} width={30} height={18} rx={4} fill={AMBER} opacity="0">
          <animate attributeName="opacity" from="0" to="0.95" begin="0.6s" dur="0.3s" fill="freeze" />
        </rect>
        <text x={289} y={79} fill="#2a1400" opacity="0">
          <animate attributeName="opacity" from="0" to="1" begin="0.6s" dur="0.3s" fill="freeze" />
          IX
        </text>

        <rect x={249} y={116} width={30} height={18} rx={4} fill={GREEN} opacity="0">
          <animate attributeName="opacity" from="0" to="0.95" begin="1.1s" dur="0.3s" fill="freeze" />
        </rect>
        <text x={264} y={129} fill="#04270f" opacity="0">
          <animate attributeName="opacity" from="0" to="1" begin="1.1s" dur="0.3s" fill="freeze" />
          X
        </text>
      </g>

      <text x={200} y={158} textAnchor="middle" fill={DIM} fontSize="10">
        Intent locks climb the tree first (IS/IX) so a table-level pause
      </text>
      <text x={200} y={172} textAnchor="middle" fill={DIM} fontSize="10">
        can coexist with thousands of independent row locks underneath.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 9. Indexing -- seq scan vs index scan
 * ------------------------------------------------------------------ */
function RowStrip({ y, highlightIndex, dur, begin }) {
  const n = 10
  const w = 300 / n
  return (
    <g>
      {Array.from({ length: n }).map((_, i) => (
        <rect key={i} x={50 + i * w} y={y} width={w - 3} height={16} rx={2} fill="#242320" />
      ))}
      <rect y={y} width={w - 3} height={16} rx={2} fill={GREEN}>
        <animate
          attributeName="x"
          values={Array.from({ length: highlightIndex + 1 }, (_, i) => 50 + i * w).join(';')}
          keyTimes={Array.from({ length: highlightIndex + 1 }, (_, i) => (i / highlightIndex || 0).toFixed(3)).join(';')}
          dur={dur}
          begin={begin}
          repeatCount="indefinite"
        />
      </rect>
    </g>
  )
}
export function IndexDiagram() {
  return (
    <Frame>
      <text x={200} y={20} textAnchor="middle" fill={DIM} fontSize="10" fontWeight="700" letterSpacing=".08em">
        WHERE sale_id = … AND created_at &gt; now() - interval '5s'
      </text>

      <text x={40} y={46} fill={RED} fontSize="10" fontWeight="800">Seq Scan</text>
      <RowStrip y={40} highlightIndex={9} dur="2.4s" begin="0s" />
      <text x={360} y={53} textAnchor="end" fill={DIM} fontSize="9">every row, in order</text>

      <text x={40} y={86} fill={GREEN} fontSize="10" fontWeight="800">Index Scan</text>
      <RowStrip y={80} highlightIndex={1} dur="2.4s" begin="0s" />
      <text x={360} y={93} textAnchor="end" fill={DIM} fontSize="9">B+ tree jumps straight there</text>

      <text x={200} y={130} textAnchor="middle" fill={DIM} fontSize="10">
        ix_orders_sale_id_created_at turns "scan the table" into
      </text>
      <text x={200} y={146} textAnchor="middle" fill={GREEN} fontSize="10" fontWeight="700">
        "look it up" -- same query, same result, radically less I/O.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 10. Recovery -- WAL undo/redo
 * ------------------------------------------------------------------ */
export function RecoveryDiagram() {
  const entries = ['BEGIN', 'UPDATE stock 5→4', 'INSERT order', 'COMMIT']
  return (
    <Frame>
      <text x={70} y={20} fill={DIM} fontSize="10" fontWeight="700" letterSpacing=".08em">WAL</text>
      {entries.map((e, i) => (
        <g key={e} opacity="0">
          <animate attributeName="opacity" values="0;0;1;1;1;0" keyTimes={`0;${(0.05 * i).toFixed(2)};${(0.05 * i + 0.05).toFixed(2)};0.75;0.95;1`} dur="4.6s" begin="0s" repeatCount="indefinite" />
          <rect x={40} y={30 + i * 24} width={140} height={18} rx={4} fill="#201f1c" stroke={LINE} />
          <text x={48} y={30 + i * 24 + 13} fill={INK} fontSize="9">{e}</text>
        </g>
      ))}

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;0" keyTimes="0;0.42;0.5;0.6" dur="4.6s" repeatCount="indefinite" />
        <text x={110} y={140} textAnchor="middle" fontSize="22">💥</text>
        <text x={110} y={158} textAnchor="middle" fill={RED} fontSize="10" fontWeight="800">CRASH mid-transaction</text>
      </g>

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;0.62;0.7;0.95;1" dur="4.6s" repeatCount="indefinite" />
        <path d="M240,40 L240,140" stroke={LINE} strokeDasharray="3 3" />
        {['redo BEGIN', 'redo UPDATE', 'undo INSERT', 'undo COMMIT?'].map((t, i) => (
          <text key={t} x={250} y={54 + i * 22} fill={SKY} fontSize="9">{t}</text>
        ))}
      </g>

      <g opacity="0">
        <animate attributeName="opacity" values="0;0;1;0" keyTimes="0;0.9;0.94;1" dur="4.6s" repeatCount="indefinite" />
        <text x={200} y={158} textAnchor="middle" fill={GREEN} fontSize="11" fontWeight="800">
          ✓ consistent state restored from the log
        </text>
      </g>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 11. Virtual waiting room -- admitted K at a time
 * ------------------------------------------------------------------ */
export function QueueDiagram() {
  const waiting = Array.from({ length: 9 })
  return (
    <Frame>
      <text x={90} y={18} textAnchor="middle" fill={DIM} fontSize="9" fontWeight="700" letterSpacing=".08em">
        REDIS ZSET
      </text>
      <text x={310} y={18} textAnchor="middle" fill={DIM} fontSize="9" fontWeight="700" letterSpacing=".08em">
        CHECKOUT
      </text>

      <rect x={172} y={44} width={16} height={90} fill="#201f1c" stroke={LINE} />
      <rect x={212} y={44} width={16} height={90} fill="#201f1c" stroke={LINE} />
      <PulseRect x={172} y={44} w={56} h={90} fill="none" dur="1s" />

      {waiting.map((_, i) => {
        const col = i % 3
        const row = Math.floor(i / 3)
        const startX = 60 + col * 22
        const startY = 40 + row * 26
        const wave = Math.floor(i / 3)
        return (
          <MovingDot
            key={i}
            path={`M${startX},${startY} L200,88 L340,88`}
            dur="2.7s"
            begin={`${wave * 0.9}s`}
            color={i % 3 === 0 ? AMBER : i % 3 === 1 ? SKY : GREEN}
            r={5}
          />
        )
      })}

      <text x={200} y={158} textAnchor="middle" fill={DIM} fontSize="10">
        Buyers queue in Redis, cheaply, and are let through the gate
      </text>
      <text x={200} y={172} textAnchor="middle" fill={AMBER} fontSize="10" fontWeight="700">
        3 at a time -- checkout only ever sees a trickle, never the herd.
      </text>
    </Frame>
  )
}

/* ------------------------------------------------------------------ *
 * 12. Intelligence layer -- demand forecast + bot detection
 * ------------------------------------------------------------------ */
export function IntelligenceDiagram() {
  const spark = 'M40,110 L70,96 L100,102 L130,80 L160,88 L190,58'
  const predicted = 'M190,58 L220,50 L250,54 L280,40'
  const normal = [
    [230, 40], [250, 60], [270, 45], [290, 70], [310, 55], [235, 70], [300, 42],
  ]
  const bots = [[262, 58], [270, 62], [255, 63]]
  return (
    <Frame>
      <text x={110} y={20} textAnchor="middle" fill={DIM} fontSize="9" fontWeight="700" letterSpacing=".08em">
        DEMAND FORECAST
      </text>
      <path d={spark} fill="none" stroke={SKY} strokeWidth="2" />
      <path d={predicted} fill="none" stroke={AMBER} strokeWidth="2" strokeDasharray="4 3">
        <animate attributeName="stroke-dashoffset" from="40" to="0" dur="1.6s" repeatCount="indefinite" />
      </path>
      <circle r={4} fill={AMBER}>
        <animateMotion path={predicted} dur="1.6s" repeatCount="indefinite" />
      </circle>
      <text x={40} y={128} fill={DIM} fontSize="9">gradient boosting, trained on real sales</text>

      <line x1={215} y1={0} x2={215} y2={170} stroke={LINE} strokeDasharray="2 4" />

      <text x={305} y={20} textAnchor="middle" fill={DIM} fontSize="9" fontWeight="700" letterSpacing=".08em">
        BOT DETECTION
      </text>
      {normal.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r={4} fill={GREEN} opacity="0.85" />
      ))}
      <ellipse cx={262} cy={61} rx={26} ry={16} fill="none" stroke={RED} strokeDasharray="4 3" strokeWidth="1.5">
        <animate attributeName="rx" values="20;28;20" dur="2.4s" repeatCount="indefinite" />
        <animate attributeName="ry" values="12;18;12" dur="2.4s" repeatCount="indefinite" />
      </ellipse>
      {bots.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r={4} fill={RED}>
          <animate attributeName="opacity" values="1;0.4;1" dur="1.3s" begin={`${i * 0.2}s`} repeatCount="indefinite" />
        </circle>
      ))}
      <text x={305} y={128} textAnchor="middle" fill={DIM} fontSize="9">
        Isolation Forest, unsupervised
      </text>

      <text x={200} y={156} textAnchor="middle" fill={DIM} fontSize="10">
        Neither model touches checkout -- they inform stock buffers and review queues.
      </text>
    </Frame>
  )
}
