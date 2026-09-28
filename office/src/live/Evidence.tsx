import { useEffect, useState } from 'react';
import { fmtTokens } from '../sim/engine';

interface Check {
  command: string;
  origin: string;
  verdict: string;
  before?: { exit_code?: number; passed?: boolean; summary?: string; tail?: string } | null;
  after?: { exit_code?: number; passed?: boolean; summary?: string; tail?: string } | null;
}
interface RunDetail {
  id: string;
  title?: string;
  status: string;
  verdict?: string | null;
  now?: string;
  elapsed_s?: number | null;
  calls?: number | null;
  tokens?: number | null;
  repo?: string;
  model?: { model?: string; provider?: string } | null;
  github?: { kind: string; ok: boolean; url?: string; error?: string }[];
  result?: {
    status?: string;
    summary?: string;
    patch?: string;
    error?: string;
    elapsed_s?: number;
    attempts?: number;
    strength?: string;
    usage?: { calls?: number; input_tokens?: number; output_tokens?: number; total_tokens?: number };
    checks?: Check[];
  } | null;
}
interface Ev {
  kind: string;
  at?: number;
  name?: string;
  message?: string;
  brief?: string;
}

const VERDICT: Record<string, { label: string; tone: string }> = {
  fixes: { label: 'fixes the bug', tone: 'ok' },
  passes_both: { label: 'still passes', tone: 'ok' },
  regression: { label: 'REGRESSION', tone: 'bad' },
  still_failing: { label: 'still failing', tone: 'bad' },
  fails_both: { label: 'failed before too', tone: 'muted' },
  fails_after: { label: 'fails after', tone: 'bad' },
  timeout: { label: 'timed out', tone: 'bad' },
  no_tests: { label: 'no tests ran', tone: 'muted' },
};
const ACTIVE = new Set(['running', 'preparing', 'starting', 'queued']);

function outcome(o?: Check['before']) {
  if (!o) return '—';
  return `${o.passed ? 'pass' : 'fail'}${o.exit_code != null ? ` (exit ${o.exit_code})` : ''}`;
}

