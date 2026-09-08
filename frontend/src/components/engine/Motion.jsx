/**
 * Small SVG animation primitives shared by every diagram in pages/EngineRoom.jsx.
 *
 * Everything here is plain SMIL (<animate>/<animateMotion>/<animateTransform>)
 * rather than a JS animation library or React state -- these are decorative,
 * always-looping illustrations, not interactive controls, so there is
 * nothing to drive from React state and no library weight worth paying for
 * twelve diagrams that just need to loop forever once mounted.
 */

/** A colored dot that travels along an SVG path, looping forever. */
export function MovingDot({ path, dur = '2.4s', begin = '0s', color = '#f97316', r = 7, glow }) {
  return (
    <circle r={r} fill={color} opacity={0.95} filter={glow ? 'url(#dotGlow)' : undefined}>
      <animateMotion path={path} dur={dur} begin={begin} repeatCount="indefinite" rotate="auto" />
    </circle>
  )
}

/** Shared <defs> (glow filter, arrowhead marker) -- include once per <svg>. */
export function EngineDefs() {
  return (
    <defs>
      <filter id="dotGlow" x="-60%" y="-60%" width="220%" height="220%">
        <feGaussianBlur stdDeviation="2.2" result="blur" />
        <feMerge>
          <feMergeNode in="blur" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="currentColor" />
      </marker>
    </defs>
  )
}

/** A rounded "chip" rect that pulses (opacity + outline) on a loop -- lock
 * clamps, grant flashes. Animates opacity/stroke rather than a scale
 * transform so it never needs an SVG transform-origin (browser support for
 * anchoring a scale at an element's own center, rather than the SVG
 * viewport's origin, is inconsistent for SMIL-driven transforms). */
export function PulseRect({ x, y, w, h, rx = 8, fill, stroke, dur = '2s', begin = '0s' }) {
  return (
    <rect x={x} y={y} width={w} height={h} rx={rx} fill={fill} stroke={stroke} strokeWidth={0}>
      <animate attributeName="opacity" values="0.7;0.7;1;0.7" keyTimes="0;0.55;0.65;1" dur={dur} begin={begin} repeatCount="indefinite" />
      <animate attributeName="stroke-width" values="0;0;3;0" keyTimes="0;0.55;0.65;1" dur={dur} begin={begin} repeatCount="indefinite" />
    </rect>
  )
}
