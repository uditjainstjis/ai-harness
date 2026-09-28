import type { Chatter } from '../domain/types';

/** The floor's running conversation: who handed what to whom, and what each step found. Newest at the bottom. */
export function Ticker({ chatter, lines = 4 }: { chatter?: Chatter[]; lines?: number }) {
  const now = Date.now();
  const shown = (chatter ?? []).filter((c) => c.at <= now && now - c.at < 45_000).slice(-lines);
  if (!shown.length) return null;
  return (
    <div className="ticker" aria-live="polite">
      {shown.map((c, i) => (
        <div key={c.id} className={`ticker-line tone-${c.tone}${i < shown.length - 2 ? ' old' : ''}`}>
          <b>{c.from}</b>
          {c.to ? (
            <>
              {' → '}
              <b>{c.to}</b>
            </>
          ) : null}
          {': '}
          {c.text}
        </div>
      ))}
    </div>
  );
}

/** The same conversation as a feed in the side panel, newest first. */
export function Conversation({ chatter }: { chatter?: Chatter[] }) {
  const now = Date.now();
  const shown = (chatter ?? []).filter((c) => c.at <= now).slice(-8).reverse();
  return (
    <div className="convo">
      {shown.length === 0 && <div className="muted small">Quiet. Hand the team an issue and every step shows up here as it happens.</div>}
      {shown.map((c) => (
        <div key={c.id} className={`convo-line tone-${c.tone}`}>
          <b>{c.from}</b>
          {c.to ? <> → <b>{c.to}</b></> : null}
          <span>{c.text}</span>
        </div>
      ))}
    </div>
  );
}
