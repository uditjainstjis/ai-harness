import { useEffect, useRef, useState } from 'react';
import { sceneFrames } from '../art/pixelPeople';
import { FLOOR, ROLES, ROOM_BY_ID, ROOMS } from '../domain/office';
import { isBusy, type Agent, type Bubble, type Flight, type OfficeState, type Room, type RoomId, type Task } from '../domain/types';

// Layout grid for desks inside a room.
const CELL_W = 64;
const CELL_H = 88;
const PAD_X = 20;
const HEADER = 52;
// Scene sprites are 18×32 px, drawn at an integer 2× scale.
const SPRITE_W = 36;
const SPRITE_H = 64;

function grid(room: Room) {
  const cols = Math.max(1, Math.floor((room.w - PAD_X * 2) / CELL_W));
  const rows = Math.max(1, Math.floor((room.h - HEADER - 6) / CELL_H));
  return { cols, rows };
}

/** Desk-top centre of a seat. The agent sits behind it; the name goes under it. */
function seat(room: Room, i: number) {
  const { cols } = grid(room);
  const left = room.x + Math.round((room.w - cols * CELL_W) / 2);
  return {
    x: left + (i % cols) * CELL_W + CELL_W / 2,
    y: room.y + HEADER + 44 + Math.floor(i / cols) * CELL_H,
  };
}

function center(id: RoomId) {
  const r = ROOM_BY_ID[id];
  return { x: r.x + r.w / 2, y: r.y + r.h / 2 };
}

/**
 * Moves a sprite toward its seat in whole-pixel steps. Positions go through the
 * SVG transform attribute: CSS px transforms inside a scaled SVG get multiplied
 * by the browser zoom in Chrome, which flings sprites off their desks.
 */