/** Everything one agent did, as evidence: the proof on original vs patched code, the diff, the cost, the log. */
export function EvidenceDrawer({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [log, setLog] = useState<Ev[]>([]);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [prev, setPrev] = useState<{ repo: string; title: string; body: string; branch: string; files: string[] } | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r: RunDetail = await (await fetch(`/api/runs/${runId}`)).json();
        const e = await (await fetch(`/api/runs/${runId}/events.json?since=0`)).json();
        if (!alive) return;
        setRun(r);
        setLog((e.events as Ev[]).filter((x) => x.kind === 'log' || x.kind === 'tool_call' || x.kind === 'stage').slice(-14));
      } catch {
        /* retry on the next tick */
      }
    };
    load();
    const t = window.setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  const act = async (what: 'stop' | 'pr') => {
    if (!run) return;
    if (what === 'stop') {
      setBusy('stop');
      await fetch(`/api/runs/${run.id}/stop`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
      setBusy('');
      setNote('Stop requested: the agent ends after its current step.');
      return;
    }
    setBusy('pr');
    try {
      if (!prev) {                                  // step 1: show what would be opened
        const p = await (await fetch(`/api/runs/${run.id}/github?kind=pr`)).json();
        if (!p.ok) throw new Error(p.error || 'not available');
        setPrev(p);
      } else {                                      // step 2: you confirmed
        const r = await (
          await fetch(`/api/runs/${run.id}/github`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ kind: 'pr', title: prev.title, body: prev.body, branch: prev.branch, files: prev.files }),
          })
        ).json();
        setPrev(null);
        setNote(r.ok ? `Pull request: ${r.url}` : `Could not open it: ${r.error}`);
      }
    } catch (e) {
      setNote(`Could not open it: ${String(e)}`);
    }
    setBusy('');
  };

  const res = run?.result;
  const active = run ? ACTIVE.has(run.status) : false;
  const verdict = res?.status || run?.verdict || (active ? 'working' : run?.status);
  const checks = res?.checks ?? [];
  const patch = (res?.patch ?? '').split('\n').filter((l) => !l.startsWith('diff --git a/.pramana'));
  const pr = (run?.github ?? []).find((g) => g.kind === 'pr' && g.ok && g.url);

  return (
    <aside className="evidence" onClick={(e) => e.stopPropagation()}>
      <div className="evidence-head">
        <div>
          <div className="approval-kicker">Evidence</div>
          <div className="evidence-title">{run?.title ?? 'loading…'}</div>
        </div>
        <button className="btn" onClick={onClose} title="Close">
          ✕
        </button>
      </div>
      {run && (
        <>
          <div className={`verdict-badge v-${verdict === 'verified' ? 'ok' : active ? 'live' : 'bad'}`}>
            {verdict === 'verified' ? '✓ VERIFIED: proven on the original and the patched code' : active ? `● ${run.now || 'working'}` : `✕ ${verdict}${res?.error ? `: ${res.error.slice(0, 120)}` : ''}`}
          </div>
          <div className="evidence-stats">
            <div>
              <b>{run.elapsed_s ?? res?.elapsed_s ?? '…'}</b>
              <span>seconds</span>
            </div>
            <div>
              <b>{fmtTokens(res?.usage?.total_tokens ?? run.tokens ?? 0)}</b>
              <span>tokens</span>
            </div>
            <div>
              <b>{res?.usage?.calls ?? run.calls ?? 0}</b>
              <span>model calls</span>
            </div>
            <div>
              <b>{res?.strength ?? '…'}</b>
              <span>proof</span>
            </div>
          </div>

          <h4>Proof: every check on the original and on the patched code</h4>
          {checks.length === 0 ? (
            <div className="muted small">{active ? 'The proof runs once the fix is written.' : 'No checks were recorded for this run.'}</div>
          ) : (
            <table className="proof">
              <thead>
                <tr>
                  <th>check</th>
                  <th>original</th>
                  <th>patched</th>
                  <th>verdict</th>
                </tr>
              </thead>
              <tbody>
                {checks.map((c, i) => {
                  const v = VERDICT[c.verdict] ?? { label: c.verdict, tone: 'muted' };
                  return (
                    <tr key={i}>
                      <td title={c.command}>
                        <code>{c.command.replace(/^cd \S+ && /, '').slice(0, 60)}</code>
                        <div className="muted tiny">{c.origin}</div>
                      </td>
                      <td>{outcome(c.before)}</td>
                      <td>{outcome(c.after)}</td>
                      <td className={`v-${v.tone}`}>{v.label}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {res?.summary && (
            <>
              <h4>Root cause</h4>
              <div className="small">{res.summary}</div>
            </>
          )}

          <h4>The change</h4>
          {patch.join('').trim() ? (
            <pre className="diff">
              {patch.slice(0, 220).map((l, i) => (
                <span key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : ''}>
                  {l + '\n'}
                </span>
              ))}
            </pre>
          ) : (
            <div className="muted small">{active ? 'Not written yet.' : 'No change was kept.'}</div>
          )}

          <h4>What it is doing</h4>
          <div className="evlog">
            {log.map((e, i) => (
              <div key={i}>{(e.message || e.brief || e.name || e.kind).toString().slice(0, 140)}</div>
            ))}
          </div>

          <div className="evidence-actions">
            {active && (
              <button className="btn" disabled={busy === 'stop'} onClick={() => act('stop')}>
                ■ Stop this agent
              </button>
            )}
            {!active && verdict === 'verified' && !pr && !prev && (
              <button className="btn primary" disabled={busy === 'pr'} onClick={() => act('pr')}>
                {busy === 'pr' ? 'Preparing…' : 'Open pull request…'}
              </button>
            )}
            {pr && (
              <a className="btn" href={pr.url} target="_blank" rel="noreferrer">
                View pull request ↗
              </a>
            )}
            {!active && (
              <a className="btn" href={`/api/runs/${run.id}/report`} target="_blank" rel="noreferrer">
                Full report ↗
              </a>
            )}
            <a className="btn" href={`/#run=${run.id}`} target="_top">
              Open in Studio
            </a>
          </div>
          {prev && (
            <div className="pr-preview">
              <div className="small">
                Open a pull request on <b>{prev.repo}</b> from <code>{prev.branch}</code>
              </div>
              <div className="small">
                <b>{prev.title}</b> · {prev.files.join(', ')}
              </div>
              <div className="evidence-actions">
                <button className="btn" onClick={() => setPrev(null)}>
                  Cancel
                </button>
                <button className="btn primary" disabled={busy === 'pr'} onClick={() => act('pr')}>
                  {busy === 'pr' ? 'Opening…' : 'Confirm: open it on GitHub'}
                </button>
              </div>
            </div>
          )}
          {note && <div className="brief-reply">{note}</div>}
        </>
      )}
    </aside>
  );
}
