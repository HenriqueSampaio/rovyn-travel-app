import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion, Reorder, useDragControls } from 'framer-motion';
import {
  BedDouble, CalendarDays, Check, CheckCircle2, ClipboardCheck, Compass,
  CreditCard, ExternalLink, Globe, GripVertical, Landmark, Lightbulb, Loader2,
  Lock, Mail, MoonStar, PartyPopper, Pencil, PlaneTakeoff, Receipt, Scale,
  Send, Star, Sun, Sunset, Ticket, Undo2, Users, Wallet, X,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText, JourneyTracker, Hero, Thumb, ThinkingLoader } from '../ui.jsx';

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
const BOOKINGS_TAB = { key: 'bookings', label: 'Bookings', Icon: ClipboardCheck };

function sellerLabel(link) {
  try {
    const host = new URL(link).hostname.replace('www.', '');
    const known = {
      'expedia.com': 'Expedia', 'booking.com': 'Booking.com', 'airbnb.com': 'Airbnb',
      'viator.com': 'Viator', 'getyourguide.com': 'GetYourGuide',
    };
    return known[host] || host;
  } catch { return 'the seller'; }
}

const BOOKING_GROUPS = [
  { category: 'flight', label: 'Flights', Icon: PlaneTakeoff,
    hint: 'Each departure city books its own flight — anyone from that city can record it for the group.' },
  { category: 'stay', label: 'Stay', Icon: BedDouble,
    hint: 'One booking for the whole group — usually made by one person.' },
  { category: 'visit', label: 'Visits', Icon: Landmark, hint: '' },
  { category: 'event', label: 'Events', Icon: Ticket, hint: '' },
];

