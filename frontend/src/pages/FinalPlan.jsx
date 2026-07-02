import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowDown, ArrowUp, BedDouble, Check, CheckCircle2, CreditCard, Landmark,
  Lightbulb, Loader2, Mail, MoonStar, PartyPopper, Pencil, PlaneTakeoff,
  Receipt, Scale, Send, Star, Sun, Sunset, Ticket, Users, Wallet, X,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText, HorizonArt, JourneyTracker } from '../ui.jsx';

const COST_LABELS = {
  stay: 'Accommodation (your share)',
  itinerary_activities: 'Itinerary activities',
  visits: 'Starred visits & sights',
  events: 'Starred events',
  meals_estimate: 'Meals (estimate)',
  transport_estimate: 'Local transport (estimate)',
};

function Section({ eyebrow, Icon, children, style }) {
  return (
    <section style={{ marginBottom: 60, ...style }}>
      <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 18 }}>
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

  return (
    <Page width={760}>
      <p className="eyebrow">{trip?.name}</p>
      <h1 style={{ fontSize: 48, margin: '10px 0 6px' }}>
        {status === 'booked' ? <>You're going to <em style={{ color: 'var(--pine)' }}>{plan.destination}</em>!</> : `The plan — ${plan.destination}`}
      </h1>
      <p className="muted" style={{ marginBottom: 8 }}>
        {plan.country ? `${plan.country} · ` : ''}{plan.duration_days} days · {plan.dates?.start} → {plan.dates?.end}
      </p>

      <JourneyTracker status={status} style={{ marginTop: 24, marginBottom: 24 }} />
      <div style={{ margin: '0 -8px 36px' }}><HorizonArt height={120} /></div>

      {plan.summary && (
        <p className="serif" style={{ fontSize: 19, fontStyle: 'italic', color: 'var(--ink-soft)', maxWidth: 620, marginBottom: 52 }}>
          {plan.summary}
        </p>
      )}

      <ErrorText>{error}</ErrorText>

      {/* Booked banner */}
      {status === 'booked' && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
          style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 52, color: 'var(--pine)' }}>
          <PartyPopper size={26} strokeWidth={1.6} color="var(--clay)" />
          <p className="serif" style={{ fontSize: 20, fontStyle: 'italic' }}>
            All {trip.total_members} travelers approved and paid — the trip is booked. Receipts are in everyone's inbox.
          </p>
        </motion.div>
      )}

      {/* How the group decided */}
      <Section eyebrow="How the group decided" Icon={Scale}>
        {(plan.decisions || []).map(d => (
          <div key={d.category} style={{ display: 'flex', gap: 16, padding: '16px 0', borderTop: '1px solid var(--line-soft)' }}>
            <span className="eyebrow" style={{ minWidth: 86, fontSize: 10.5, paddingTop: 3 }}>{d.category}</span>
            <div>
              <p style={{ fontWeight: 700, fontSize: 15 }}>{d.choice}</p>
              <p className="muted" style={{ fontSize: 14 }}>{d.rationale}</p>
            </div>
          </div>
        ))}
      </Section>

      {/* Flights */}
      {Object.keys(plan.flights || {}).length > 0 && (
        <Section eyebrow="Flights — one per departure city" Icon={PlaneTakeoff}>
          {Object.entries(plan.flights).map(([city, f]) => (
            <div key={city} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 16, padding: '16px 0', borderTop: '1px solid var(--line-soft)', flexWrap: 'wrap' }}>
              <div>
                <p style={{ fontWeight: 700, fontSize: 15.5 }}>From {city} — {f.airline}</p>
                <p className="muted" style={{ fontSize: 14 }}>{f.route} · out {f.depart} · back {f.return}</p>
              </div>
              <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${f.est_price_per_person}</span>
            </div>
          ))}
        </Section>
      )}

      {/* Stay */}
      {plan.stay?.name && (
        <Section eyebrow="Where you'll rest" Icon={BedDouble}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
            <div>
              <h2 style={{ fontSize: 27, marginBottom: 4 }}>{plan.stay.name}</h2>
              <p className="muted" style={{ fontSize: 14, display: 'flex', alignItems: 'center', gap: 6 }}>
                {plan.stay.type}{plan.stay.area ? ` · ${plan.stay.area}` : ''}
                {plan.stay.rating ? <><Star size={13} fill="var(--clay)" color="var(--clay)" /> {plan.stay.rating}</> : null}
              </p>
              {(plan.stay.amenities || []).length > 0 && (
                <p className="muted" style={{ fontSize: 13.5, marginTop: 6 }}>{plan.stay.amenities.join(' · ')}</p>
              )}
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="serif" style={{ fontSize: 30, color: 'var(--pine)' }}>${plan.stay.price_per_night}<span style={{ fontSize: 15 }}> / night</span></div>
              <div className="muted" style={{ fontSize: 13 }}>≈ ${plan.stay.total_per_person} per person total</div>
            </div>
          </div>
        </Section>
      )}

      {/* Day by day */}
      <Section eyebrow={`Day by day — “${plan.itinerary?.title || 'the plan'}”`} Icon={Sun}>
        {editable && (
          <p className="muted" style={{ fontSize: 13.5, marginBottom: 8, fontStyle: 'italic' }}>
            This itinerary is yours to shape: reorder whole days with the side arrows, nudge an
            activity earlier/later within its day, or remove it (Rovyn will offer replacements).
            Any change asks the group to re-approve.
          </p>
        )}
        {days.map((day, i) => (
          <div key={`${day.day}-${day.date}`} style={{ display: 'grid', gridTemplateColumns: '96px 1fr auto', gap: 22, padding: '26px 0', borderTop: '1px solid var(--line-soft)' }}>
            <div>
              <div className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>Day {day.day}</div>
              <div className="muted" style={{ fontSize: 12.5 }}>{day.date}</div>
            </div>
            <div>
              <h3 className="serif" style={{ fontSize: 19, fontStyle: 'italic', marginBottom: 10 }}>{day.theme}</h3>
              {[['morning', Sun], ['afternoon', Sunset], ['evening', MoonStar]].map(([slot, Icon], si) => (
                day[slot] ? (
                  <div key={slot} style={{ display: 'flex', gap: 12, marginBottom: 8, alignItems: 'flex-start' }}>
                    <Icon size={15} strokeWidth={1.7} color="var(--clay)" style={{ marginTop: 4, flexShrink: 0 }} />
                    <p style={{ flex: 1, fontSize: 14.5, color: 'var(--ink-soft)' }}>{day[slot]}</p>
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
            {editable && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <button className="btn-quiet" onClick={() => moveDay(i, -1)} disabled={busy || i === 0} style={{ padding: 4 }}>
                  <ArrowUp size={15} />
                </button>
                <button className="btn-quiet" onClick={() => moveDay(i, 1)} disabled={busy || i === days.length - 1} style={{ padding: 4 }}>
                  <ArrowDown size={15} />
                </button>
              </div>
            )}
          </div>
        ))}
      </Section>

      {/* Visits & events */}
      {(plan.visits || []).length > 0 && (
        <Section eyebrow="Visits the group starred" Icon={Landmark}>
          {plan.visits.map(v => (
            <div key={v.id || v.name} style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)' }}>
              <div>
                <p style={{ fontWeight: 600, fontSize: 15 }}>{v.name}</p>
                <p className="muted" style={{ fontSize: 13.5 }}>{v.description}</p>
              </div>
              <span className="muted" style={{ fontSize: 14, whiteSpace: 'nowrap' }}>{v.cost_per_person > 0 ? `$${v.cost_per_person} pp` : 'free'}</span>
            </div>
          ))}
        </Section>
      )}
      {(plan.events || []).length > 0 && (
        <Section eyebrow="Events on the calendar" Icon={Ticket}>
          {plan.events.map(e => (
            <div key={e.id || e.name} style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)' }}>
              <div>
                <p style={{ fontWeight: 600, fontSize: 15 }}>{e.name}</p>
                <p className="muted" style={{ fontSize: 13.5 }}>{e.description}{e.date_hint ? ` — ${e.date_hint}` : ''}</p>
              </div>
              <span className="muted" style={{ fontSize: 14, whiteSpace: 'nowrap' }}>{e.cost_per_person > 0 ? `$${e.cost_per_person} pp` : 'free'}</span>
            </div>
          ))}
        </Section>
      )}

      {/* Costs */}
      <Section eyebrow="Cost per person" Icon={Wallet}>
        {Object.entries(COST_LABELS).map(([key, label]) => (
          (plan.cost_per_person?.[key] || 0) > 0 ? (
            <div key={key} style={{ display: 'flex', justifyContent: 'space-between', padding: '11px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 15 }}>
              <span className="muted">{label}</span>
              <span style={{ fontWeight: 600 }}>${plan.cost_per_person[key]}</span>
            </div>
          ) : null
        ))}
        <div style={{ display: 'flex', justifyContent: 'space-between', padding: '16px 0 0', alignItems: 'baseline' }}>
          <span style={{ fontWeight: 700 }}>Shared costs, split equally</span>
          <span className="serif" style={{ fontSize: 30, color: 'var(--pine)' }}>${plan.cost_per_person?.shared_total || 0}</span>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
          Flights are added per traveler from their own departure city — your exact total is on your check below.
        </p>
      </Section>

      {/* Revision notes */}
      {(plan.revision_notes || []).length > 0 && (
        <Section eyebrow="Changes so far" Icon={Pencil}>
          {plan.revision_notes.map((n, i) => (
            <p key={i} className="serif" style={{ fontSize: 15.5, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 8 }}>— {n}</p>
          ))}
        </Section>
      )}

      {/* Tips */}
      {(plan.tips || []).length > 0 && (
        <Section eyebrow="Good to know" Icon={Lightbulb}>
          {plan.tips.map((tip, i) => (
            <p key={i} className="serif" style={{ fontSize: 16, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 10, maxWidth: 620 }}>
              {tip}
            </p>
          ))}
        </Section>
      )}

      <hr className="rule" style={{ marginBottom: 44 }} />

      {/* Approval */}
      <Section eyebrow={`Approvals — ${trip.approved_count} of ${trip.total_members}`} Icon={Users}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 22 }}>
          {trip.members.map(m => (
            <span key={m.user_id} style={{
              display: 'inline-flex', alignItems: 'center', gap: 7, padding: '8px 16px',
              border: '1px solid var(--line)', borderRadius: 999, fontSize: 13.5, fontWeight: 600,
              color: m.approved ? 'var(--pine)' : 'var(--muted)',
              borderColor: m.approved ? 'var(--pine)' : 'var(--line)',
            }}>
              {m.approved ? <CheckCircle2 size={14} /> : <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--line)' }} />}
              {m.name}{m.user_id === me?.id ? ' (you)' : ''}
            </span>
          ))}
        </div>

        {status === 'final_review' && !iApproved && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 22, flexWrap: 'wrap', marginBottom: 10 }}>
            <button className="btn" onClick={() => act(api.approvePlan, tripId)} disabled={busy}>
              Approve this plan <Check size={16} strokeWidth={2.5} />
            </button>
            <span className="muted" style={{ fontSize: 14 }}>Payment opens when all {trip.total_members} approve.</span>
          </div>
        )}
        {status === 'final_review' && iApproved && (
          <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 16 }}>
            You approved — waiting on {trip.total_members - trip.approved_count} more traveler{trip.total_members - trip.approved_count === 1 ? '' : 's'}.
          </p>
        )}

        {/* Request a change */}
        {editable && (
          <div style={{ marginTop: 26 }}>
            {!editOpen ? (
              <button className="btn-quiet" onClick={() => setEditOpen(true)}>
                <Pencil size={14} /> Request a change to the plan
              </button>
            ) : (
              <div>
                <p className="muted" style={{ fontSize: 14, marginBottom: 10, maxWidth: 520 }}>
                  Anyone can request a final alteration — Rovyn revises the plan and everyone approves again.
                </p>
                <textarea
                  placeholder='e.g. "Swap the Day 3 boat trip for the kayak tour" or "Add a rest morning on Day 4"'
                  value={editText}
                  onChange={e => setEditText(e.target.value)}
                  style={{ minHeight: 80, resize: 'vertical', marginBottom: 14 }}
                />
                <div style={{ display: 'flex', gap: 18 }}>
                  <button className="btn" onClick={submitEdit} disabled={busy || !editText.trim()}>
                    {busy ? <Loader2 className="spin" size={15} /> : <Send size={15} />} Revise the plan
                  </button>
                  <button className="btn-quiet" onClick={() => setEditOpen(false)}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        )}
        {anyPaid && status !== 'booked' && (
          <p className="muted" style={{ fontSize: 13.5, marginTop: 16, fontStyle: 'italic' }}>
            Payments have started — the plan is locked.
          </p>
        )}
      </Section>

      {/* The check & payment */}
      {['approved', 'booked'].includes(status) && myCheck && (
        <Section eyebrow="The check — your share" Icon={Receipt}>
          <p className="muted" style={{ fontSize: 14, marginBottom: 16, maxWidth: 520 }}>
            Shared costs are split equally across all {trip.total_members} travelers.
            Your flight is priced from your departure city.
          </p>
          {myCheck.items.map(item => (
            <div key={item.label} style={{ display: 'flex', justifyContent: 'space-between', padding: '11px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 15 }}>
              <span className="muted">{item.label}</span>
              <span style={{ fontWeight: 600 }}>${item.amount.toLocaleString()}</span>
            </div>
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '18px 0 26px', alignItems: 'baseline' }}>
            <span style={{ fontWeight: 700 }}>Your total</span>
            <span className="serif" style={{ fontSize: 34, color: 'var(--pine)' }}>${myCheck.total.toLocaleString()}</span>
          </div>

          {!iPaid ? (
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
          )}

          <div style={{ marginTop: 30 }}>
            <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10 }}>Group payment status — {trip.paid_count} of {trip.total_members}</p>
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
