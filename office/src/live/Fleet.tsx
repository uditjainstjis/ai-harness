import { useEffect, useState } from 'react';
import { fmtTokens } from '../sim/engine';

interface Row {
  id: string;
  title?: string;
  repo?: string;
  status: string;
  verdict?: string | null;
  now?: string;
  phase?: string;
  elapsed_s?: number | null;
  tokens?: number | null;
  calls?: number | null;
  created?: number;
  batch_id?: string;
  github?: { kind: string; ok: boolean; url?: string }[];
}

const ACTIVE = new Set(['running', 'preparing', 'starting', 'queued']);
/** "…/workspace/owner__repo-issue-4-ab12" or a GitHub URL -> "owner/repo". */
const repoName = (repo?: string) => {
  const base = (repo || '').replace(/^https:\/\/github\.com\//, '').split('/workspace/').pop() || '';
  return base.replace(/(_\d+)?(-issue-\d+(-\w{4})?)?$/, '').replace('__', '/').slice(0, 48);
};
const label = (r: Row) =>
  ACTIVE.has(r.status) ? 'working' : r.verdict === 'verified' ? 'verified' : r.verdict === 'patched' ? 'unproven' : r.verdict || r.status;

/** Every agent today in one table: what it is on, how long, what it cost, and whether its work is proven. */
export function Fleet({ onInspect }: { onInspect: (id: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [filter, setFilter] = useState<'all' | 'working' | 'verified' | 'other'>('all');

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const all: Row[] = await (await fetch('/api/runs')).json();
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        if (alive) setRows(all.filter((r) => (r.created ?? 0) * 1000 >= today.getTime() || ACTIVE.has(r.status)));
      } catch {
        /* next tick */
      }
    };
    load();
    const t = window.setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const working = rows.filter((r) => ACTIVE.has(r.status));
  const verified = rows.filter((r) => !ACTIVE.has(r.status) && r.verdict === 'verified');
  const ended = rows.filter((r) => !ACTIVE.has(r.status));
  const spent = ended.reduce((s, r) => s + (r.tokens ?? 0), 0);
  const perFix = verified.length ? spent / verified.length : 0;
  const avgSecs = verified.length ? verified.reduce((s, r) => s + (r.elapsed_s ?? 0), 0) / verified.length : 0;
  const shown = rows.filter((r) =>
    filter === 'all' ? true : filter === 'working' ? ACTIVE.has(r.status) : filter === 'verified' ? label(r) === 'verified' : !ACTIVE.has(r.status) && label(r) !== 'verified',
  );

  return (
    <div className="fleet">
      <div className="fleet-kpis">
        <div>
          <b>{working.length}</b>
          <span>agents working</span>
        </div>
        <div>
          <b>
            {verified.length}/{ended.length}
          </b>
          <span>verified today</span>
        </div>
        <div>
          <b>{fmtTokens(perFix)}</b>
          <span>tokens per verified fix (all runs' spend)</span>
        </div>
        <div>
          <b>{avgSecs ? `${Math.round(avgSecs)} s` : '—'}</b>
          <span>average time to a verified fix</span>
        </div>
      </div>
      <div className="seg fleet-filter">
        {(['all', 'working', 'verified', 'other'] as const).map((f) => (
          <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>
            {f}
          </button>
        ))}
      </div>
      <div className="fleet-table-wrap">
        <table className="fleet-table">
          <thead>
            <tr>
              <th>issue</th>
              <th>repository</th>
              <th>state</th>
              <th>time</th>
              <th>tokens</th>
              <th>calls</th>
              <th>PR</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const st = label(r);
              const pr = (r.github ?? []).find((g) => g.kind === 'pr' && g.ok && g.url);
              return (
                <tr key={r.id} onClick={() => onInspect(r.id)} title="Open the evidence">
                  <td className="fleet-issue">{r.title}</td>
                  <td className="muted">{repoName(r.repo)}</td>
                  <td>
                    <span className={`chip c-${st}`}>{st === 'working' ? `● ${r.phase || 'working'}` : st}</span>
                  </td>
                  <td>{r.elapsed_s != null ? `${Math.round(r.elapsed_s)} s` : ACTIVE.has(r.status) ? '…' : '—'}</td>
                  <td>{r.tokens != null ? fmtTokens(r.tokens) : ACTIVE.has(r.status) ? '…' : '—'}</td>
                  <td>{r.calls ?? '—'}</td>
                  <td>
                    {pr ? (
                      <a href={pr.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                        PR ↗
                      </a>
                    ) : (
                      ''
                    )}
                  </td>
                </tr>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  Nothing here yet today.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