function BookingsChecklist({ bookings, busy, onConfirm, onUnbook }) {
  const [openKey, setOpenKey] = useState(null);
  const [code, setCode] = useState('');
  const [note, setNote] = useState('');

  if (!bookings) return <Loading text="Preparing your booking checklist…" />;
  const { items, total, booked } = bookings;

  async function submit(itemKey) {
    const ok = await onConfirm(itemKey, code.trim(), note.trim());
    if (ok) {
      setOpenKey(null);
      setCode('');
      setNote('');
    }
  }

  return (
    <>
      <p className="muted" style={{ maxWidth: 560, marginBottom: 6 }}>
        Rovyn doesn't charge for travel yet — book each item directly with the seller,
        then record the confirmation here so the whole group sees what's done.
      </p>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 26 }}>
        <div style={{ flex: 1, maxWidth: 260, height: 2, background: 'var(--line-soft)' }}>
          <div style={{ width: `${total ? (booked / total) * 100 : 0}%`, height: '100%', background: 'var(--pine)' }} />
        </div>
        <span className="muted" style={{ fontSize: 13.5, fontWeight: 600 }}>{booked} of {total} booked</span>
      </div>

      {BOOKING_GROUPS.map(({ category, label, Icon, hint }) => {
        const group = items.filter(i => i.category === category);
        if (group.length === 0) return null;
        return (
          <section key={category} style={{ marginBottom: 36 }}>
            <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <Icon size={13} strokeWidth={2.5} /> {label}
            </p>
            {hint && <p className="muted" style={{ fontSize: 13, fontStyle: 'italic', marginBottom: 4 }}>{hint}</p>}
            {group.map(item => (
              <div key={item.item_key} style={{ padding: '16px 0', borderTop: '1px solid var(--line-soft)' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14, flexWrap: 'wrap' }}>
                  <div style={{ flex: 1, minWidth: 220 }}>
                    <p style={{ fontWeight: 600, fontSize: 15 }}>
                      {item.title}
                      {item.category === 'flight' && item.mine && !item.stale && (
                        <span style={{ marginLeft: 10, fontSize: 11, color: 'var(--clay)', fontWeight: 700, letterSpacing: '0.08em' }}>YOURS</span>
                      )}
                      {item.stale && (
                        <span style={{ marginLeft: 10, fontSize: 11, color: 'var(--clay)', fontWeight: 700, letterSpacing: '0.08em', border: '1px solid var(--clay)', borderRadius: 999, padding: '2px 8px' }}>
                          NO LONGER IN THE PLAN
                        </span>
                      )}
                    </p>
                    {item.stale && (
                      <p className="muted" style={{ fontSize: 13, marginTop: 3 }}>
                        The plan changed after this was booked — cancel it with the seller, or keep it and tap undo to clear it from the list.
                      </p>
                    )}
                    {item.status === 'booked' ? (
                      <p style={{ fontSize: 13.5, color: 'var(--pine)', display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
                        <CheckCircle2 size={14} /> Booked by {item.booked_by} — confirmation <strong>{item.confirmation}</strong>
                        {item.note ? <span className="muted">· {item.note}</span> : null}
                      </p>
                    ) : (
                      <p className="muted" style={{ fontSize: 13, marginTop: 3 }}>Not booked yet</p>
                    )}
                  </div>
                  <span style={{ display: 'inline-flex', gap: 12, alignItems: 'center', flexShrink: 0 }}>
                    {item.link && (
                      <a className="ext-link" href={item.link} target="_blank" rel="noreferrer" style={{ padding: '8px 16px', fontSize: 13 }}>
                        <ExternalLink size={13} /> Book on {sellerLabel(item.link)}
                      </a>
                    )}
                    {item.status === 'booked' ? (
                      <button className="btn-quiet" disabled={busy} onClick={() => onUnbook(item.item_key)} title="Mark as not booked">
                        <Undo2 size={13} /> undo
                      </button>
                    ) : (
                      <button className="btn-quiet" disabled={busy}
                              onClick={() => { setOpenKey(openKey === item.item_key ? null : item.item_key); setCode(''); setNote(''); }}>
                        <Check size={14} /> Mark as booked
                      </button>
                    )}
                  </span>
                </div>
                {openKey === item.item_key && item.status !== 'booked' && (
                  <div style={{ marginTop: 12, maxWidth: 480 }}>
                    <input
                      placeholder="Confirmation code (PNR, booking number, voucher #…)"
                      value={code} onChange={e => setCode(e.target.value)}
                      style={{ marginBottom: 10 }}
                    />
                    <input
                      placeholder="Optional note (e.g. booked for me and Julia)"
                      value={note} onChange={e => setNote(e.target.value)}
                      style={{ marginBottom: 14 }}
                    />
                    <div style={{ display: 'flex', gap: 16 }}>
                      <button className="btn" disabled={busy || !code.trim()} onClick={() => submit(item.item_key)}>
                        Save confirmation <Check size={14} strokeWidth={2.5} />
                      </button>
                      <button className="btn-quiet" onClick={() => setOpenKey(null)}>Cancel</button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </section>
        );
      })}
    </>
  );
}

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

/* ---------- drag-and-drop itinerary ---------- */

const SLOT_KEYS = ['morning', 'afternoon', 'evening'];
const SLOT_ICONS = [Sun, Sunset, MoonStar];

function DayBody({ day, editable, busy, locks, slotOrder, onSlotReorder, onDragStart, onSlotCommit, onDeleteSlot }) {
  return (
    <div>
      <h3 className="serif" style={{ fontSize: 17.5, fontStyle: 'italic', marginBottom: 8 }}>{day.theme}</h3>
      <Reorder.Group as="div" axis="y" values={slotOrder} onReorder={onSlotReorder} style={{ margin: 0, padding: 0 }}>
        {slotOrder.map((srcKey, i) => {
          const Icon = SLOT_ICONS[i];
          const text = day[srcKey] || '';
          const locked = Boolean(locks?.[srcKey]);
          const draggable = editable && !locked && Boolean(text);
          return (
            <Reorder.Item
              key={srcKey} value={srcKey} as="div"
              dragListener={draggable}
              onDragStart={onDragStart}
              onDragEnd={() => onSlotCommit(day.day)}
              whileDrag={{ scale: 1.02, boxShadow: '0 10px 28px rgba(35, 49, 43, 0.22)', zIndex: 6 }}
              style={{
                display: 'flex', gap: 12, marginBottom: 6, alignItems: 'flex-start',
                position: 'relative', background: 'var(--bg)', borderRadius: 4, padding: '2px 4px',
                cursor: draggable ? 'grab' : 'default', touchAction: draggable ? 'none' : 'auto',
              }}
            >
              <Icon size={14} strokeWidth={1.7} color="var(--clay)" style={{ marginTop: 4, flexShrink: 0 }} />
              <p style={{ flex: 1, fontSize: 14, color: 'var(--ink-soft)' }}>
                {text || <em className="muted">Free</em>}
              </p>
              {locked ? (
                <span title="Fixed to the calendar — can't be moved or removed" style={{
                  display: 'inline-flex', alignItems: 'center', gap: 5, flexShrink: 0, marginTop: 3,
                  fontSize: 11, fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)',
                }}>
                  <Lock size={11} strokeWidth={2.2} /> fixed
                </span>
              ) : editable && text ? (
                <span style={{ display: 'inline-flex', gap: 8, flexShrink: 0, marginTop: 2, alignItems: 'center' }}>
                  <GripVertical size={14} color="var(--muted)" style={{ cursor: 'grab' }} />
                  <button className="btn-quiet" title="Remove this" disabled={busy}
                          style={{ padding: 2, color: 'var(--clay)' }}
                          onClick={() => onDeleteSlot(day.day, srcKey, text)}>
                    <X size={13} />
                  </button>
                </span>
              ) : null}
            </Reorder.Item>
          );
        })}
      </Reorder.Group>
    </div>
  );
}

function DayLabel({ day, tag, handle }) {
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {handle}
        <div className="serif" style={{ fontSize: 22, color: 'var(--pine)' }}>Day {day.day}</div>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{day.date}</div>
      {tag && (
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 5, marginTop: 5,
          fontSize: 10.5, fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)',
        }}>
          <Lock size={10} strokeWidth={2.2} /> {tag}
        </span>
      )}
    </div>
  );
}

