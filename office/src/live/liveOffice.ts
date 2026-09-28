/**
 * The office driven by REAL Pramana runs instead of the simulation.
 *
 * The floor shows what is happening now: every issue being worked on gets its own developer (the
 * model), who walks in from the Issue Desk, works at a desk on the Engineering Floor, carries the fix
 * to the Proof Lab while the harness runs the checks on the original and the patched code, goes to the
 * Review Room, and finally hands the verified fix to you in the Evidence Room before leaving. The
 * specialists (setup, triage, localization, reproduction, proof, blind test, review, release) light up
 * when the harness is doing their step. Everything is driven by the events the run really emits.
 */
import { ROLES, ROOMS } from '../domain/office';
import type { Agent, Flight, Handoff, LogEntry, OfficeState, Role, RoomId, Task } from '../domain/types';

export interface RunSummary {
  id: string;
  title?: string;
  status: string;
  verdict?: string | null;
  calls?: number | null;
  tokens?: number | null;
  elapsed_s?: number | null;
  created?: number;
  issue?: number | null;
  repo?: string;
}

export interface OpenIssue {
  number: number;
  title: string;
  labels?: string[];
}

/** What the brief form shows after a submit. */
export type BriefReply =
  | { kind: 'started'; text: string }
  | { kind: 'pick'; repo: string; issues: OpenIssue[]; preselect: number[] }
  | { kind: 'error'; text: string };

interface Ev {
  kind: string;
  at?: number;
  name?: string;
  message?: string;
  status?: string;
  result?: string;
  size?: string;
  plan?: string;
  strength?: string;
  accepted?: boolean | string;
  verdict?: string;
  url?: string;
  ok?: boolean;
  total_tokens?: number | string;
  [k: string]: unknown;
}

type Stage = 'arrive' | 'setup' | 'intake' | 'localize' | 'reproduce' | 'fix' | 'verify' | 'blind' | 'review' | 'pr' | 'done';
type WorkStage = Exclude<Stage, 'arrive' | 'done'>;

const STAFF: { id: string; name: string; role: Role; room: RoomId }[] = [
  { id: 'you', name: 'You', role: 'client', room: 'board' },
  { id: 'intake', name: 'Ira', role: 'pm', room: 'product' },
  { id: 'env', name: 'Eli', role: 'hr', room: 'hr' },
  { id: 'loc', name: 'Lina', role: 'architect', room: 'architecture' },
  { id: 'repro', name: 'Ravi', role: 'designer', room: 'design' },
  { id: 'review', name: 'Tara', role: 'scrum', room: 'war' },
  { id: 'proof', name: 'Priya', role: 'qa', room: 'qa' },
  { id: 'blind', name: 'Bo', role: 'perf', room: 'perf' },
  { id: 'release', name: 'Dev', role: 'devops', room: 'server' },
];
const DEV_NAMES = ['Arjun', 'Maya', 'Kabir', 'Zoya', 'Neel', 'Isha', 'Omar', 'Sara', 'Vik', 'Anya', 'Rohan', 'Lea', 'Kiran', 'Tanvi'];
/** Where each step happens, which specialist does it, and where the developer stands meanwhile. */
export const STAGE: Record<WorkStage, { room: RoomId; worker: string; devAt: RoomId; caption: string; label: string }> = {
  setup: { room: 'hr', worker: 'env', devAt: 'engineering', caption: 'installing the project', label: 'Setup' },
  intake: { room: 'product', worker: 'intake', devAt: 'engineering', caption: 'sizing the issue', label: 'Triage' },
  localize: { room: 'architecture', worker: 'loc', devAt: 'engineering', caption: 'finding the code', label: 'Localize' },
  reproduce: { room: 'design', worker: 'repro', devAt: 'engineering', caption: 'reproducing the bug', label: 'Reproduce' },
  fix: { room: 'engineering', worker: 'DEV', devAt: 'engineering', caption: 'writing the fix', label: 'Fix' },
  verify: { room: 'qa', worker: 'proof', devAt: 'qa', caption: 'proving it: original vs patched', label: 'Proof' },
  blind: { room: 'perf', worker: 'blind', devAt: 'perf', caption: 'blind regression test', label: 'Blind test' },
  review: { room: 'war', worker: 'review', devAt: 'war', caption: 'review', label: 'Review' },
  pr: { room: 'server', worker: 'release', devAt: 'board', caption: 'opening the pull request', label: 'Pull request' },
};
const ACTIVE = new Set(['running', 'preparing', 'starting', 'queued']);
const LINGER_MS = 30_000; // a finished issue stays in the Evidence Room this long, then its developer leaves
const WALK_MS = 1700;

