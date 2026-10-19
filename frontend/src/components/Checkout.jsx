import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle, ArrowRight, Check, CheckCircle2, ClipboardCheck, CreditCard, Loader2,
  Lock, Plane, RefreshCw, Undo2, XCircle,
} from 'lucide-react';
import { api, auth } from '../api/index.js';

/* The group checkout: every traveler authorizes their share, nobody is charged
   until everyone has, then Rovyn books the stay and the flights and captures.
   Data comes from the plan poll (a light summary) plus GET /checkout, which is
   fetched on mount, whenever the summary changes, and every 3 s only while the
   settlement is in a transient state (verifying → paying). */

const STATUS_COPY = {
  collecting: 'Collecting the group\'s payments',
  reconfirm: 'Prices changed — everyone pays again',
  verifying: 'Re-checking prices with the suppliers…',
  booking: 'Booking the stay…',
  capturing: 'Charging everyone\'s cards…',
  unwinding: 'A card was declined — refunding the others…',
  paying: 'Paying the airlines…',
  reconciling: 'Checking in with the suppliers…',
  booked: 'Booked — every reservation is in the Bookings tab',
  partially_booked: 'Booked, with an exception',
  needs_attention: 'Paused — Rovyn support is checking the suppliers',
  failed: 'This round didn\'t complete',
  cancelled: 'Checkout abandoned',
};
const TRANSIENT = new Set(['verifying', 'booking', 'capturing', 'unwinding', 'paying', 'reconciling']);
const ITEM_LABEL = { liteapi: 'hotel', duffel: 'airline', simulated: 'simulated', self: 'you book it', rovyn: 'fee' };

