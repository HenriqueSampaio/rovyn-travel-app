import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowDown, ArrowUp, BedDouble, CalendarDays, Check, CheckCircle2, Compass,
  CreditCard, Landmark, Lightbulb, Loader2, Mail, MoonStar, PartyPopper,
  Pencil, PlaneTakeoff, Receipt, Scale, Send, Star, Sun, Sunset, Ticket,
  Users, Wallet, X,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText, JourneyTracker } from '../ui.jsx';

const COST_LABELS = {
  stay: 'Accommodation (your share)',
  itinerary_activities: 'Itinerary activities',
  visits: 'Starred visits & sights',
  events: 'Starred events',
  meals_estimate: 'Meals (estimate)',
  transport_estimate: 'Local transport (estimate)',
};

const TABS = [
  { key: 'overview', label: 'Overview', Icon: Compass },
  { key: 'itinerary', label: 'Itinerary', Icon: CalendarDays },
  { key: 'travel', label: 'Travel & stay', Icon: PlaneTakeoff },
  { key: 'money', label: 'Costs & payment', Icon: Wallet },
];

function Section({ eyebrow, Icon, children, style }) {
  return (
    <section style={{ marginBottom: 48, ...style }}>
      <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
        {Icon && <Icon size={13} strokeWidth={2.5} />} {eyebrow}
      </p>
      {children}
    </section>
  );
}