/** Renames the rooms and roles to what they really are in Pramana (the art stays the same). */
export function applyLiveLabels() {
  const names: Partial<Record<RoomId, [string, string]>> = {
    reception: ['Issue Desk', 'Intake'],
    product: ['Triage', 'Intake'],
    architecture: ['Localization', 'Analysis'],
    design: ['Reproduction Lab', 'Reproduction'],
    hr: ['Environment', 'Setup'],
    engineering: ['Engineering Floor', 'Fixing'],
    war: ['Review Room', 'Review'],
    board: ['Evidence Room', 'Evidence'],
    qa: ['Proof Lab', 'Verification'],
    perf: ['Blind Test Lab', 'Verification'],
    server: ['Release', 'Pull requests'],
  };
  for (const r of ROOMS) {
    const v = names[r.id];
    if (v) [r.name, r.dept] = v;
  }
  const labels: Partial<Record<Role, string>> = {
    client: 'You (issue owner)',
    pm: 'Intake & triage',
    hr: 'Environment setup',
    architect: 'Localizer',
    designer: 'Reproducer (runs the issue code)',
    scrum: 'Reviewer',
    fullstack: 'Developer (the model)',
    qa: 'Proof gate (original vs patched)',
    perf: 'Blind test writer',
    devops: 'Release (pull requests)',
  };
  for (const [k, v] of Object.entries(labels)) ROLES[k as Role].label = v;
  ROLES.scrum.room = 'war';
}

export interface Track {
  run: RunSummary;
  task: Task;
  devId: string;
  stage: Stage;
  worker: string;
  cursor: number;
  fetching: boolean;
  final: boolean;
  tokens: number;
  endedAt?: number;
  thinkingUntil: number;
  verdict?: string;
}

export class LiveOffice {
  private s: OfficeState;
  private view: OfficeState;
  private subs = new Set<() => void>();
  readonly tracks = new Map<string, Track>();
  private seen = new Set<string>();
  private timers: number[] = [];
  private walkEnds = new Map<string, { until: number; then: Agent['status'] }>();
  private logId = 0;
  private handoffId = 0;
  private flightId = 0;
  private devN = 0;
  private first = true;
  totals = { verified: 0, other: 0 };
  offline = false;

  constructor(private base = '') {
    this.s = this.fresh();
    this.view = { ...this.s };
  }

  private fresh(): OfficeState {
    return {
      tick: 0,
      day: 1,
      phase: 'idle',
      tokensUsed: 0,
      agents: STAFF.map((p) => this.agent(p.id, p.name, p.role, p.room)),
      tasks: [],
      flights: [],
      handoffs: [],
      approvals: [],
      log: [],
      reports: [],
      paused: false,
      speed: 1,
      autoApprove: true,
    };
  }

  private agent(id: string, name: string, role: Role, room: RoomId): Agent {
    return { id, name, role, home: room, at: room, status: 'idle', route: [], nextMoveAt: 0, hiredDay: 1, tasksDone: 0, tokensUsed: 0 };
  }

  // ------------------------------------------------------------ same surface as OfficeSim
  subscribe = (fn: () => void) => {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  };
  getState = () => this.view;
  start() {
    if (this.timers.length) return;
    this.poll();
    this.timers.push(window.setInterval(() => this.poll(), 1200));
    this.timers.push(window.setInterval(() => this.tick(), 400));
  }
  stop() {
    this.timers.forEach((t) => clearInterval(t));
    this.timers = [];
  }
  setPaused() {}
  stepOnce() {}
  setSpeed() {}
  setAutoApprove() {}
  approve() {}
  requestChanges() {}
  requestReport() {}
  reset() {
    this.stop();
    this.tracks.clear();
    this.seen.clear();
    this.s = this.fresh();
    this.first = true;
    this.start();
  }