const DAY_ROW_STYLE = {
  display: 'grid', gridTemplateColumns: '96px 1fr', gap: 20,
  padding: '20px 0', borderTop: '1px solid var(--line-soft)',
};

function DraggableDay({ day, onDragStart, onDragEnd, children }) {
  const controls = useDragControls();
  const handle = (
    <GripVertical
      size={15} color="var(--muted)"
      style={{ cursor: 'grab', touchAction: 'none', flexShrink: 0 }}
      onPointerDown={e => controls.start(e)}
    />
  );
  return (
    <Reorder.Item
      as="div" value={day.day} dragListener={false} dragControls={controls}
      onDragStart={onDragStart} onDragEnd={onDragEnd}
      whileDrag={{ scale: 1.01, boxShadow: '0 14px 40px rgba(35, 49, 43, 0.25)', zIndex: 10 }}
      style={{ ...DAY_ROW_STYLE, position: 'relative', background: 'var(--bg)', borderRadius: 4 }}
    >
      {children(handle)}
    </Reorder.Item>
  );
}

function ItineraryDays({ plan, editable, busy, slotLocks, commitDays, commitSlots, deleteSlot }) {
  const days = plan.itinerary?.days || [];
  const sig = JSON.stringify(days.map(d => [d.day, d.date, d.morning, d.afternoon, d.evening]));
  const [middleIds, setMiddleIds] = useState(() => days.slice(1, -1).map(d => d.day));
  const [slotOrders, setSlotOrders] = useState({});
  const draggingRef = useRef(false);
  const middleRef = useRef(middleIds);
  middleRef.current = middleIds;
  const slotOrdersRef = useRef(slotOrders);
  slotOrdersRef.current = slotOrders;

  useEffect(() => {
    if (!draggingRef.current) {
      setMiddleIds(days.slice(1, -1).map(d => d.day));
      setSlotOrders({});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  if (days.length === 0) return null;

  const byId = Object.fromEntries(days.map(d => [d.day, d]));
  const first = days[0];
  const last = days.length > 1 ? days[days.length - 1] : null;
  const canDragDays = editable && days.length >= 3;
  const locksFor = d => slotLocks?.[String(d.day)] || {};

  const onDragStart = () => { draggingRef.current = true; };

  function daysDragEnd() {
    draggingRef.current = false;
    const order = [first.day, ...middleRef.current, ...(last ? [last.day] : [])];
    if (JSON.stringify(order) !== JSON.stringify(days.map(d => d.day))) commitDays(order);
  }

  function slotsDragEnd(dayNum) {
    draggingRef.current = false;
    const order = slotOrdersRef.current[dayNum] || SLOT_KEYS;
    if (JSON.stringify(order) === JSON.stringify(SLOT_KEYS)) return;
    const locks = locksFor(byId[dayNum]);
    const lockedMoved = SLOT_KEYS.some(k => locks[k] && order.indexOf(k) !== SLOT_KEYS.indexOf(k));
    if (lockedMoved) {
      // snap fixed moments back where they belong
      setSlotOrders(s => ({ ...s, [dayNum]: undefined }));
      return;
    }
    commitSlots(dayNum, order);
  }

  const renderBody = (d, dragHandle) => (
    <>
      <DayLabel
        day={d}
        handle={dragHandle}
        tag={d.day === first.day ? 'arrival' : last && d.day === last.day ? 'departure' : null}
      />
      <DayBody
        day={d} editable={editable} busy={busy} locks={locksFor(d)}
        slotOrder={slotOrders[d.day] || SLOT_KEYS}
        onSlotReorder={order => setSlotOrders(s => ({ ...s, [d.day]: order }))}
        onDragStart={onDragStart}
        onSlotCommit={slotsDragEnd}
        onDeleteSlot={deleteSlot}
      />
    </>
  );

  return (
    <div>
      {/* Arrival day — pinned */}
      <div style={DAY_ROW_STYLE}>{renderBody(first, null)}</div>

      {/* Middle days — drag by the grip to reorder */}
      {canDragDays ? (
        <Reorder.Group as="div" axis="y" values={middleIds} onReorder={setMiddleIds} style={{ margin: 0, padding: 0 }}>
          {middleIds.map(id => byId[id] && (
            <DraggableDay key={id} day={byId[id]} onDragStart={onDragStart} onDragEnd={daysDragEnd}>
              {handle => renderBody(byId[id], handle)}
            </DraggableDay>
          ))}
        </Reorder.Group>
      ) : (
        days.slice(1, -1).map(d => <div key={d.day} style={DAY_ROW_STYLE}>{renderBody(d, null)}</div>)
      )}

      {/* Departure day — pinned */}
      {last && <div style={DAY_ROW_STYLE}>{renderBody(last, null)}</div>}
    </div>
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
  const [revising, setRevising] = useState(false);
  const [showChanges, setShowChanges] = useState(false);
  const [slotModal, setSlotModal] = useState(null); // { day, slot, label }
  const [receipt, setReceipt] = useState(null);
  const [emailInfo, setEmailInfo] = useState(null);
  const [bookings, setBookings] = useState(null);
  const pollRef = useRef(null);
  const bookingsReqId = useRef(0);

  async function refreshBookings() {
    // Monotonic counter: a stale poll response can never overwrite a newer one
    const id = ++bookingsReqId.current;
    try {
      const b = await api.getBookings(tripId);
      if (id === bookingsReqId.current) setBookings(b);
    } catch { /* checklist may be momentarily locked (e.g. plan re-opened) */ }
  }

  async function load() {
    try {
      const d = await api.getPlan(tripId);
      if (!d.plan && !d.generating) {
        navigate(`/trips/${tripId}`);
        return;
      }
      setData(d);
      if (['approved', 'booked'].includes(d.status)) refreshBookings();
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

  // If the plan is re-opened while someone is on the Bookings tab, the tab
  // disappears — land them on Overview instead of a blank pane. (Must live
  // above the early returns: hooks can't be conditional.)
  const bookingsTabAllowed = ['approved', 'booked'].includes(data?.status);
  useEffect(() => {
    if (tab === 'bookings' && !bookingsTabAllowed) setTab('overview');
  }, [bookingsTabAllowed, tab]);

  if (!data) return <Page><Loading text="Opening the final plan…" /></Page>;
  if (!data.plan) {
    return (
      <Page>
        <ThinkingLoader centered messages={[
          'Rovyn is planning your trip…',
          'Counting everyone’s stars…',
          'Weaving your picks into the days…',
          'Rovyn is thinking…',
          'Balancing budgets and pace…',
          'Almost there — polishing the details…',
        ]} />
      </Page>
    );
  }

  const { plan, checks, trip } = data;
  const status = data.status;
  const anyPaid = checks.some(c => c.paid);
  const editable = !anyPaid && ['final_review', 'approved'].includes(status);
  const myCheck = checks.find(c => c.user_id === me?.id);
  const iApproved = trip?.me?.approved;
  const iPaid = trip?.me?.paid;
  const days = plan.itinerary?.days || [];
  const canBook = ['approved', 'booked'].includes(status);
  const visibleTabs = canBook ? [...TABS, BOOKINGS_TAB] : TABS;

  async function confirmBooking(itemKey, code, note) {
    setBusy(true);
    setError('');
    try {
      await api.confirmBooking(tripId, itemKey, code, note);
      await refreshBookings();
      return true;
    } catch (err) {
      setError(err.message);
      return false; // keep the form open so the pasted code isn't lost
    } finally {
      setBusy(false);
    }
  }

  async function unbookBooking(itemKey) {
    setBusy(true);
    setError('');
    try {
      await api.unbookBooking(tripId, itemKey);
      await refreshBookings();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

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
    setRevising(true);
    setError('');
    try {
      await api.editPlan(tripId, editText.trim());
      setEditText('');
      setEditOpen(false);
      await load();
    } catch (err) {
      setError(err.message); // includes "that change doesn't fit" explanations
    } finally {
      setBusy(false);
      setRevising(false);
    }
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
      if (bookings && bookings.booked < bookings.total) {
        action = (
          <button className="btn" onClick={() => setTab('bookings')}>
            <ClipboardCheck size={15} /> Book your trip — {bookings.booked} of {bookings.total} done
          </button>
        );
      }
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
    const notes = plan.revision_notes || [];
    return (
      <>
        <Section eyebrow="How the group decided" Icon={Scale}>
          {(plan.decisions || []).map(d => (
            <div key={d.category} style={{ display: 'flex', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)' }}>
              <span className="eyebrow" style={{ minWidth: 86, fontSize: 10.5, paddingTop: 3 }}>{d.category}</span>
              <div style={{ flex: 1 }}>
                <p style={{ fontWeight: 700, fontSize: 15 }}>{d.choice}</p>
                <p className="muted" style={{ fontSize: 14 }}>{d.rationale}</p>
                {(d.items || []).length > 0 && (
                  <ul style={{ margin: '8px 0 2px', padding: 0, listStyle: 'none' }}>
                    {d.items.map(item => (
                      <li key={item.name} style={{
                        display: 'flex', justifyContent: 'space-between', gap: 14,
                        padding: '5px 0', borderTop: '1px dashed var(--line-soft)', fontSize: 14,
                      }}>
                        <span style={{ color: 'var(--ink-soft)' }}>{item.name}</span>
                        {item.stars > 0 && (
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: 'var(--clay)', fontWeight: 700, whiteSpace: 'nowrap' }}>
                            {item.stars} <Star size={12} fill="var(--clay)" />
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ))}
        </Section>

        {notes.length > 0 && (
          <Section eyebrow="Plan history" Icon={Pencil}>
            {!showChanges ? (
              <button className="btn-quiet" onClick={() => setShowChanges(true)}>
                See changes that were made ({notes.length})
              </button>
            ) : (
              <>
                {notes.map((n, i) => (
                  <p key={i} className="serif" style={{ fontSize: 15, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 7 }}>— {n}</p>
                ))}
                <button className="btn-quiet" onClick={() => setShowChanges(false)} style={{ marginTop: 6 }}>Hide changes</button>
              </>
            )}
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
            This itinerary is yours to shape: drag whole days by their grip to reorder them, drag
            activities within a day to reshuffle the schedule, or remove one with ✕ (Rovyn will
            offer replacements). Moments marked <Lock size={11} style={{ verticalAlign: -1 }} /> —
            arrival, check-in, check-out, departure — are pinned to the calendar and stay put.
            Any change asks the group to re-approve.
          </p>
        )}
        <ItineraryDays
          plan={plan}
          editable={editable}
          busy={busy}
          slotLocks={data.slot_locks || {}}
          commitDays={order => act(api.reorderDays, tripId, order)}
          commitSlots={(day, order) => act(api.arrangeSlots, tripId, day, order)}
          deleteSlot={(day, slot, label) => setSlotModal({ day, slot, label })}
        />
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
            <Hero image={plan.stay.image} height={180} style={{ marginBottom: 16 }} />
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
                {plan.stay.official_website && (
                  <a className="btn-quiet" href={plan.stay.official_website} target="_blank" rel="noreferrer"
                     style={{ marginTop: 8, fontSize: 13.5 }}>
                    <Globe size={13} /> Official website
                  </a>
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
              <div key={v.id || v.name} style={{ display: 'flex', gap: 14, padding: '11px 0', borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start' }}>
                <Thumb image={v.image} size={54} />
                <div style={{ flex: 1 }}>
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
              <div key={e.id || e.name} style={{ display: 'flex', gap: 14, padding: '11px 0', borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start' }}>
                <Thumb image={e.image} size={54} />
                <div style={{ flex: 1 }}>
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

      <Hero image={plan.image} height={210} style={{ marginTop: 18 }} />

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
          {revising ? (
            <ThinkingLoader messages={[
              'Rovyn is reworking the plan…',
              'Fitting your change into the days…',
              'Rechecking the costs…',
              'Rovyn is thinking…',
            ]} />
          ) : (
            <div style={{ display: 'flex', gap: 18 }}>
              <button className="btn" onClick={submitEdit} disabled={busy || !editText.trim()}>
                <Send size={15} /> Revise the plan
              </button>
              <button className="btn-quiet" onClick={() => setEditOpen(false)}>Cancel</button>
            </div>
          )}
        </div>
      )}

      <ErrorText>{error}</ErrorText>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 26, borderBottom: '1px solid var(--line)', marginBottom: 30, flexWrap: 'wrap' }}>
        {visibleTabs.map(({ key, label, Icon }) => {
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
        {tab === 'bookings' && canBook && (
          <BookingsChecklist bookings={bookings} busy={busy} onConfirm={confirmBooking} onUnbook={unbookBooking} />
        )}
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
                <Thumb image={o.image} size={46} />
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
