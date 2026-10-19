import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { motion, Reorder, useDragControls } from 'framer-motion';
import {
  BedDouble, Bus, CalendarDays, Check, CheckCircle2, ClipboardCheck, Compass,
  CreditCard, ExternalLink, Globe, GripVertical, Landmark, Lightbulb, Loader2,
  Lock, Mail, MoonStar, PartyPopper, Pencil, PlaneTakeoff, Receipt, Scale,
  Send, Ship, Star, Sun, Sunset, Ticket, TrainFront, Undo2, Users, Wallet, X,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import Checkout from '../components/Checkout.jsx';
import { Page, Loading, ErrorText, JourneyTracker, Hero, MapImage, RouteTimeline, Thumb, ThinkingLoader, TripAvatar, useSwipeTabs } from '../ui.jsx';

const COST_LABELS = {
  stay: 'Accommodation (your share)',
  intercity_transport: 'Trains & flights between cities',
  itinerary_activities: 'Itinerary activities',
  visits: 'Starred visits & sights',
  events: 'Starred events',
  meals_estimate: 'Meals (estimate)',
  transport_estimate: 'Local transport (estimate)',
};

const MODE_ICONS = { train: TrainFront, flight: PlaneTakeoff, bus: Bus, ferry: Ship };

// The itinerary IS the plan — it leads; the how-we-decided overview closes.
const TABS = [
  { key: 'itinerary', label: 'Itinerary', Icon: CalendarDays },
  { key: 'travel', label: 'Travel & stay', Icon: PlaneTakeoff },
  { key: 'money', label: 'Costs & payment', Icon: Wallet },
];
const OVERVIEW_TAB = { key: 'overview', label: 'Overview', Icon: Compass };
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
  { category: 'transport', label: 'Between cities', Icon: TrainFront,
    hint: 'The group\'s chosen way to make each leg of the route — one booking covers everyone.' },
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
  // Items the open group checkout is buying: recording one by hand would book it twice
  const charged = new Set(bookings.charged_keys || []);

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
        {items.some(i => i.provider !== 'self')
          ? 'Rovyn booked the items marked "Booked by Rovyn". Book the rest directly with the seller, then record the confirmation here so the whole group sees what\'s done.'
          : 'Book each item directly with the seller, then record the confirmation here so the whole group sees what\'s done — or pay in Rovyn from Costs & payment and let Rovyn book the stay and flights.'}
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
                        <span style={{ marginLeft: 10, fontSize: 10.5, color: 'var(--sun)', background: 'rgba(169, 128, 43, 0.15)', fontWeight: 700, letterSpacing: '0.08em', borderRadius: 999, padding: '3px 9px', whiteSpace: 'nowrap', display: 'inline-block' }}>YOURS</span>
                      )}
                      {item.stale && (
                        <span style={{ marginLeft: 10, fontSize: 10.5, color: 'var(--clay)', background: 'rgba(190, 107, 63, 0.13)', fontWeight: 700, letterSpacing: '0.08em', borderRadius: 999, padding: '3px 9px', whiteSpace: 'nowrap', display: 'inline-block' }}>
                          NO LONGER IN THE PLAN
                        </span>
                      )}
                    </p>
                    {item.stale && (
                      <p className="muted" style={{ fontSize: 13, marginTop: 3 }}>
                        The plan changed after this was booked — cancel it with the seller, or keep it and tap undo to clear it from the list.
                      </p>
                    )}
                    {item.status === 'booked' || item.status === 'pending_confirmation' ? (
                      <p style={{ fontSize: 13.5, color: 'var(--pine)', display: 'flex', alignItems: 'center', gap: 6, marginTop: 3, flexWrap: 'wrap' }}>
                        <CheckCircle2 size={14} />
                        {item.provider !== 'self'
                          ? <>Booked by Rovyn{item.provider === 'simulated' ? ' (simulated)' : ''} — {item.status === 'pending_confirmation'
                              ? 'hotel confirmation code pending' : <>confirmation <strong>{item.confirmation}</strong></>}</>
                          : <>Booked by {item.booked_by} — confirmation <strong>{item.confirmation}</strong></>}
                        {item.note ? <span className="muted">· {item.note}</span> : null}
                      </p>
                    ) : (
                      <p className="muted" style={{ fontSize: 13, marginTop: 3 }}>Not booked yet{item.note ? ` — ${item.note}` : ''}</p>
                    )}
                  </div>
                  <span style={{ display: 'inline-flex', gap: 12, alignItems: 'center', flexShrink: 0 }}>
                    {item.link && item.provider === 'self' && !charged.has(item.item_key) && (
                      <a className="ext-link" href={item.link} target="_blank" rel="noreferrer" style={{ padding: '8px 16px', fontSize: 13 }}>
                        <ExternalLink size={13} /> Book on {sellerLabel(item.link)}
                      </a>
                    )}
                    {item.provider !== 'self' ? (
                      <span className="muted" style={{ fontSize: 12.5 }}>manage in Costs &amp; payment</span>
                    ) : item.status === 'booked' ? (
                      <button className="btn-quiet" disabled={busy} onClick={() => onUnbook(item.item_key)} title="Mark as not booked">
                        <Undo2 size={13} /> undo
                      </button>
                    ) : charged.has(item.item_key) ? (
                      <span className="muted" style={{ fontSize: 12.5 }}>Rovyn is booking this in the group checkout</span>
                    ) : (
                      <button className="btn-quiet" disabled={busy}
                              onClick={() => { setOpenKey(openKey === item.item_key ? null : item.item_key); setCode(''); setNote(''); }}>
                        <Check size={14} /> Mark as booked
                      </button>
                    )}
                  </span>
                </div>
                {openKey === item.item_key && item.status !== 'booked' && !charged.has(item.item_key) && (
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

const DAY_ROW_STYLE = { padding: '20px 0', borderTop: '1px solid var(--line-soft)' };

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
      className="day-row"
      style={{ ...DAY_ROW_STYLE, position: 'relative', background: 'var(--bg)', borderRadius: 4 }}
    >
      {children(handle)}
    </Reorder.Item>
  );
}

