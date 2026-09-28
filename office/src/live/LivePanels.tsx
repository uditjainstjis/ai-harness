import { useState } from 'react';
import { ROOM_BY_ID } from '../domain/office';
import { isBusy, type OfficeState } from '../domain/types';
import { fmtTokens, type OfficeSim } from '../sim/engine';
import { STAGE, type BriefReply, type LiveOffice, type OpenIssue } from './liveOffice';
import { Conversation } from './Ticker';

const DEMO_REPO = 'https://github.com/Pranav-Singh-Devloper/harness-demo-recipes';

/** Hand REAL work to Pramana: an issue link, plain words plus a repository, or a repository to pick issues from. */
export function LiveBrief({ sim }: { sim: OfficeSim }) {
  const live = sim as unknown as LiveOffice;
  const [issue, setIssue] = useState('');
  const [repo, setRepo] = useState('');
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<BriefReply | null>(null);
  const [picked, setPicked] = useState<number[]>([]);

  const submit = async () => {
    if (!issue.trim() && !repo.trim()) {
      setReply({ kind: 'error', text: 'Paste an issue link, or give a repository.' });
      return;
    }
    setBusy(true);
    setReply(null);
    const r = await live.submitIssue(issue.trim(), repo.trim());
    setBusy(false);
    setReply(r);
    if (r.kind === 'pick') setPicked(r.preselect.filter((n) => r.issues.some((i) => i.number === n)).slice(0, 10));
    if (r.kind === 'started') setIssue('');
  };

  const startPicked = async (repoSlug: string) => {
    setBusy(true);
    const r = await live.startIssues(repoSlug, picked);
    setBusy(false);
    setReply(r);
  };

  if (reply?.kind === 'pick') {
    const all = reply.issues;
    return (
      <div className="brief live-brief">
        <div className="approval-kicker">Open issues in {reply.repo}</div>
        <div className="muted small">Pick what the team should take. Each issue gets its own developer, all at once.</div>
        <div className="pick-list">
          {all.length === 0 && <div className="muted small">No open issues found.</div>}
          {all.map((i: OpenIssue) => (
            <label key={i.number} className={`pick-row ${picked.includes(i.number) ? 'on' : ''}`}>
              <input
                type="checkbox"
                checked={picked.includes(i.number)}
                onChange={(e) => setPicked(e.target.checked ? [...picked, i.number] : picked.filter((n) => n !== i.number))}
              />
              <span className="pick-num">#{i.number}</span>
              <span className="pick-title">{i.title}</span>
            </label>
          ))}
        </div>
        <div className="pick-actions">
          <button className="btn" type="button" onClick={() => setReply(null)}>
            Back
          </button>
          <button className="btn primary" type="button" disabled={busy || picked.length === 0} onClick={() => startPicked(reply.repo)}>
            {busy ? 'Handing over…' : `Start ${picked.length} issue${picked.length === 1 ? '' : 's'}`}
          </button>
        </div>
      </div>
    );
  }

  return (
    <form
      className="brief live-brief"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="approval-kicker">Give the team an issue</div>
      <label>
        Issue
        <textarea
          rows={3}
          value={issue}
          placeholder="A GitHub issue link, or the bug in plain words (leave empty to pick from the repo's open issues)"
          onChange={(e) => setIssue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit();
          }}
        />
      </label>
      <label>
        Repository
        <input value={repo} placeholder="github.com/owner/repo (optional with an issue link)" onChange={(e) => setRepo(e.target.value)} />
      </label>
      <button className="btn primary" type="submit" disabled={busy}>
        {busy ? 'Handing over…' : 'Hand it to the Issue Desk'}
      </button>
      {reply && <div className={`brief-reply ${reply.kind}`}>{reply.kind === 'error' ? `⚠ ${reply.text}` : `✓ ${reply.text}`}</div>}
      <button
        className="linkish small"
        type="button"
        onClick={() => {
          setIssue('');
          setRepo(DEMO_REPO);
        }}
      >
        No repo handy? Use the recipes demo repo (10 real bugs)
      </button>
    </form>
  );
}

/** What every issue on the floor is doing right now, and what it has cost. */
export function LiveOverview({ state, sim, onInspect }: { state: OfficeState; sim: OfficeSim; onInspect?: (id: string) => void }) {
  const live = sim as unknown as LiveOffice;
  const tracks = [...live.tracks.values()];
  const working = state.agents.filter((a) => isBusy(a.status)).length;
  const order = Object.keys(STAGE) as (keyof typeof STAGE)[];
  return (
    <div className="stack">
      <section>
        <h3>Live conversation</h3>
        <Conversation chatter={state.chatter} />
      </section>
      <section>
        <h3>On the floor now</h3>
        {tracks.length === 0 ? (
          <div className="muted small">Nobody is working yet. Give the team an issue above, or start a batch in Studio.</div>
        ) : (
          <div className="live-issues">
            {tracks.map((t) => {
              const done = t.stage === 'done';
              const ok = t.verdict === 'verified';
              const idx = order.indexOf(t.stage as keyof typeof STAGE);
              return (
                <div key={t.task.id} className={`live-issue ${done ? (ok ? 'ok' : 'bad') : ''}`} onClick={() => onInspect?.(t.run.id)} title="Open the evidence">
                  <div className="live-issue-head">
                    <strong>{t.task.title}</strong>
                    <span className="live-state">
                      {done ? (ok ? 'verified ✓' : t.verdict || 'ended') : t.stage === 'arrive' ? 'arriving' : STAGE[t.stage as keyof typeof STAGE].label}
                    </span>
                  </div>
                  <div className="live-steps">
                    {order.slice(0, 8).map((s, i) => (
                      <span key={s} className={`step ${done || i < idx ? 'past' : i === idx ? 'now' : ''}`} title={STAGE[s].label} />
                    ))}
                  </div>
                  <div className="muted small">
                    {done ? 'in the Evidence Room' : `in ${ROOM_BY_ID[t.task.room].name}`} · {fmtTokens(t.tokens)} tokens
                    {live.thinking(t.task.id) ? ' · model answering…' : ''}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        <div className="muted small">
          {working} working · {state.flights.length} hand-off{state.flights.length === 1 ? '' : 's'} in flight · today {live.totals.verified} verified,{' '}
          {live.totals.other} other
        </div>
      </section>
      <section>
        <h3>Tokens, from the provider</h3>
        <div className="small">
          <strong>{fmtTokens(state.tokensUsed)}</strong> used by the issues on the floor. Setup, reproduction and the proof run code, not the
          model, so they cost none.
        </div>
      </section>
    </div>
  );
}