function useWalk(x: number, y: number, ms = 1600) {
  const [pos, setPos] = useState({ x, y });
  const last = useRef(pos);
  useEffect(() => {
    const origin = { ...last.current };
    if (origin.x === x && origin.y === y) return;
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      const p = { x: Math.round(origin.x + (x - origin.x) * e), y: Math.round(origin.y + (y - origin.y) * e) };
      last.current = p;
      setPos(p);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [x, y, ms]);
  return pos;
}

const STATUS_LABEL: Record<Agent['status'], string | undefined> = {
  idle: undefined,
  working: 'working',
  walking: 'walking',
  meeting: 'in meeting',
  thinking: 'thinking…',
};

export function Floor({
  state,
  selected,
  onSelect,
  live = false,
  mini = false,
}: {
  state: OfficeState;
  selected?: string;
  onSelect: (id?: string) => void;
  /** real runs: finished work lands in the Evidence Room as proven, nothing is "shipped" until a PR is opened */
  live?: boolean;
  /** the small preview on the Studio pages: speech is drawn larger so it reads at that size */
  mini?: boolean;
}) {
  // Stable per-room ordering so avatars don't shuffle every tick.
  const byRoom = new Map<RoomId, Agent[]>();
  for (const a of [...state.agents].sort((a, b) => a.id.localeCompare(b.id))) {
    const list = byRoom.get(a.at) ?? [];
    list.push(a);
    byRoom.set(a.at, list);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const a of state.agents) positions.set(a.id, seat(ROOM_BY_ID[a.at], (byRoom.get(a.at) ?? []).indexOf(a)));

  const openTasks = state.tasks.filter((t) => !t.done && !t.inTransit && t.kind !== 'approval');
  const shipped = state.tasks.filter((t) => t.kind === 'story' && t.done).length;

  return (
    <svg className="floor" viewBox={`0 0 ${FLOOR.w} ${FLOOR.h}`} shapeRendering="crispEdges" onClick={() => onSelect(undefined)}>
      <defs>
        <pattern id="path" width="32" height="32" patternUnits="userSpaceOnUse">
          <rect width="32" height="32" fill="#E8D8B0" />
          <rect width="16" height="16" fill="#E0CEA2" />
          <rect x="16" y="16" width="16" height="16" fill="#E0CEA2" />
        </pattern>
        <pattern id="wood" width="32" height="16" patternUnits="userSpaceOnUse">
          <rect width="32" height="16" fill="#E5C896" />
          <rect y="7" width="32" height="1" fill="#D4B27C" />
          <rect y="15" width="32" height="1" fill="#C9A66B" />
          <rect x="12" width="1" height="8" fill="#D4B27C" />
          <rect x="26" y="8" width="1" height="8" fill="#D4B27C" />
        </pattern>
      </defs>

      <rect width={FLOOR.w} height={FLOOR.h} fill="url(#path)" />

      {/* floors, walls, signs, desks (back layer) */}
      {ROOMS.map((room) => {
        const people = state.agents.filter((a) => a.at === room.id);
        return (
          <RoomBack
            key={room.id}
            room={room}
            headcount={people.length}
            working={people.filter((a) => isBusy(a.status)).length}
            tasks={openTasks.filter((t) => t.room === room.id).length}
          />
        );
      })}

      {/* people sit behind their desks */}
      {state.agents.map((a) => {
        const pos = positions.get(a.id)!;
        const task = a.taskId ? state.tasks.find((t) => t.id === a.taskId) : undefined;
        return <AgentSprite key={a.id} agent={a} x={pos.x} y={pos.y} task={task} selected={selected === a.id} onSelect={onSelect} />;
      })}

      {/* desk fronts, name plates and in-trays (front layer) */}
      {ROOMS.map((room) => (
        <RoomFront
          key={room.id}
          room={room}
          people={byRoom.get(room.id) ?? []}
          tasks={openTasks.filter((t) => t.room === room.id)}
          shipped={room.id === (live ? 'board' : 'server') ? shipped : undefined}
          shippedLabel={live ? 'proven' : 'shipped'}
        />
      ))}

      <FlightLayer flights={state.flights} positions={positions} scale={mini ? 1.6 : 1} />
      {state.bubbles && <BubbleLayer bubbles={state.bubbles} positions={positions} mini={mini} />}
    </svg>
  );
}

function RoomBack({ room, headcount, working, tasks }: { room: Room; headcount: number; working: number; tasks: number }) {
  return (
    <g>
      <rect x={room.x} y={room.y} width={room.w} height={room.h} fill="url(#wood)" />
      {/* 4px wall with a darker top edge */}
      <rect x={room.x} y={room.y} width={room.w} height={room.h} fill="none" stroke="#8B6F47" strokeWidth="4" />
      <rect x={room.x - 2} y={room.y - 2} width={room.w + 4} height="6" fill="#6E5536" />
      {/* signboard hangs on the top wall, clear of the desks */}
      <g transform={`translate(${room.x + 10}, ${room.y - 10})`}>
        <rect width={room.name.length * 8 + 34} height="28" fill="#FFF8E7" stroke="#1A1320" strokeWidth="2" />
        <rect x="4" y="4" width="10" height="20" fill={room.color} stroke="#1A1320" strokeWidth="1" />
        <text x="22" y="15" className="sign-name">
          {room.name}
        </text>
        <text x="22" y="24" className="sign-dept">
          {room.dept.toLowerCase()}
          {working > 0 ? ` · ${working} working` : ''}
        </text>
      </g>
      {/* counters sit bottom-left, clear of the signboard */}
      <g transform={`translate(${room.x + 10}, ${room.y + room.h - 30})`}>
        <Counter x={0} icon="person" value={headcount} title={`${headcount} agents in this room`} />
        <Counter x={38} icon="task" value={tasks} title={`${tasks} open tasks here`} />
      </g>
      <Plant x={room.x + room.w - 22} y={room.y + room.h - 30} />
    </g>
  );
}

function Counter({ x, icon, value, title }: { x: number; icon: 'person' | 'task'; value: number; title: string }) {
  return (
    <g transform={`translate(${x}, 0)`}>
      <title>{title}</title>
      <rect width="34" height="20" fill="#FFF8E7" stroke="#1A1320" strokeWidth="2" />
      {icon === 'person' ? (
        <g fill="#3D2E4A">
          <rect x="7" y="4" width="4" height="4" />
          <rect x="5" y="9" width="8" height="6" />
        </g>
      ) : (
        <g>
          <rect x="4" y="5" width="11" height="9" fill="#FFFDF5" stroke="#3D2E4A" strokeWidth="1.5" />
          <path d="M4 5 L9.5 10 L15 5" fill="none" stroke="#3D2E4A" strokeWidth="1.5" />
        </g>
      )}
      <text x="24" y="15" textAnchor="middle" className="counter-n">
        {value}
      </text>
    </g>
  );
}

function Plant({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x}, ${y})`}>
      <rect x="2" y="10" width="12" height="10" fill="#C96F4A" stroke="#1A1320" strokeWidth="1" />
      <rect x="4" y="0" width="4" height="10" fill="#6BCF7F" />
      <rect x="8" y="2" width="4" height="8" fill="#3E9E52" />
      <rect x="0" y="4" width="4" height="6" fill="#3E9E52" />
      <rect x="12" y="5" width="4" height="5" fill="#6BCF7F" />
    </g>
  );
}

function RoomFront({ room, people, tasks, shipped, shippedLabel = 'shipped' }: { room: Room; people: Agent[]; tasks: Task[]; shipped?: number; shippedLabel?: string }) {
  const { cols, rows } = grid(room);
  const seats = Array.from({ length: Math.max(cols * rows, people.length) }, (_, i) => seat(room, i));
  const shown = tasks.slice(0, 6);
  return (
    <g>
      {seats.map((d, i) => {
        const person = people[i];
        const busy = !!person && isBusy(person.status);
        return (
          <g key={i}>
            {/* desk */}
            <rect x={d.x - 22} y={d.y - 2} width="44" height="16" fill="#A87B4F" stroke="#1A1320" strokeWidth="2" />
            <rect x={d.x - 20} y={d.y} width="40" height="3" fill="#C99A62" />
            {/* monitor faces away from us, screen glow shows through when busy */}
            <rect x={d.x - 9} y={d.y - 12} width="18" height="11" fill="#3D2E4A" stroke="#1A1320" strokeWidth="1" />
            <rect x={d.x - 7} y={d.y - 10} width="14" height="2" fill={busy ? '#4ECDC4' : '#6B5878'} />
            {person && (
              <text x={d.x} y={d.y + 27} textAnchor="middle" className="name-plate">
                {person.name}
              </text>
            )}
          </g>
        );
      })}
      {shown.map((t, i) => (
        <g key={t.id} transform={`translate(${room.x + room.w - 50 - i * 30}, ${room.y + room.h - 24})`}>
          <title>{`${t.id} · ${t.title}${t.stage ? ` · ${t.stage}` : ''}${t.rework ? ` · rework (${t.rework})` : ''}`}</title>
          <Envelope kind={t.kind} rework={!!t.rework} />
          <text x="11" y="-3" textAnchor="middle" className="tray-id">
            {t.id.replace('T-', '')}
          </text>
        </g>
      ))}
      {tasks.length > shown.length && (
        <text x={room.x + room.w - 56 - shown.length * 30} y={room.y + room.h - 12} textAnchor="end" className="tray-id">
          +{tasks.length - shown.length}
        </text>
      )}
      {shipped !== undefined && shipped > 0 && (
        <g transform={`translate(${room.x + 86}, ${room.y + room.h - 30})`}>
          <rect width="104" height="20" fill="#6BCF7F" stroke="#1A1320" strokeWidth="2" />
          <text x="52" y="14" textAnchor="middle" className="shipped">
            {shipped} {shippedLabel}
          </text>
        </g>
      )}
    </g>
  );
}

const ENVELOPE_FILL: Record<Task['kind'], string> = {
  doc: '#FFFDF5',
  story: '#A8E6E0',
  hire: '#FFD0B5',
  approval: '#FFEC99',
};

function Envelope({ kind, rework, scale = 1 }: { kind: Task['kind']; rework?: boolean; scale?: number }) {
  return (
    <g transform={scale === 1 ? undefined : `scale(${scale})`}>
      <rect width="22" height="14" fill={rework ? '#FFB4B4' : ENVELOPE_FILL[kind]} stroke="#1A1320" strokeWidth="2" />
      <path d="M1 1 L11 8 L21 1" fill="none" stroke="#1A1320" strokeWidth="1.5" shapeRendering="auto" />
    </g>
  );
}

function AgentSprite({
  agent,
  x,
  y,
  task,
  selected,
  onSelect,
}: {
  agent: Agent;
  x: number;
  y: number;
  task?: Task;
  selected: boolean;
  onSelect: (id?: string) => void;
}) {
  const frames = sceneFrames(agent.name);
  const pos = useWalk(Math.round(x), Math.round(y));
  const label = STATUS_LABEL[agent.status];
  return (
    <g
      className={`agent status-${agent.status}`}
      transform={`translate(${pos.x}, ${pos.y})`}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(agent.id);
      }}
    >
      <title>{`${agent.name} · ${ROLES[agent.role].label} · ${agent.status}${task ? ` · ${task.title}` : ''}`}</title>
      {selected && <rect x={-SPRITE_W / 2 - 4} y={-50} width={SPRITE_W + 8} height="68" className="select-box" />}
      {/* All three frames stay mounted so walking never waits on an image decode. */}
      <g className="sprite">
        {frames.map((href, i) => (
          <image key={i} href={href} x={-SPRITE_W / 2} y={-46} width={SPRITE_W} height={SPRITE_H} className={`f${i}`} />
        ))}
      </g>
      {label && (
        <g transform="translate(0, -58)" className="tag">
          <rect x={-label.length * 3.5 - 6} y="-12" width={label.length * 7 + 12} height="16" className={`tag-box tag-${agent.status}`} />
          <text y="0" textAnchor="middle" className="tag-text">
            {label}
          </text>
          {task && isBusy(agent.status) && (
            <g transform={`translate(${-label.length * 3.5 - 6}, 4)`}>
              <rect width={label.length * 7 + 12} height="4" fill="#1A1320" />
              <rect x="1" y="1" width={Math.max(0, (label.length * 7 + 10) * task.progress)} height="2" fill="#6BCF7F" />
            </g>
          )}
          <rect x="-2" y="4" width="4" height="4" className={`tag-tail tag-${agent.status}`} />
        </g>
      )}
    </g>
  );
}

function FlightLayer({ flights, positions, scale = 1 }: { flights: Flight[]; positions: Map<string, { x: number; y: number }>; scale?: number }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!flights.length) return;
    let raf = 0;
    const loop = () => {
      setNow(performance.now());
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [flights.length]);

  // startWall is Date.now()-based (the live engine) or performance-based (the simulation)
  const wall = (f: Flight) => (f.startWall > 1e12 ? Date.now() : now);
  return (
    <g>
      {flights.map((f) => {
        const clock = wall(f);
        if (clock < f.startWall) return null; // staggered hand-offs wait their turn
        const a = (f.fromAgent && positions.get(f.fromAgent)) || center(f.from);
        const b = (f.toAgent && positions.get(f.toAgent)) || center(f.to);
        const from = { x: a.x, y: a.y - 30 };
        const to = { x: b.x, y: b.y - 30 };
        const lift = Math.max(40, Math.hypot(to.x - from.x, to.y - from.y) * 0.25);
        const c = { x: (from.x + to.x) / 2, y: Math.min(from.y, to.y) - lift };
        const t = Math.min(1, (clock - f.startWall) / f.durationMs);
        const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        const px = Math.round((1 - e) * (1 - e) * from.x + 2 * (1 - e) * e * c.x + e * e * to.x);
        const py = Math.round((1 - e) * (1 - e) * from.y + 2 * (1 - e) * e * c.y + e * e * to.y);
        const caption = `${f.label.replace('T-', '#')} ${f.caption}`;
        const landed = clock - f.startWall - f.durationMs;         // > 0: the ring where it arrived
        const d = `M${from.x},${from.y} Q${c.x},${c.y} ${to.x},${to.y}`;
        return (
          <g key={f.id}>
            <path d={d} className="flight-path" shapeRendering="auto" />
            <path d={d} className="flight-path-live" shapeRendering="auto" pathLength={100} strokeDasharray={`${Math.round(e * 100)} 100`} />
            {landed > 0 ? (
              <circle cx={to.x} cy={to.y} r={10 + (landed / 800) * 46} className="flight-ring" shapeRendering="auto" style={{ opacity: Math.max(0, 1 - landed / 800) }} />
            ) : (
              <g transform={`translate(${px}, ${py}) scale(${scale})`}>
                <circle r="17" className="flight-glow" shapeRendering="auto" />
                <g transform="translate(-16, -10) scale(1.5)">
                  <Envelope kind={f.kind} />
                </g>
                <rect x={-caption.length * 3.5 - 5} y="14" width={caption.length * 7 + 10} height="16" className="flight-chip" />
                <text y="26" textAnchor="middle" className="flight-caption">
                  {caption}
                </text>
              </g>
            )}
          </g>
        );
      })}
    </g>
  );
}

/** Wraps speech into lines of at most n characters (pixel font, fixed width). */
function wrap(text: string, n: number) {
  const lines: string[] = [];
  let cur = '';
  for (const w of text.split(' ')) {
    if ((cur + ' ' + w).trim().length > n && cur) {
      lines.push(cur);
      cur = w;
    } else cur = (cur + ' ' + w).trim();
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 3);
}

/** What people are saying right now: one bubble per person, the newest wins; the thought cloud while the model
 *  answers. Neighbours' bubbles are stacked instead of drawn over each other. */
function BubbleLayer({ bubbles, positions, mini }: { bubbles: Bubble[]; positions: Map<string, { x: number; y: number }>; mini: boolean }) {
  const now = Date.now();
  const shown = new Map<string, Bubble>();
  for (const b of bubbles) {
    if (now < b.start || now >= b.until) continue;
    const cur = shown.get(b.agentId);
    if (!cur || (cur.tone === 'think' && b.tone !== 'think') || (b.tone !== 'think' && b.start > cur.start)) shown.set(b.agentId, b);
  }
  const fs = mini ? 27 : 17;
  const cw = fs * 0.5;
  const k = mini ? 1.5 : 1;

  type Box = { b: Bubble; p: { x: number; y: number }; x: number; y: number; w: number; h: number; tip: number; below: boolean; lines: string[] };
  const boxes: Box[] = [];
  // older bubbles keep their place; newer ones move out of the way
  for (const b of [...shown.values()].sort((a, c) => a.start - c.start)) {
    const p = positions.get(b.agentId);
    if (!p) continue;
    const tipY = p.y - 74;
    const think = b.tone === 'think';
    const lines = think ? [] : wrap(b.text, mini ? 24 : 30);
    const w = think ? (b.text ? 86 : 60) * k : Math.max(...lines.map((l) => l.length)) * cw + 18;
    const h = think ? 30 * k : lines.length * fs * 0.95 + 12;
    const x = Math.min(Math.max(think ? p.x + 6 : p.x - w / 2, 4), FLOOR.w - w - 4);
    // places to try, in order: above the speaker, under the desk, then further up or down; the first free one wins
    const above = tipY - (think ? 14 : 10) - h;
    const under = p.y + 26;
    const slots: { y: number; below: boolean }[] = [];
    for (let i = 0; i < 4; i++) {
      if (above - i * (h + 6) >= 4) slots.push({ y: above - i * (h + 6), below: false });
      if (!think && under + i * (h + 6) + h <= FLOOR.h - 4) slots.push({ y: under + i * (h + 6), below: true });
    }
    const free = (c: { y: number }) => !boxes.some((o) => x < o.x + o.w + 4 && o.x < x + w + 4 && c.y < o.y + o.h + 4 && o.y < c.y + h + 4);
    const slot = slots.find(free) ?? slots[0] ?? { y: Math.max(4, above), below: false };
    const { y, below } = slot;
    const tip = below ? p.y + 16 : tipY;
    boxes.push({ b, p, x, y, w, h, tip, below, lines });
  }

  return (
    <g className="bubbles">
      {boxes.map(({ b, p, x, y, w, h, tip, below, lines }) => {
        if (b.tone === 'think') {
          return (
            <g key={b.agentId + '-think'} className="think">
              <circle cx={p.x + 4} cy={p.y - 70} r={mini ? 5 : 3} className="think-puff" />
              <circle cx={p.x + 10} cy={p.y - 80} r={mini ? 7 : 5} className="think-puff" />
              <rect x={x} y={y} width={w} height={h} rx={h / 2} className="think-cloud" shapeRendering="auto" />
              {[0, 1, 2].map((i) => (
                <circle key={i} cx={x + h * 0.55 + i * h * 0.45} cy={y + h / 2} r={h * 0.12} className={`think-dot d${i}`} shapeRendering="auto" />
              ))}
              {b.text && (
                <text x={x + w - h * 0.35} y={y + h / 2 + fs * 0.32} textAnchor="end" className="think-secs" style={{ fontSize: fs }}>
                  {b.text}
                </text>
              )}
            </g>
          );
        }
        // the tail reaches from the bubble's nearest edge to the speaker, even when the bubble was moved aside
        const edge = below || y > tip ? y + 1 : y + h - 1;
        const tx = Math.min(Math.max(p.x, x + 10), x + w - 10);
        const leaving = b.until - now < 450;
        return (
          <g key={b.id} className={`bubble tone-${b.tone}${leaving ? ' leaving' : ''}`} style={{ transformOrigin: `${p.x}px ${tip}px` }}>
            <rect x={x + 3} y={y + 3} width={w} height={h} className="bubble-shadow" />
            <path d={`M${tx - 7},${edge} L${p.x},${tip} L${tx + 7},${edge}`} className="bubble-tail" />
            <rect x={x} y={y} width={w} height={h} className="bubble-box" />
            <path d={`M${tx - 6},${edge} L${tx + 6},${edge}`} className="bubble-seam" />
            {lines.map((l, i) => (
              <text key={i} x={x + 9} y={y + 6 + (i + 0.78) * fs * 0.95} className="bubble-text" style={{ fontSize: fs }}>
                {l}
              </text>
            ))}
          </g>
        );
      })}
    </g>
  );
}