function fmtShort(iso) {
  if (!iso) return '';
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function CityBand({ city, days }) {
  const first = days[0];
  const last = days[days.length - 1];
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', gap: 14, flexWrap: 'wrap',
      margin: '36px 0 2px', paddingBottom: 8, borderBottom: '2px solid var(--pine)',
    }}>
      <span className="serif" style={{ fontSize: 23, fontStyle: 'italic', color: 'var(--pine)' }}>
        {city}
      </span>
      <span className="muted" style={{ fontSize: 12.5, fontWeight: 600 }}>
        Day{days.length > 1 ? `s ${first.day}–${last.day}` : ` ${first.day}`}
        {first.date ? ` · ${fmtShort(first.date)} → ${fmtShort(last.date)}` : ''}
      </span>
    </div>
  );
}

function ItineraryDays({ plan, editable, busy, slotLocks, commitDays, commitSlots, deleteSlot }) {
  const days = plan.itinerary?.days || [];
  const sig = JSON.stringify(days.map(d => [d.day, d.date, d.city, d.morning, d.afternoon, d.evening]));
  const [runOrders, setRunOrders] = useState({});
  const [slotOrders, setSlotOrders] = useState({});
  const draggingRef = useRef(false);
  const runOrdersRef = useRef(runOrders);
  runOrdersRef.current = runOrders;
  const slotOrdersRef = useRef(slotOrders);
  slotOrdersRef.current = slotOrders;

  useEffect(() => {
    if (!draggingRef.current) {
      setRunOrders({});
      setSlotOrders({});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  if (days.length === 0) return null;

  const byId = Object.fromEntries(days.map(d => [d.day, d]));
  const locksFor = d => slotLocks?.[String(d.day)] || {};
  const dayLocked = d => SLOT_KEYS.some(k => locksFor(d)[k]);
  const multiCity = new Set(days.map(d => d.city).filter(Boolean)).size > 1;

  // Days can be dragged only within their own city's stretch of the trip, and
  // days that hold a fixed travel moment (arrival, transfer, departure) are
  // pinned. Segments group the days by city; inside each, contiguous movable
  // days form a drag run.
  const isPinned = (d, idx) => idx === 0 || idx === days.length - 1 || dayLocked(d) || !editable;
  const segments = [];
  days.forEach((d, idx) => {
    const cityKey = d.city || '';
    let seg = segments[segments.length - 1];
    if (!seg || seg.city !== cityKey) {
      seg = { city: cityKey, days: [], items: [] };
      segments.push(seg);
    }
    seg.days.push(d);
    if (isPinned(d, idx)) {
      seg.items.push({ kind: 'pinned', day: d });
    } else {
      let run = seg.items[seg.items.length - 1];
      if (!run || run.kind !== 'run') {
        run = { kind: 'run', key: `run-${segments.length}-${seg.items.length}`, ids: [] };
        seg.items.push(run);
      }
      run.ids.push(d.day);
    }
  });

  const onDragStart = () => { draggingRef.current = true; };

  function daysDragEnd() {
    draggingRef.current = false;
    const order = segments.flatMap(seg => seg.items.flatMap(item =>
      item.kind === 'pinned' ? [item.day.day] : (runOrdersRef.current[item.key] || item.ids)
    ));
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

  const dayTag = d => {
    if (d.day === days[0].day) return 'arrival';
    if (days.length > 1 && d.day === days[days.length - 1].day) return 'departure';
    if (dayLocked(d)) return 'travel day';
    return null;
  };

  const renderBody = (d, dragHandle) => (
    <>
      <DayLabel day={d} handle={dragHandle} tag={dayTag(d)} />
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
      {segments.map((seg, si) => (
        <div key={`${seg.city}-${si}`}>
          {multiCity && seg.city && <CityBand city={seg.city} days={seg.days} />}
          {seg.items.map(item =>
            item.kind === 'pinned' ? (
              <div key={item.day.day} className="day-row" style={DAY_ROW_STYLE}>
                {renderBody(item.day, null)}
              </div>
            ) : (
              <Reorder.Group
                key={item.key} as="div" axis="y"
                values={runOrders[item.key] || item.ids}
                onReorder={ids => setRunOrders(s => ({ ...s, [item.key]: ids }))}
                style={{ margin: 0, padding: 0 }}
              >
                {(runOrders[item.key] || item.ids).map(id => byId[id] && (
                  <DraggableDay key={id} day={byId[id]} onDragStart={onDragStart} onDragEnd={daysDragEnd}>
                    {handle => renderBody(byId[id], handle)}
                  </DraggableDay>
                ))}
              </Reorder.Group>
            )
          )}
        </div>
      ))}
    </div>
  );
}

export default function FinalPlan() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const me = auth.user();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('itinerary');
  const [tabDir, setTabDir] = useState(0); // which way the content slides in
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
  const [searchParams, setSearchParams] = useSearchParams();
  const returnHandled = useRef(false);

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

  // Back from Stripe's hosted Checkout: land on the requested tab, confirm the
  // session with the backend (it re-reads Stripe), then clean the URL.
  useEffect(() => {
    if (returnHandled.current) return;
    const tabParam = searchParams.get('tab');
    const outcome = searchParams.get('checkout');
    const sessionId = searchParams.get('session_id');
    if (!tabParam && !outcome) return;
    returnHandled.current = true;
    if (tabParam) setTab(tabParam);
    const finish = () => setSearchParams({}, { replace: true });
    if (outcome === 'success' && sessionId) {
      api.confirmCheckout(tripId, sessionId).catch(err => setError(err.message)).finally(() => { load(); finish(); });
    } else {
      finish();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

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
  // disappears — land them on the itinerary instead of a blank pane. (Must
  // live above the early returns: hooks can't be conditional.)
  const bookingsTabAllowed = ['approved', 'booked'].includes(data?.status);
  useEffect(() => {
    if (tab === 'bookings' && !bookingsTabAllowed) setTab('itinerary');
  }, [bookingsTabAllowed, tab]);

  // Same swipe-between-tabs gesture as the planning board
  const visibleTabs = bookingsTabAllowed
    ? [...TABS, BOOKINGS_TAB, OVERVIEW_TAB]
    : [...TABS, OVERVIEW_TAB];
  const tabIdx = Math.max(0, visibleTabs.findIndex(t => t.key === tab));
  function goTab(key) {
    const to = visibleTabs.findIndex(t => t.key === key);
    setTabDir(to > tabIdx ? 1 : to < tabIdx ? -1 : 0);
    setTab(key);
  }
  const swipeRef = useSwipeTabs(visibleTabs.length, tabIdx, i => goTab(visibleTabs[i].key));

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
  // The server's own lock (an open group checkout, or any payment in motion) —
  // a pending or authorized share never reads as "paid", so guessing from the
  // checks left edit controls up that every edit endpoint refused. Older
  // backends don't send it: fall back to "someone has paid".
  const anyPaid = checks.some(c => c.paid);
  const planLocked = data.plan_locked === undefined ? anyPaid : data.plan_locked;
  const editable = !planLocked && ['final_review', 'approved'].includes(status);
  const myCheck = checks.find(c => c.user_id === me?.id);
  const iApproved = trip?.me?.approved;
  const iPaid = trip?.me?.paid;
  const iAuthorized = trip?.me?.payment_status === 'authorized' || trip?.me?.payment_status === 'nothing_due';
  const iPending = trip?.me?.payment_status === 'pending';
  const selfBooked = trip?.me?.booking_mode === 'self_book' || receipt?.mode === 'self_book';
  const days = plan.itinerary?.days || [];
  const canBook = ['approved', 'booked'].includes(status);

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

  async function chooseBooking(mode) {
    setBusy(true);
    setError('');
    try {
      const res = await api.payShare(tripId, mode);
      setReceipt(res.receipt);
      setEmailInfo(res.email);
      await load();
      if (mode === 'self_book') {
        await refreshBookings();
        goTab('bookings'); // straight to the checklist they just committed to
      }
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
          <PartyPopper size={16} color="var(--clay)" /> Locked in — everyone approved and chose how to book
        </span>
      );
      if (bookings && bookings.booked < bookings.total) {
        action = (
          <button className="btn" onClick={() => goTab('bookings')}>
            <ClipboardCheck size={15} /> Book your trip — {bookings.booked} of {bookings.total} done
          </button>
        );
      }
    } else if (status === 'approved') {
      const committed = trip.paid_count + (trip.authorized_count || 0);
      statusText = <span className="muted">{committed} of {trip.total_members} locked in</span>;
      action = !iPaid && !iAuthorized ? (
        <button className="btn clay" onClick={() => goTab('money')} disabled={busy}>
          <Wallet size={15} /> {iPending ? 'Finish paying' : 'Choose how to book'} — ${myCheck?.total.toLocaleString()}
        </button>
      ) : (
        <span className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
          {iPaid ? "You're locked in — waiting on the others." : 'Your share is authorized — waiting on the others.'}
        </span>
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
      <div className="action-strip">
        <span style={{ display: 'inline-flex', gap: 6 }}>
          {trip.members.map(m => (
            <span key={m.user_id} title={`${m.name}${m.approved ? ' — approved' : ''}${m.paid ? (m.booking_mode === 'self_book' ? ' & booking solo' : ' & paid') : ''}`}
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
        <MapImage
          load={() => api.routeMap(tripId)}
          alt={plan.route ? 'Route map' : `Map of ${plan.destination}`}
          style={{ margin: '4px 0 18px' }}
          embed={plan.route
            ? { mode: 'directions', params: {
                origin: plan.route.cities[0].city,
                destination: plan.route.cities[plan.route.cities.length - 1].city,
                ...(plan.route.cities.length > 2
                  ? { waypoints: plan.route.cities.slice(1, -1).map(c => c.city).join('|') } : {}),
                mode: 'transit',
              } }
            : { mode: 'place', params: { q: plan.destination } }}
        />
        {plan.route && (
          <div style={{ margin: '4px 0 22px' }}>
            <RouteTimeline route={plan.route} />
          </div>
        )}
        {editable && (
          <p className="muted" style={{ fontSize: 13.5, marginBottom: 8, fontStyle: 'italic' }}>
            This itinerary is yours to shape: drag whole days by their grip to reorder them, drag
            activities within a day to reshuffle the schedule, or remove one with ✕ (Rovyn will
            offer replacements). Moments marked <Lock size={11} style={{ verticalAlign: -1 }} /> —
            arrival, check-in, check-out, departure{plan.route ? ', travel between cities' : ''} — are
            pinned to the calendar and stay put.
            {plan.route ? ' Days trade places only within their own city.' : ''}
            {' '}Any change asks the group to re-approve.
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
    const routeCities = plan.route?.cities || [];
    const stayCity = s => (s.city || '').split(',')[0].trim();
    const cityWindow = s => {
      const seg = routeCities.find(c => c.town === stayCity(s));
      return seg ? `${seg.nights} night${seg.nights === 1 ? '' : 's'}${seg.start ? ` · ${fmtShort(seg.start)} → ${fmtShort(seg.end)}` : ''}` : '';
    };
    return (
      <>
        {Object.keys(plan.flights || {}).length > 0 && (
          <Section eyebrow="Flights — round trip, one per departure city" Icon={PlaneTakeoff}>
            {Object.entries(plan.flights).map(([city, f]) => (
              <div key={city} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)', flexWrap: 'wrap' }}>
                <div>
                  <p style={{ fontWeight: 700, fontSize: 15 }}>From {city} — {f.airline}</p>
                  <p className="muted" style={{ fontSize: 13.5 }}>
                    {f.route} · out {f.depart}
                    {f.return_route ? <><br />{f.return_route} · back {f.return}</> : <> · back {f.return}</>}
                  </p>
                </div>
                <span className="serif" style={{ fontSize: 22, color: 'var(--pine)' }}>${f.est_price_per_person}</span>
              </div>
            ))}
          </Section>
        )}

        {(plan.transport || []).length > 0 && (
          <Section eyebrow="Between cities — the group travels together" Icon={TrainFront}>
            {plan.transport.map(t => {
              const ModeIcon = MODE_ICONS[(t.mode || '').toLowerCase()] || TrainFront;
              return (
                <div key={t.leg} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 16, padding: '13px 0', borderTop: '1px solid var(--line-soft)', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                    <ModeIcon size={17} strokeWidth={1.8} color="var(--pine)" style={{ marginTop: 2, flexShrink: 0 }} />
                    <div>
                      <p style={{ fontWeight: 700, fontSize: 15 }}>{t.leg}</p>
                      <p className="muted" style={{ fontSize: 13.5, textTransform: 'capitalize' }}>
                        {[t.mode, t.carrier].filter(Boolean).join(' · ')}
                        {t.date ? ` · ${fmtShort(t.date)}` : ''}{t.depart ? ` · dep ${t.depart}` : ''}{t.duration ? ` · ${t.duration}` : ''}
                      </p>
                      {t.link && (
                        <a className="btn-quiet" href={t.link} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>
                          <ExternalLink size={12} /> compare on Rome2Rio
                        </a>
                      )}
                    </div>
                  </div>
                  <span className="serif" style={{ fontSize: 22, color: 'var(--pine)' }}>${t.est_price_per_person}</span>
                </div>
              );
            })}
          </Section>
        )}

        {(plan.stays || []).length > 0 && (
          <Section eyebrow="Where you'll rest — city by city" Icon={BedDouble}>
            {plan.stays.map(s => (
              <div key={`${s.city}-${s.name}`} style={{ padding: '16px 0', borderTop: '1px solid var(--line-soft)' }}>
                <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 8 }}>
                  {stayCity(s)}{cityWindow(s) ? ` · ${cityWindow(s)}` : ''}
                </p>
                <Hero image={s.image} height={240} style={{ marginBottom: 12 }} />
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
                  <div>
                    <h2 style={{ fontSize: 21, marginBottom: 3 }}>{s.name}</h2>
                    <p className="muted" style={{ fontSize: 13.5, display: 'flex', alignItems: 'center', gap: 6 }}>
                      {s.type}{s.area ? ` · ${s.area}` : ''}
                      {s.rating ? <><Star size={12} fill="var(--clay)" color="var(--clay)" /> {s.rating}</> : null}
                    </p>
                    {(s.amenities || []).length > 0 && (
                      <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>{s.amenities.join(' · ')}</p>
                    )}
                    {s.official_website && (
                      <a className="btn-quiet" href={s.official_website} target="_blank" rel="noreferrer"
                         style={{ marginTop: 6, fontSize: 13 }}>
                        <Globe size={12} /> Official website
                      </a>
                    )}
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div className="serif" style={{ fontSize: 23, color: 'var(--pine)' }}>${s.price_per_night}<span style={{ fontSize: 13 }}> / night</span></div>
                    <div className="muted" style={{ fontSize: 12.5 }}>≈ ${s.total_per_person} per person for this city</div>
                  </div>
                </div>
              </div>
            ))}
          </Section>
        )}

        {plan.stay?.name && (
          <Section eyebrow="Where you'll rest" Icon={BedDouble}>
            <Hero image={plan.stay.image} height={260} style={{ marginBottom: 16 }} />
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
                  <p style={{ fontWeight: 600, fontSize: 14.5 }}>
                    {v.name}
                    {plan.route && v.city && (
                      <span className="muted" style={{ fontWeight: 600, fontSize: 12, marginLeft: 8 }}>
                        · {(v.city || '').split(',')[0]}
                      </span>
                    )}
                  </p>
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
                  <p style={{ fontWeight: 600, fontSize: 14.5 }}>
                    {e.name}
                    {plan.route && e.city && (
                      <span className="muted" style={{ fontWeight: 600, fontSize: 12, marginLeft: 8 }}>
                        · {(e.city || '').split(',')[0]}
                      </span>
                    )}
                  </p>
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
          {plan.budget_per_person?.max > 0 && (() => {
            // The band everyone agreed to in the questionnaire, flights excluded —
            // held against the shared costs so the group sees the verdict, not math
            const b = plan.budget_per_person;
            const shared = plan.cost_per_person?.shared_total || 0;
            const over = shared - b.max;
            const within = over <= 0;
            return (
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px dashed var(--line-soft)' }}>
                <p style={{
                  fontSize: 14.5, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8,
                  color: within ? 'var(--pine)' : 'var(--clay)',
                }}>
                  <Scale size={15} strokeWidth={2.2} />
                  {within
                    ? `Within the group's budget — $${Math.abs(over).toLocaleString()} under the $${b.max.toLocaleString()} ceiling`
                    : `$${over.toLocaleString()} over the group's $${b.max.toLocaleString()} ceiling`}
                </p>
                <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  The group agreed on ${b.min.toLocaleString()}–${b.max.toLocaleString()} per person, flights excluded.
                  {within ? '' : ' “Request a change” with a cheaper stay or fewer paid activities brings it back inside.'}
                </p>
              </div>
            );
          })()}
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
              selfBooked ? (
                <div>
                  <p style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700, marginBottom: 6 }}>
                    <CheckCircle2 size={17} /> You're booking it yourself — nothing was charged
                  </p>
                  <p className="muted" style={{ fontSize: 14, marginBottom: 12, maxWidth: 480 }}>
                    Your total above is what you'll pay sellers directly. Record each confirmation on the
                    checklist so the group sees what's done.
                  </p>
                  <button className="btn" onClick={() => goTab('bookings')}>
                    <ClipboardCheck size={15} /> Open the booking checklist
                  </button>
                </div>
              ) : (
                /* The group checkout — Rovyn as merchant of record (docs/PAYMENTS-MOR-SPEC.md) */
                <div style={{ padding: '18px 0 4px', borderTop: '1px solid var(--line-soft)' }}>
                  <Checkout tripId={tripId} trip={trip} summary={data.checkout} onChanged={load} goTab={goTab}
                            onSelfBook={() => chooseBooking('self_book')} busy={busy} />
                </div>
              )
            ) : (
              <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
                Booking opens once all {trip.total_members} travelers approve the plan.
              </p>
            )}

            <div style={{ marginTop: 26 }}>
              <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10 }}>
                <Users size={11} style={{ verticalAlign: -1, marginRight: 6 }} />
                Group status — {trip.paid_count + (trip.authorized_count || 0)} of {trip.total_members} locked in
              </p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                {checks.map(c => (
                  <span key={c.user_id} style={{
                    display: 'inline-flex', alignItems: 'center', gap: 7, padding: '7px 14px',
                    border: '1px solid', borderColor: c.paid || ['authorized', 'nothing_due'].includes(c.payment_status) ? 'var(--pine)' : 'var(--line)',
                    borderRadius: 999, fontSize: 13, fontWeight: 600,
                    color: c.paid || ['authorized', 'nothing_due'].includes(c.payment_status) ? 'var(--pine)' : 'var(--muted)',
                  }}>
                    {c.paid || ['authorized', 'nothing_due'].includes(c.payment_status) ? <CheckCircle2 size={13} /> : <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--line)' }} />}
                    {c.name} · ${c.total.toLocaleString()}{c.payment_status === 'authorized' ? ' · authorized' : c.payment_status === 'nothing_due' ? ' · nothing due' : ''}
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
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 4 }}>
        {trip && (
          <TripAvatar tripId={tripId} name={trip.name} photoUrl={trip.photo_url} size={64}
                      onChange={url => setData(d => ({ ...d, trip: { ...d.trip, photo_url: url } }))} />
        )}
        <div style={{ minWidth: 0 }}>
          <p className="eyebrow">{trip?.name}</p>
          <h1 style={{ fontSize: 42, margin: '8px 0 0' }}>
            {status === 'booked'
              ? <>You're going to <em style={{ color: 'var(--pine)' }}>{plan.destination}</em>!</>
              : <>The plan — <em style={{ color: 'var(--pine)' }}>{plan.destination}</em></>}
          </h1>
        </div>
      </div>
      <p className="muted" style={{ marginBottom: 6 }}>
        {plan.country ? `${plan.country} · ` : ''}{plan.duration_days} days · {plan.dates?.start} → {plan.dates?.end}
      </p>
      {plan.summary && (
        <p className="serif" style={{ fontSize: 16.5, fontStyle: 'italic', color: 'var(--ink-soft)', maxWidth: 620, margin: '10px 0 0' }}>
          {plan.summary}
        </p>
      )}

      <Hero image={plan.image} height={210} style={{ marginTop: 18 }} />

      {plan.route && <RouteTimeline route={plan.route} style={{ marginTop: 22 }} />}

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

      {/* Tab bar — active section as a filled pill; swipe the content to move between tabs */}
      <div className="tab-bar" style={{ marginBottom: 30 }}>
        {visibleTabs.map(({ key, label, Icon }) => {
          const active = tab === key;
          return (
            <button
              key={key}
              className={`tab-btn${active ? ' active' : ''}`}
              onClick={() => goTab(key)}
            >
              <Icon size={15} strokeWidth={2} />
              {label}
            </button>
          );
        })}
      </div>

      <div ref={swipeRef}>
      <motion.div
        key={tab}
        initial={{ opacity: 0, x: tabDir ? tabDir * 42 : 0, y: tabDir ? 0 : 10 }}
        animate={{ opacity: 1, x: 0, y: 0 }}
        transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      >
        {tab === 'overview' && <Overview />}
        {tab === 'itinerary' && <Itinerary />}
        {tab === 'travel' && <Travel />}
        {/* Money() is called, not mounted: a closure component would remount the
            Checkout (and reset its form) on every 3 s poll */}
        {tab === 'money' && Money()}
        {tab === 'bookings' && canBook && (
          <BookingsChecklist bookings={bookings} busy={busy} onConfirm={confirmBooking} onUnbook={unbookBooking} />
        )}
      </motion.div>
      </div>

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
            className="modal-card"
            initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }}
            onClick={e => e.stopPropagation()}
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