  // ------------------------------------------------------------ handing over work (the brief form)
  /** An issue link, plain words plus a repository, or just a repository (then you pick its open issues). */
  async submitIssue(issue: string, repo: string): Promise<BriefReply> {
    try {
      const r = await fetch(`${this.base}/api/runs`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ prompt: issue, repo }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error) return { kind: 'error', text: j.error || `Pramana answered ${r.status}` };
      if (j.select_issues) return { kind: 'pick', repo: j.repo, issues: j.issues || [], preselect: j.preselect || [] };
      this.poll();
      return { kind: 'started', text: 'Handed to the Issue Desk. A developer is on the way.' };
    } catch (e) {
      return { kind: 'error', text: `Could not reach Pramana (${String(e)})` };
    }
  }

  async startIssues(repo: string, numbers: number[]): Promise<BriefReply> {
    try {
      const r = await fetch(`${this.base}/api/batch`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ repo, numbers, want_pr: false, auto_pr: false }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.error) return { kind: 'error', text: j.error || `Pramana answered ${r.status}` };
      this.poll();
      return { kind: 'started', text: `${numbers.length} issue${numbers.length === 1 ? '' : 's'} handed over. Developers are on the way.` };
    } catch (e) {
      return { kind: 'error', text: `Could not reach Pramana (${String(e)})` };
    }
  }

  /** Kept for the simulation's form signature. */
  submitBrief(repo: string, brief: string) {
    void this.submitIssue(brief, repo);
  }

  // ------------------------------------------------------------ following the runs
  private async poll() {
    let runs: RunSummary[];
    try {
      runs = await (await fetch(`${this.base}/api/runs`)).json();
      this.offline = false;
    } catch {
      this.offline = true;
      this.emit();
      return;
    }
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const done = runs.filter((r) => !ACTIVE.has(r.status) && (r.created ?? 0) * 1000 >= today.getTime());
    this.totals = { verified: done.filter((r) => r.verdict === 'verified').length, other: done.filter((r) => r.verdict !== 'verified').length };
    for (const run of runs.slice(0, 60)) {
      const t = this.tracks.get(run.id);
      if (t) {
        t.run = run;
        if (!ACTIVE.has(run.status) && t.stage !== 'done') this.pull(t, true);
        continue;
      }
      if (this.seen.has(run.id)) continue;
      this.seen.add(run.id);
      // the floor is "now": runs still going, or started after this page opened
      if (ACTIVE.has(run.status) || (!this.first && (run.created ?? 0) * 1000 > Date.now() - 60_000)) this.track(run);
    }
    this.first = false;
    for (const t of this.tracks.values()) if (t.stage !== 'done') this.pull(t);
    this.emit();
  }

  private track(run: RunSummary) {
    const n = this.devN++;
    const label = run.issue ? `#${run.issue}` : `R${n + 1}`;
    const devId = `dev-${run.id}`;
    const dev = this.agent(devId, `${DEV_NAMES[n % DEV_NAMES.length]} ${label}`, 'fullstack', 'engineering');
    this.s.agents.push(dev);
    if (!this.first) {                     // a new issue: the developer appears at the Issue Desk and walks in
      dev.at = 'reception';
      window.setTimeout(() => this.walk(dev, 'engineering'), 450);
    }                                      // opening the page mid-run: everyone is already where they work
    const title = (run.title || 'issue').replace(/^#\d+\s*/, '').slice(0, 70);
    const task: Task = {
      id: this.cardId(label),
      title: `${label} ${title}`,
      kind: 'story',
      phase: 'build',
      role: 'fullstack',
      room: 'reception',
      inTransit: false,
      owner: devId,
      bugs: 0,
      perfIssues: 0,
      progress: 0,
      work: 1,
      done: false,
      createdTick: this.s.tick,
      trail: ['reception'],
    };
    this.s.tasks.push(task);
    this.tracks.set(run.id, { run, task, devId, stage: 'arrive', worker: 'you', cursor: 0, fetching: false, final: false, tokens: 0, thinkingUntil: 0 });
    this.log(`${dev.name} picked up ${task.title}`, 'hire', 'reception');
  }

  /** Short polls, not open streams: browsers allow only 6 connections per host and a batch can run 10 issues. */
  private async pull(t: Track, last = false) {
    if (t.fetching || (t.final && !last)) return;
    t.fetching = true;
    try {
      const r = await (await fetch(`${this.base}/api/runs/${t.run.id}/events.json?since=${t.cursor}`)).json();
      const catchingUp = r.events.length > 25; // a run already under way: jump to where it is, no parade of hand-offs
      for (const ev of r.events as Ev[]) this.onEvent(t, ev, !catchingUp);
      t.cursor = r.next;
      if (!ACTIVE.has(r.status) && r.events.length === 0) {
        t.final = true;
        if (t.stage !== 'done') this.finish(t, t.run.verdict || r.status);
      }
    } catch {
      /* next tick */
    } finally {
      t.fetching = false;
      this.emit();
    }
  }

  private onEvent(t: Track, ev: Ev, animate: boolean) {
    const k = ev.kind;
    if (k === 'llm') {
      t.thinkingUntil = Date.now() + 2500;
      if (ev.total_tokens != null) this.addTokens(t, Number(ev.total_tokens) || 0);
    } else if (k === 'wait') t.thinkingUntil = Date.now() + 6000;
    else if (k === 'stage' && ev.name === 'setup') this.go(t, 'setup', animate);
    else if (k === 'phase' && ev.name) {
      const map: Record<string, WorkStage> = { intake: 'intake', localize: 'localize', reproduce: 'reproduce', fix: 'fix', verify: 'verify', review: 'review' };
      if (map[ev.name]) this.go(t, map[ev.name], animate);
    } else if (k === 'triage') this.log(`${t.task.title}: sized ${ev.size ?? '?'}, ${ev.plan ?? ''}`, 'info', 'product', animate);
    else if (k === 'verify' || k === 'checkpoint') {
      this.go(t, 'verify', animate);
      const ok = ev.accepted === true || ev.accepted === 'True';
      if (ok || k === 'verify') this.log(`${t.task.title}: proof ${ev.strength ?? '?'}${ok ? ', accepted' : ''}`, ok ? 'done' : 'info', 'qa', animate);
    } else if (k === 'review') this.go(t, 'review', animate);
    else if (k.startsWith('independent')) this.go(t, 'blind', animate);
    else if (k === 'github' || k === 'pr') {
      this.go(t, 'pr', animate);
      this.log(`${t.task.title}: ${ev.ok === false ? 'pull request failed' : `pull request ${ev.url ?? ''}`}`, ev.ok === false ? 'warn' : 'done', 'server', animate);
    } else if ((k === 'run' && ev.status === 'end') || k === 'done') this.finish(t, String(ev.result || ev.status || ''));
  }

  private go(t: Track, stage: WorkStage, animate: boolean) {
    if (t.stage === 'done' || t.stage === stage) return;
    const st = STAGE[stage];
    this.handOver(t, st.room, st.worker, st.caption, animate);
    t.stage = stage;
    t.worker = st.worker;
    const dev = this.agentById(t.devId);
    if (dev && dev.at !== st.devAt) {
      if (animate) this.walk(dev, st.devAt);
      else dev.at = st.devAt;
    }
  }

  private finish(t: Track, result: string) {
    if (t.stage === 'done') return;
    const ok = result === 'verified';
    this.handOver(t, 'board', 'you', ok ? 'verified fix + evidence' : `ended: ${result || 'no proof'}`, true);
    t.stage = 'done';
    t.verdict = result;
    t.worker = 'you';
    t.endedAt = Date.now();
    t.task.done = ok;
    t.task.stage = 'deploy';
    t.task.progress = 1;
    const dev = this.agentById(t.devId);
    if (dev) {
      if (ok) dev.tasksDone += 1;
      this.walk(dev, 'board');
    }
    const secs = t.run.elapsed_s ? ` in ${Math.round(t.run.elapsed_s)} s` : '';
    this.log(`${t.task.title}: ${ok ? 'VERIFIED' : result}${secs}${t.run.calls != null ? ` · ${t.run.calls} model calls` : ''}`, ok ? 'done' : 'warn', 'board', true);
  }

  /** Tokens arrive as a running total; only the model spends them, and the model is the developer. */
  private addTokens(t: Track, total: number) {
    const delta = Math.max(0, total - t.tokens);
    if (!delta) return;
    t.tokens = total;
    const dev = this.agentById(t.devId);
    if (dev) dev.tokensUsed += delta;
    this.s.tokensUsed += delta;
  }

  // ------------------------------------------------------------ animation plumbing
  private walk(a: Agent, room: RoomId) {
    a.at = room;
    a.status = 'walking';
    this.walkEnds.set(a.id, { until: Date.now() + WALK_MS, then: 'idle' });
  }

  private handOver(t: Track, room: RoomId, worker: string, caption: string, animate: boolean) {
    const from = t.task.room;
    if (animate && from !== room) {
      const now = Date.now();
      const fromAgent = t.worker === 'DEV' ? t.devId : t.worker;
      const toAgent = worker === 'DEV' ? t.devId : worker;
      const f: Flight = {
        id: `F${this.flightId++}`,
        taskId: t.task.id,
        label: t.task.id.replace('T-', ''),
        caption,
        kind: 'story',
        from,
        to: room,
        fromAgent,
        toAgent,
        landAt: this.s.tick + 3,
        startWall: now,
        durationMs: 1500,
      };
      this.s.flights = [...this.s.flights, f];
      const fromName = this.agentById(fromAgent)?.name ?? 'Issue Desk';
      const toName = this.agentById(toAgent)?.name ?? room;
      const h: Handoff = { id: this.handoffId++, tick: this.s.tick, day: this.s.day, taskId: t.task.id, title: t.task.title, fromName, toName, fromRoom: from, toRoom: room };
      this.s.handoffs = [h, ...this.s.handoffs].slice(0, 200);
      this.log(`${fromName} → ${toName}: ${t.task.title} (${caption})`, 'move', room, true);
    }
    t.task.room = room;
    if (t.task.trail[t.task.trail.length - 1] !== room) t.task.trail = [...t.task.trail, room];
  }

  private tick() {
    const now = Date.now();
    this.s.tick += 1;
    this.s.flights = this.s.flights.filter((f) => now < f.startWall + f.durationMs + 300);
    // finished issues: after a moment in the Evidence Room the developer walks out
    for (const [id, t] of this.tracks) {
      if (t.stage !== 'done' || !t.endedAt) continue;
      const dev = this.agentById(t.devId);
      if (now - t.endedAt > LINGER_MS && dev && dev.at !== 'reception') this.walk(dev, 'reception');
      if (now - t.endedAt > LINGER_MS + 2500) {
        this.s.agents = this.s.agents.filter((a) => a.id !== t.devId);
        this.s.tasks = this.s.tasks.filter((x) => x.id !== t.task.id);
        this.tracks.delete(id);
      }
    }
    this.refreshStatus(now);
    this.emit();
  }

  private refreshStatus(now: number) {
    const busy = new Map<string, Track>();
    for (const t of this.tracks.values()) {
      if (t.stage === 'done' || t.stage === 'arrive') continue;
      busy.set(t.worker === 'DEV' ? t.devId : t.worker, t);
    }
    for (const a of this.s.agents) {
      const walking = this.walkEnds.get(a.id);
      if (walking && now < walking.until) {
        a.status = 'walking';
        continue;
      }
      if (walking) this.walkEnds.delete(a.id);
      const own = a.id.startsWith('dev-') ? this.tracks.get(a.id.slice(4)) : undefined;
      if (own && own.stage === 'done') {
        a.status = 'meeting';
        a.taskId = own.task.id;
        continue;
      }
      const t = own ?? busy.get(a.id);
      a.status = t ? 'working' : 'idle';
      a.taskId = t?.task.id;
    }
    const open = [...this.tracks.values()].filter((t) => t.stage !== 'done').length;
    this.s.phase = open ? 'build' : 'idle';
    this.s.project = open ? { name: `${open} issue${open === 1 ? '' : 's'} in progress`, brief: '', startedTick: 0 } : undefined;
  }

  /** Is the model answering for this issue right now (a call in flight)? */
  thinking(taskId?: string) {
    if (!taskId) return false;
    for (const t of this.tracks.values()) if (t.task.id === taskId) return Date.now() < t.thinkingUntil;
    return false;
  }

  /** Cards read "#4"; the same issue run twice gets "#4b", "#4c". */
  private cardId(label: string) {
    let id = `T-${label}`;
    for (let i = 0; this.s.tasks.some((x) => x.id === id); i++) id = `T-${label}${String.fromCharCode(98 + (i % 24))}`;
    return id;
  }

  private agentById(id: string) {
    return this.s.agents.find((a) => a.id === id);
  }

  private log(text: string, tone: LogEntry['tone'], room?: RoomId, live = true) {
    if (!live) return;
    this.s.log = [{ id: this.logId++, tick: this.s.tick, day: this.s.day, text, room, tone }, ...this.s.log].slice(0, 300);
  }

  private emit() {
    this.view = { ...this.s, agents: this.s.agents.map((a) => ({ ...a })), tasks: this.s.tasks.map((t) => ({ ...t })) };
    this.subs.forEach((fn) => fn());
  }
}