const money = c => `$${(Math.round(c || 0) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const when = iso => {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
};

const PROFILE_FIELDS = [
  ['given_name', 'First name (as on your ID)', 'text'], ['family_name', 'Last name', 'text'],
  ['born_on', 'Date of birth', 'date'], ['gender', 'Gender (airline: m / f)', 'text'],
  ['title', 'Title (mr / ms / mrs / miss)', 'text'], ['phone', 'Phone (+14155550123)', 'tel'],
];

function ProfileForm({ tripId, profile, needs, onSaved }) {
  const me = auth.user();
  const parts = (me?.name || '').split(' ');
  const [form, setForm] = useState({
    given_name: profile?.given_name || parts[0] || '', family_name: profile?.family_name || parts.slice(1).join(' ') || '',
    born_on: profile?.born_on || '', gender: profile?.gender || '', title: profile?.title || '', phone: profile?.phone || '',
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const fields = PROFILE_FIELDS.filter(([k]) => needs.fields.includes(k));
  async function save(e) {
    e.preventDefault();
    setBusy(true); setErr('');
    try { await api.saveTravelerProfile(tripId, form); await onSaved(); }
    catch (ex) { setErr(ex.message); }
    finally { setBusy(false); }
  }
  return (
    <form onSubmit={save} style={{ maxWidth: 480, marginBottom: 22 }}>
      <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 6 }}>Traveler details</p>
      <p className="muted" style={{ fontSize: 13.5, marginBottom: 12 }}>
        {needs.flight
          ? 'The airline issues your ticket in this name and needs these exact fields. Rovyn stores only what the booking needs.'
          : 'The hotel registers the rooms in these names.'}
      </p>
      {fields.map(([key, label, type]) => (
        <label key={key} style={{ display: 'block', marginBottom: 10 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>{label}</span>
          <input type={type} value={form[key]} onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                 required={needs.fields.includes(key)} />
        </label>
      ))}
      {err && <p style={{ color: 'var(--clay)', fontSize: 13.5, marginBottom: 8 }}>{err}</p>}
      <button className="btn" type="submit" disabled={busy}>
        {busy ? <Loader2 className="spin" size={15} /> : <Check size={15} strokeWidth={2.5} />} Save details
      </button>
    </form>
  );
}

function Members({ members, meId }) {
  const label = { none: 'not yet', pending: 'paying…', authorized: 'authorized', nothing_due: 'nothing due', releasing: 'releasing…', captured: 'charged', refunded: 'refunded', released: 'released', failed: 'failed', expired: 'link expired' };
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
      {members.map(m => {
        const done = ['authorized', 'nothing_due', 'captured', 'refunded'].includes(m.state);
        return (
          <span key={m.user_id} style={{
            display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 999, fontSize: 13,
            fontWeight: 600, border: '1px solid', borderColor: done ? 'var(--pine)' : 'var(--line)',
            color: done ? 'var(--pine)' : 'var(--muted)',
          }}>
            {done ? <CheckCircle2 size={13} /> : <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--line)' }} />}
            {m.name}{m.user_id === meId ? ' (you)' : ''} · {money(m.amount_cents)} · {label[m.state] || m.state}
          </span>
        );
      })}
    </div>
  );
}

export default function Checkout({ tripId, trip, summary, onChanged, goTab, onSelfBook, busy: parentBusy }) {
  const me = auth.user();
  const [state, setState] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [ack, setAck] = useState(false);
  const [showEstimates, setShowEstimates] = useState(false);
  const pollRef = useRef(null);
  const sig = JSON.stringify(summary || {});

  async function refresh() {
    try { setState(await api.getCheckout(tripId)); }
    catch (err) { setError(err.message); }
  }

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [tripId, sig]);

  const settlement = state?.settlement;
  const transient = Boolean(settlement && TRANSIENT.has(settlement.status));
  useEffect(() => {
    clearInterval(pollRef.current);
    if (transient) pollRef.current = setInterval(refresh, 3000);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transient, tripId]);

  async function act(fn, after = 'refresh') {
    setBusy(true); setError('');
    try {
      const res = await fn();
      if (res?.url) { window.location.assign(res.url); return; }
      await refresh();
      if (after === 'reload') await onChanged?.();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  if (!state) return <p className="muted" style={{ fontSize: 14 }}><Loader2 className="spin" size={14} /> Preparing the checkout…</p>;

  const isLeader = trip?.is_leader;
  const check = state.me?.check;
  const myPayment = state.me?.payment;
  const needs = state.me?.needs_profile || { fields: [], missing: [], flight: false };
  const status = settlement?.status;
  const open = !settlement || ['collecting', 'reconfirm'].includes(status);
  const items = settlement?.items || [];
  const charged = (check?.lines || []).filter(l => l.charged);
  const estimates = (check?.lines || []).filter(l => !l.charged);
  const selfItems = items.filter(i => !i.charge && i.category !== 'estimate');
  const confirmations = state.confirmations || [];
  const simulated = state.simulated;
  // Once any traveler books the trip themselves the group checkout is off for
  // everyone (it needs every traveler), so booking your own part is the only
  // way left to lock in — it becomes the main action, not a footnote.
  const itemByItem = !state.available && state.self_book_chosen;
  const modeNote = itemByItem ? null
    : simulated ? 'Simulated — no money moves and confirmations are test codes.'
    : state.money_mode === 'test' ? 'Stripe test mode — use test cards; nothing real is charged.' : null;

  const headline = (
    <div style={{ marginBottom: 14 }}>
      <p style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 700, fontSize: 15, color: status === 'failed' || status === 'needs_attention' ? 'var(--clay)' : 'var(--pine)' }}>
        {transient ? <Loader2 className="spin" size={15} /> : status === 'booked' ? <CheckCircle2 size={16} /> : status === 'failed' ? <XCircle size={16} /> : itemByItem ? <ClipboardCheck size={16} /> : <CreditCard size={16} />}
        {settlement ? STATUS_COPY[status] || status : itemByItem ? state.reason : 'Pay in Rovyn — one checkout, every reservation in the app'}
      </p>
      {modeNote && <p className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>{modeNote}</p>}
      {settlement?.deadline && open && !settlement.deadline_passed && (
        <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
          <Lock size={11} style={{ verticalAlign: -1 }} /> Everyone must pay by <strong>{when(settlement.deadline)}</strong> — after that every authorization is released.
        </p>
      )}
      {settlement?.deadline_passed && open && (
        <p style={{ fontSize: 13, marginTop: 4, color: 'var(--clay)' }}>
          <Lock size={11} style={{ verticalAlign: -1 }} /> The group's deadline ({when(settlement.deadline)}) has passed — the authorizations are being released and the checkout restarts.
        </p>
      )}
    </div>
  );

  // ---- booked / partial / needs attention / failed ----------------------------
  if (settlement && !open && !transient) {
    return (
      <div>
        {headline}
        {status === 'failed' && (
          <>
            <p className="muted" style={{ fontSize: 14, marginBottom: 10 }}>
              {settlement.timeline.filter(e => e.level === 'error').slice(-1)[0]?.message || 'Something did not go through. No one was charged for what was not booked.'}
            </p>
            {isLeader && (
              <button className="btn" disabled={busy} onClick={() => act(() => api.retryCheckout(tripId), 'reload')}>
                <RefreshCw size={14} /> Try again with fresh prices
              </button>
            )}
          </>
        )}
        {status === 'needs_attention' && (
          <p className="muted" style={{ fontSize: 14 }}>A supplier didn't answer clearly. Nothing more will be charged or refunded automatically — the trip leader has an email, and Rovyn reconciles it by hand.</p>
        )}
        {['booked', 'partially_booked'].includes(status) && (
          <>
            {confirmations.map(c => (
              <div key={c.item_key} style={{ display: 'flex', gap: 14, alignItems: 'baseline', padding: '10px 0', borderTop: '1px dashed var(--line-soft)', flexWrap: 'wrap' }}>
                <div style={{ flex: 1, minWidth: 200 }}>
                  <p style={{ fontWeight: 600, fontSize: 14.5 }}>{c.title}</p>
                  <p className="muted" style={{ fontSize: 13 }}>
                    {c.status === 'pending_confirmation' ? 'booked — hotel confirmation code pending' : c.confirmation ? <>confirmation <strong>{c.confirmation}</strong></> : c.status}
                    {c.provider === 'simulated' ? ' · simulated' : ''}
                  </p>
                </div>
                {c.can_cancel && (
                  <button className="btn-quiet" disabled={busy} style={{ fontSize: 13, color: 'var(--clay)' }}
                          onClick={() => window.confirm(`Cancel ${c.title}? Refunds follow the rate's cancellation policy.`) && act(() => api.cancelRovynBooking(tripId, c.item_key), 'reload')}>
                    <Undo2 size={13} /> cancel
                  </button>
                )}
              </div>
            ))}
            {items.filter(i => i.failed).map(i => (
              <p key={i.item_key} style={{ fontSize: 13.5, color: 'var(--clay)', marginTop: 8 }}><AlertTriangle size={13} /> {i.title}: {i.failed} — refunded, back on the checklist.</p>
            ))}
            {state.me?.receipt && (
              <p className="muted" style={{ fontSize: 13.5, marginTop: 12 }}>Charged {money((state.me.receipt.total || 0) * 100)} · receipt {state.me.receipt.receipt_no}{state.me.receipt.demo ? ' (test)' : ''}</p>
            )}
            <button className="btn" style={{ marginTop: 14 }} onClick={() => goTab?.('bookings')}><ClipboardCheck size={15} /> Open the Bookings tab</button>
          </>
        )}
        {error && <p style={{ color: 'var(--clay)', fontSize: 13.5, marginTop: 10 }}>{error}</p>}
      </div>
    );
  }

  // ---- transient --------------------------------------------------------------
  if (transient) {
    return (
      <div>
        {headline}
        {settlement.timeline.slice(-5).map((e, i) => (
          <p key={i} className="muted" style={{ fontSize: 13 }}>{when(e.at)} · {e.message}</p>
        ))}
      </div>
    );
  }

  // ---- open: profile → check → pay / waiting --------------------------------
  const authorized = myPayment && ['authorized', 'nothing_due', 'captured'].includes(myPayment.status);
  const anyoneCommitted = Boolean(settlement && settlement.members.some(m => ['authorized', 'nothing_due', 'captured'].includes(m.state)));
  const pending = myPayment && myPayment.status === 'pending';
  return (
    <div>
      {headline}
      {!state.available && !itemByItem && <p style={{ color: 'var(--clay)', fontSize: 14 }}>{state.reason}</p>}
      {state.available && needs.missing.length > 0 && !authorized && (
        <ProfileForm tripId={tripId} profile={state.me.profile} needs={needs} onSaved={refresh} />
      )}
      {check && (
        <div style={{ maxWidth: 560 }}>
          {charged.map(l => (
            <div key={l.item_key} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: '8px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 14 }}>
              <span>{l.label} <span className="muted" style={{ fontSize: 12 }}>· {l.estimate ? 'estimate' : 'live price'} · {ITEM_LABEL[l.provider] || l.provider}</span></span>
              <span style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{money(l.cents)}</span>
            </div>
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '12px 0 6px', alignItems: 'baseline' }}>
            <span style={{ fontWeight: 700 }}>Charged now (held, not charged, until everyone pays)</span>
            <span className="serif" style={{ fontSize: 26, color: 'var(--pine)' }}>{money(check.charged_cents)}</span>
          </div>
          {items.filter(i => i.charge && i.previous_cents != null && i.previous_cents !== i.per_person_cents).map(i => (
            <p key={i.item_key} style={{ fontSize: 13, color: 'var(--clay)' }}>
              {i.title}: was {money(i.previous_cents)} per person, now {money(i.per_person_cents)}.
            </p>
          ))}
          {items.filter(i => i.charge && i.cancel_deadline).map(i => (
            <p key={i.item_key} className="muted" style={{ fontSize: 12.5 }}>{i.title}: {i.refundable ? `free cancellation until ${when(i.cancel_deadline)}` : 'non-refundable'}{i.hold_until ? ` · airline holds the fare until ${when(i.hold_until)}` : ''}</p>
          ))}
          {selfItems.length > 0 && (
            <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
              <Plane size={12} style={{ verticalAlign: -1 }} /> Not booked by Rovyn (you book these yourself from the checklist): {selfItems.map(i => `${i.title}${i.note ? ` — ${i.note}` : ''}`).join('; ')}
            </p>
          )}
          <button className="btn-quiet" style={{ fontSize: 13, marginTop: 6 }} onClick={() => setShowEstimates(s => !s)}>
            {showEstimates ? 'Hide' : 'Show'} what you'll pay on the spot ({money(estimates.reduce((s, l) => s + l.cents, 0))} est.)
          </button>
          {showEstimates && estimates.map(l => (
            <div key={l.item_key} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: '5px 0', fontSize: 13 }} className="muted">
              <span>{l.label}</span><span>{money(l.cents)}</span>
            </div>
          ))}
        </div>
      )}

      {state.available && !settlement && needs.missing.length === 0 && (
        <div style={{ marginTop: 18 }}>
          <button className="btn" disabled={busy || parentBusy} onClick={() => act(() => api.lockCheckout(tripId))}>
            {busy ? <Loader2 className="spin" size={16} /> : <ArrowRight size={16} />} See my share at live prices
          </button>
          <p className="muted" style={{ fontSize: 13, marginTop: 7, maxWidth: 480 }}>
            Rovyn asks the hotel and the airlines for today's prices and holds the fares. Nothing is paid yet.
          </p>
        </div>
      )}
      {state.available && settlement && !authorized && needs.missing.length === 0 && (
        <div style={{ marginTop: 18 }}>
          {settlement?.needs_ack && (
            <label style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: 13.5, maxWidth: 520, marginBottom: 12 }}>
              <input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} style={{ width: 'auto', marginTop: 4 }} />
              <span>I understand this check includes a <strong>non-refundable</strong> item that is booked only after every card is charged; cancelling it returns nothing.</span>
            </label>
          )}
          <button className="btn clay" disabled={busy || parentBusy || (settlement?.needs_ack && !ack)}
                  onClick={() => act(() => api.startCheckoutSession(tripId, ack), 'reload')}>
            {busy ? <Loader2 className="spin" size={16} /> : <CreditCard size={16} />}
            {pending ? 'Continue paying' : simulated ? 'Authorize my share (simulated)' : 'Pay my share'} · {money(check?.charged_cents)}
          </button>
          <p className="muted" style={{ fontSize: 13, marginTop: 7, maxWidth: 480 }}>
            Your card is authorized now and charged only when everyone has paid and the suppliers confirmed. The total above is the full amount — nothing is added at checkout.
          </p>
        </div>
      )}
      {authorized && (
        <div style={{ marginTop: 18 }}>
          <p style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700 }}>
            <CheckCircle2 size={17} /> {myPayment.status === 'nothing_due' ? 'Nothing to charge you — waiting on the others' : 'Your share is authorized — waiting on the others'}
          </p>
          <button className="btn-quiet" disabled={busy} style={{ fontSize: 13, marginTop: 6 }}
                  onClick={() => act(() => api.releaseAuthorization(tripId), 'reload')}>
            <Undo2 size={13} /> Release my authorization
          </button>
        </div>
      )}
      {settlement && <Members members={settlement.members} meId={me?.id} />}
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginTop: 18, alignItems: 'center' }}>
        {isLeader && settlement && ['collecting', 'reconfirm'].includes(status) && (
          <button className="btn-quiet" disabled={busy} style={{ fontSize: 13, color: 'var(--clay)' }}
                  onClick={() => window.confirm('Abandon the group checkout? Every authorization is released.') && act(() => api.abandonCheckout(tripId), 'reload')}>
            <XCircle size={13} /> Abandon the group checkout
          </button>
        )}
        {/* self_book_chosen is trip-wide and never coexists with a round (lock and
            session refuse a self-booked trip; /pay refuses while a round is
            active), so it must not hide this: every later traveler needs it */}
        {onSelfBook && !settlement && (itemByItem ? (
          <button className="btn" disabled={busy || parentBusy} onClick={onSelfBook}>
            <ClipboardCheck size={16} /> The group is booking item by item — I'll book my part myself
          </button>
        ) : (
          <button className="btn-quiet" disabled={busy || parentBusy} onClick={onSelfBook}>
            <ClipboardCheck size={14} /> I'll book everything myself instead <ArrowRight size={13} />
          </button>
        ))}
        {onSelfBook && !state.self_book_chosen && settlement && !anyoneCommitted && (
          <span className="muted" style={{ fontSize: 13 }}>
            To book everything yourself instead, {isLeader ? 'abandon the group checkout first.' : 'ask the leader to abandon the group checkout first.'}
          </span>
        )}
      </div>
      {onSelfBook && itemByItem && (
        <p className="muted" style={{ fontSize: 13, marginTop: 7, maxWidth: 480 }}>
          Nothing is charged in Rovyn — book your part with the sellers, then record each confirmation on the checklist so the group sees what's done.
        </p>
      )}
      {error && <p style={{ color: 'var(--clay)', fontSize: 13.5, marginTop: 10 }}>{error}</p>}
    </div>
  );
}