export default function FinalPlan() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const me = auth.user();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('overview');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [editText, setEditText] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [slotModal, setSlotModal] = useState(null); // { day, slot, label }
  const [receipt, setReceipt] = useState(null);
  const [emailInfo, setEmailInfo] = useState(null);
  const pollRef = useRef(null);

  async function load() {
    try {
      const d = await api.getPlan(tripId);
      if (!d.plan && !d.generating) {
        navigate(`/trips/${tripId}`);
        return;
      }
      setData(d);
    } catch (err) {
      if (err.status === 401) navigate('/');
      else if (err.status === 403 || err.status === 404) navigate('/trips');
    }
  }

  useEffect(() => {
    load();
    pollRef.current = setInterval(load, 3000);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tripId]);

  useEffect(() => {
    if (data?.trip?.me?.paid && !receipt) {
      api.getReceipt(tripId).then(r => {
        setReceipt(r.receipt);
        setEmailInfo({ status: r.email_status });
      }).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.trip?.me?.paid]);

  if (!data) return <Page><Loading text="Opening the final plan…" /></Page>;
  if (!data.plan) return <Page><Loading text="Counting stars and drafting the plan…" /></Page>;

  const { plan, checks, trip } = data;
  const status = data.status;
  const anyPaid = checks.some(c => c.paid);
  const editable = !anyPaid && ['final_review', 'approved'].includes(status);
  const myCheck = checks.find(c => c.user_id === me?.id);
  const iApproved = trip?.me?.approved;
  const iPaid = trip?.me?.paid;
  const days = plan.itinerary?.days || [];

  async function act(fn, ...args) {
    setBusy(true);
    setError('');
    try {
      await fn(...args);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function submitEdit() {
    if (!editText.trim()) return;
    setBusy(true);
    setError('');
    try {
      await api.editPlan(tripId, editText.trim());
      setEditText('');
      setEditOpen(false);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function moveDay(index, delta) {
    const order = days.map(d => d.day);
    const j = index + delta;
    if (j < 0 || j >= order.length) return;
    [order[index], order[j]] = [order[j], order[index]];
    act(api.reorderDays, tripId, order);
  }

  async function resolveSlot(action, replacementId = '') {
    const m = slotModal;
    setSlotModal(null);
    if (m) await act(api.editSlot, tripId, m.day, m.slot, action, replacementId);
  }

  async function pay() {
    setBusy(true);
    setError('');
    try {
      const res = await api.payShare(tripId);
      setReceipt(res.receipt);
      setEmailInfo(res.email);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  /* ---------- action strip (always visible) ---------- */

  function ActionStrip() {
    let statusText, action = null;
    if (status === 'booked') {
      statusText = (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700 }}>
          <PartyPopper size={16} color="var(--clay)" /> Booked — everyone approved and paid
        </span>
      );
    } else if (status === 'approved') {
      statusText = <span className="muted">{trip.paid_count} of {trip.total_members} paid</span>;
      action = !iPaid && (
        <button className="btn clay" onClick={() => { setTab('money'); pay(); }} disabled={busy}>
          {busy ? <Loader2 className="spin" size={15} /> : <CreditCard size={15} />} Pay my share — ${myCheck?.total.toLocaleString()}
        </button>
      );
    } else {
      statusText = <span className="muted">{trip.approved_count} of {trip.total_members} approved</span>;
      action = !iApproved ? (
        <button className="btn" onClick={() => act(api.approvePlan, tripId)} disabled={busy}>
          Approve this plan <Check size={15} strokeWidth={2.5} />
        </button>
      ) : (
        <span className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
          You approved — waiting on the others.
        </span>
      );
    }
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 18, flexWrap: 'wrap',
        padding: '16px 0', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)',
        marginBottom: 28,
      }}>
        <span style={{ display: 'inline-flex', gap: 6 }}>
          {trip.members.map(m => (
            <span key={m.user_id} title={`${m.name}${m.approved ? ' — approved' : ''}${m.paid ? ' & paid' : ''}`}
              style={{
                width: 26, height: 26, borderRadius: '50%', display: 'inline-flex',
                alignItems: 'center', justifyContent: 'center', fontSize: 11.5, fontWeight: 700,
                background: m.paid ? 'var(--pine)' : m.approved ? 'rgba(46,93,79,0.18)' : 'var(--bg-soft)',
                color: m.paid ? '#F6F2E8' : m.approved ? 'var(--pine)' : 'var(--muted)',
                border: '1px solid', borderColor: m.approved || m.paid ? 'var(--pine)' : 'var(--line)',
              }}>
              {m.name.slice(0, 1).toUpperCase()}
            </span>
          ))}
        </span>
        {statusText}
        <span style={{ flex: 1 }} />
        {editable && (
          <button className="btn-quiet" onClick={() => setEditOpen(o => !o)}>
            <Pencil size={13} /> Request a change
          </button>
        )}
        {action}
      </div>
    );
  }

  /* ---------- tab contents ---------- */

  function Overview() {
    return (
      <>
        <Section eyebrow="How the group decided" Icon={Scale}>
          {(plan.decisions || []).map(d => (
            <div key={d.category} style={{ display: 'flex', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)' }}>
              <span className="eyebrow" style={{ minWidth: 86, fontSize: 10.5, paddingTop: 3 }}>{d.category}</span>
              <div>
                <p style={{ fontWeight: 700, fontSize: 15 }}>{d.choice}</p>
                <p className="muted" style={{ fontSize: 14 }}>{d.rationale}</p>
              </div>
            </div>
          ))}
        </Section>

        {(plan.revision_notes || []).length > 0 && (
          <Section eyebrow="Changes so far" Icon={Pencil}>
            {plan.revision_notes.map((n, i) => (
              <p key={i} className="serif" style={{ fontSize: 15, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 7 }}>— {n}</p>
            ))}
          </Section>
        )}

        {(plan.tips || []).length > 0 && (
          <Section eyebrow="Good to know" Icon={Lightbulb}>
            {plan.tips.map((tip, i) => (
              <p key={i} className="serif" style={{ fontSize: 15.5, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 9, maxWidth: 620 }}>
                {tip}
              </p>
            ))}
          </Section>
        )}
      </>
    );
  }

  function Itinerary() {
    return (
      <Section eyebrow={`Day by day — “${plan.itinerary?.title || 'the plan'}”`} Icon={Sun}>
        {editable && (
          <p className="muted" style={{ fontSize: 13.5, marginBottom: 8, fontStyle: 'italic' }}>
            This itinerary is yours to shape: reorder whole days with the side arrows, nudge an
            activity earlier/later within its day, or remove it (Rovyn will offer replacements).
            Any change asks the group to re-approve.
          </p>
        )}
        {days.map((day, i) => (
          <div key={`${day.day}-${day.date}`} style={{ display: 'grid', gridTemplateColumns: '86px 1fr auto', gap: 20, padding: '20px 0', borderTop: '1px solid var(--line-soft)' }}>
            <div>
              <div className="serif" style={{ fontSize: 22, color: 'var(--pine)' }}>Day {day.day}</div>
              <div className="muted" style={{ fontSize: 12 }}>{day.date}</div>
            </div>
            <div>
              <h3 className="serif" style={{ fontSize: 17.5, fontStyle: 'italic', marginBottom: 8 }}>{day.theme}</h3>
              {[['morning', Sun], ['afternoon', Sunset], ['evening', MoonStar]].map(([slot, Icon], si) => (
                day[slot] ? (
                  <div key={slot} style={{ display: 'flex', gap: 12, marginBottom: 6, alignItems: 'flex-start' }}>
                    <Icon size={14} strokeWidth={1.7} color="var(--clay)" style={{ marginTop: 4, flexShrink: 0 }} />
                    <p style={{ flex: 1, fontSize: 14, color: 'var(--ink-soft)' }}>{day[slot]}</p>
                    {editable && (
                      <span style={{ display: 'inline-flex', gap: 6, flexShrink: 0, marginTop: 2 }}>
                        <button className="btn-quiet" title="Move earlier in the day" disabled={busy || si === 0}
                                style={{ padding: 2 }}
                                onClick={() => act(api.editSlot, tripId, day.day, slot, 'move_up')}>
                          <ArrowUp size={13} />
                        </button>
                        <button className="btn-quiet" title="Move later in the day" disabled={busy || si === 2}
                                style={{ padding: 2 }}
                                onClick={() => act(api.editSlot, tripId, day.day, slot, 'move_down')}>
                          <ArrowDown size={13} />
                        </button>
                        <button className="btn-quiet" title="Remove this" disabled={busy}
                                style={{ padding: 2, color: 'var(--clay)' }}
                                onClick={() => setSlotModal({ day: day.day, slot, label: day[slot] })}>
                          <X size={13} />
                        </button>
                      </span>
                    )}
                  </div>
                ) : null
              ))}
            </div>
            {editable ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <button className="btn-quiet" onClick={() => moveDay(i, -1)} disabled={busy || i === 0} style={{ padding: 4 }}>
                  <ArrowUp size={15} />
                </button>
                <button className="btn-quiet" onClick={() => moveDay(i, 1)} disabled={busy || i === days.length - 1} style={{ padding: 4 }}>
                  <ArrowDown size={15} />
                </button>
              </div>
            ) : <span />}
          </div>
        ))}
      </Section>
    );
  }

  function Travel() {
    return (
      <>
        {Object.keys(plan.flights || {}).length > 0 && (
          <Section eyebrow="Flights — one per departure city" Icon={PlaneTakeoff}>
            {Object.entries(plan.flights).map(([city, f]) => (
              <div key={city} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)', flexWrap: 'wrap' }}>
                <div>
                  <p style={{ fontWeight: 700, fontSize: 15 }}>From {city} — {f.airline}</p>
                  <p className="muted" style={{ fontSize: 13.5 }}>{f.route} · out {f.depart} · back {f.return}</p>
                </div>
                <span className="serif" style={{ fontSize: 22, color: 'var(--pine)' }}>${f.est_price_per_person}</span>
              </div>
            ))}
          </Section>
        )}

        {plan.stay?.name && (
          <Section eyebrow="Where you'll rest" Icon={BedDouble}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
              <div>
                <h2 style={{ fontSize: 24, marginBottom: 4 }}>{plan.stay.name}</h2>
                <p className="muted" style={{ fontSize: 14, display: 'flex', alignItems: 'center', gap: 6 }}>
                  {plan.stay.type}{plan.stay.area ? ` · ${plan.stay.area}` : ''}
                  {plan.stay.rating ? <><Star size={13} fill="var(--clay)" color="var(--clay)" /> {plan.stay.rating}</> : null}
                </p>
                {(plan.stay.amenities || []).length > 0 && (
                  <p className="muted" style={{ fontSize: 13.5, marginTop: 6 }}>{plan.stay.amenities.join(' · ')}</p>
                )}
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="serif" style={{ fontSize: 27, color: 'var(--pine)' }}>${plan.stay.price_per_night}<span style={{ fontSize: 14 }}> / night</span></div>
                <div className="muted" style={{ fontSize: 13 }}>≈ ${plan.stay.total_per_person} per person total</div>
              </div>
            </div>
          </Section>
        )}

        {(plan.visits || []).length > 0 && (
          <Section eyebrow="Visits the group starred" Icon={Landmark}>
            {plan.visits.map(v => (
              <div key={v.id || v.name} style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '11px 0', borderTop: '1px solid var(--line-soft)' }}>
                <div>
                  <p style={{ fontWeight: 600, fontSize: 14.5 }}>{v.name}</p>
                  <p className="muted" style={{ fontSize: 13 }}>{v.description}</p>
                </div>
                <span className="muted" style={{ fontSize: 13.5, whiteSpace: 'nowrap' }}>{v.cost_per_person > 0 ? `$${v.cost_per_person} pp` : 'free'}</span>
              </div>
            ))}
          </Section>
        )}

        {(plan.events || []).length > 0 && (
          <Section eyebrow="Events on the calendar" Icon={Ticket}>
            {plan.events.map(e => (
              <div key={e.id || e.name} style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '11px 0', borderTop: '1px solid var(--line-soft)' }}>
                <div>
                  <p style={{ fontWeight: 600, fontSize: 14.5 }}>{e.name}</p>
                  <p className="muted" style={{ fontSize: 13 }}>{e.description}{e.date_hint ? ` — ${e.date_hint}` : ''}</p>
                </div>
                <span className="muted" style={{ fontSize: 13.5, whiteSpace: 'nowrap' }}>{e.cost_per_person > 0 ? `$${e.cost_per_person} pp` : 'free'}</span>
              </div>
            ))}
          </Section>
        )}
      </>
    );
  }

  function Money() {
    return (
      <>
        <Section eyebrow="Cost per person — shared, split equally" Icon={Wallet}>
          {Object.entries(COST_LABELS).map(([key, label]) => (
            (plan.cost_per_person?.[key] || 0) > 0 ? (
              <div key={key} style={{ display: 'flex', justifyContent: 'space-between', padding: '9px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 14.5 }}>
                <span className="muted">{label}</span>
                <span style={{ fontWeight: 600 }}>${plan.cost_per_person[key]}</span>
              </div>
            ) : null
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '14px 0 0', alignItems: 'baseline' }}>
            <span style={{ fontWeight: 700 }}>Shared total</span>
            <span className="serif" style={{ fontSize: 26, color: 'var(--pine)' }}>${plan.cost_per_person?.shared_total || 0}</span>
          </div>
          <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            Flights are added per traveler from their own departure city — totals differ per person.
          </p>
        </Section>

        {myCheck && (
          <Section eyebrow="The check — your share" Icon={Receipt}>
            {myCheck.items.map(item => (
              <div key={item.label} style={{ display: 'flex', justifyContent: 'space-between', padding: '9px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 14.5 }}>
                <span className="muted">{item.label}</span>
                <span style={{ fontWeight: 600 }}>${item.amount.toLocaleString()}</span>
              </div>
            ))}
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '14px 0 20px', alignItems: 'baseline' }}>
              <span style={{ fontWeight: 700 }}>Your total</span>
              <span className="serif" style={{ fontSize: 30, color: 'var(--pine)' }}>${myCheck.total.toLocaleString()}</span>
            </div>

            {['approved', 'booked'].includes(status) ? (
              !iPaid ? (
                <button className="btn clay" onClick={pay} disabled={busy}>
                  {busy ? <Loader2 className="spin" size={16} /> : <CreditCard size={16} />} Pay my share — ${myCheck.total.toLocaleString()}
                </button>
              ) : (
                <div>
                  <p style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700, marginBottom: 6 }}>
                    <CheckCircle2 size={17} /> Paid{receipt ? ` — receipt ${receipt.receipt_no}` : ''}
                  </p>
                  {emailInfo && (
                    <p className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
                      <Mail size={14} />
                      {emailInfo.sent || emailInfo.status === 'sent'
                        ? 'A receipt email is on its way to your inbox.'
                        : 'Receipt saved here in Rovyn — add RESEND_API_KEY to Rovyn/.env to email real receipts.'}
                    </p>
                  )}
                </div>
              )
            ) : (
              <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
                Payment opens once all {trip.total_members} travelers approve the plan.
              </p>
            )}

            <div style={{ marginTop: 26 }}>
              <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10 }}>
                <Users size={11} style={{ verticalAlign: -1, marginRight: 6 }} />
                Group payment status — {trip.paid_count} of {trip.total_members}
              </p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                {checks.map(c => (
                  <span key={c.user_id} style={{
                    display: 'inline-flex', alignItems: 'center', gap: 7, padding: '7px 14px',
                    border: '1px solid', borderColor: c.paid ? 'var(--pine)' : 'var(--line)',
                    borderRadius: 999, fontSize: 13, fontWeight: 600,
                    color: c.paid ? 'var(--pine)' : 'var(--muted)',
                  }}>
                    {c.paid ? <CheckCircle2 size={13} /> : <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--line)' }} />}
                    {c.name} · ${c.total.toLocaleString()}
                  </span>
                ))}
              </div>
            </div>
          </Section>
        )}
      </>
    );
  }

  /* ---------- page ---------- */

  return (
    <Page width={760}>
      <p className="eyebrow">{trip?.name}</p>
      <h1 style={{ fontSize: 42, margin: '10px 0 4px' }}>
        {status === 'booked' ? <>You're going to <em style={{ color: 'var(--pine)' }}>{plan.destination}</em>!</> : `The plan — ${plan.destination}`}
      </h1>
      <p className="muted" style={{ marginBottom: 6 }}>
        {plan.country ? `${plan.country} · ` : ''}{plan.duration_days} days · {plan.dates?.start} → {plan.dates?.end}
      </p>
      {plan.summary && (
        <p className="serif" style={{ fontSize: 16.5, fontStyle: 'italic', color: 'var(--ink-soft)', maxWidth: 620, margin: '10px 0 0' }}>
          {plan.summary}
        </p>
      )}

      <JourneyTracker status={status} style={{ marginTop: 26, marginBottom: 0 }} />

      <ActionStrip />

      {editOpen && editable && (
        <div style={{ marginBottom: 28 }}>
          <p className="muted" style={{ fontSize: 14, marginBottom: 10, maxWidth: 520 }}>
            Anyone can request a final alteration — Rovyn revises the plan and everyone approves again.
          </p>
          <textarea
            placeholder='e.g. "Swap the Day 3 boat trip for the kayak tour" or "Add a rest morning on Day 4"'
            value={editText}
            onChange={e => setEditText(e.target.value)}
            style={{ minHeight: 74, resize: 'vertical', marginBottom: 12 }}
          />
          <div style={{ display: 'flex', gap: 18 }}>
            <button className="btn" onClick={submitEdit} disabled={busy || !editText.trim()}>
              {busy ? <Loader2 className="spin" size={15} /> : <Send size={15} />} Revise the plan
            </button>
            <button className="btn-quiet" onClick={() => setEditOpen(false)}>Cancel</button>
          </div>
        </div>
      )}

      <ErrorText>{error}</ErrorText>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 26, borderBottom: '1px solid var(--line)', marginBottom: 30, flexWrap: 'wrap' }}>
        {TABS.map(({ key, label, Icon }) => {
          const active = tab === key;
          return (
            <button
              key={key}
              onClick={() => setTab(key)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 7,
                padding: '10px 2px 12px', fontSize: 14.5, fontWeight: 700,
                color: active ? 'var(--ink)' : 'var(--muted)',
                borderBottom: active ? '2px solid var(--pine)' : '2px solid transparent',
                marginBottom: -1,
              }}
            >
              <Icon size={15} strokeWidth={2} />
              {label}
            </button>
          );
        })}
      </div>

      <motion.div key={tab} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3 }}>
        {tab === 'overview' && <Overview />}
        {tab === 'itinerary' && <Itinerary />}
        {tab === 'travel' && <Travel />}
        {tab === 'money' && <Money />}
      </motion.div>

      {/* Replace-or-free modal */}
      {slotModal && (
        <div
          onClick={() => setSlotModal(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(35, 49, 43, 0.45)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
          }}
        >
          <motion.div
            initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }}
            onClick={e => e.stopPropagation()}
            style={{
              background: 'var(--bg)', maxWidth: 540, width: '100%', maxHeight: '80vh',
              overflowY: 'auto', padding: '36px 38px 30px', borderRadius: 4,
              boxShadow: '0 24px 60px rgba(35, 49, 43, 0.35)',
            }}
          >
            <p className="eyebrow">Day {slotModal.day} · {slotModal.slot}</p>
            <h2 style={{ fontSize: 26, margin: '8px 0 6px' }}>Wish to replace this with something?</h2>
            <p className="muted" style={{ fontSize: 14, marginBottom: 18 }}>
              Removing “{slotModal.label}”. Pick something else from the group's board — or leave the slot free.
            </p>

            {(data.replacement_pool || []).map(o => (
              <button key={o.id} className="opt-row" onClick={() => resolveSlot('replace', o.id)}>
                <span style={{ flex: 1 }}>
                  <span className="opt-title" style={{ display: 'block' }}>{o.name}</span>
                  <span className="opt-desc">
                    {o.description}{o.date_hint ? ` — ${o.date_hint}` : ''}{o.duration ? ` · ${o.duration}` : ''}
                  </span>
                </span>
                <span className="muted" style={{ fontSize: 13, whiteSpace: 'nowrap' }}>
                  {o.cost_per_person > 0 ? `$${o.cost_per_person} pp` : 'free'}
                </span>
              </button>
            ))}
            {(data.replacement_pool || []).length === 0 && (
              <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15, marginBottom: 10 }}>
                Nothing left on the board to swap in — the group starred everything!
              </p>
            )}

            <div style={{ display: 'flex', gap: 22, marginTop: 24 }}>
              <button className="btn" onClick={() => resolveSlot('delete')}>Just leave it free</button>
              <button className="btn-quiet" onClick={() => setSlotModal(null)}>Cancel</button>
            </div>
          </motion.div>
        </div>
      )}
    </Page>
  );
}
