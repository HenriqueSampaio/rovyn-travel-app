import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowRight, BedDouble, Bus, Check, Compass, Landmark, Lock, LockOpen,
  MapPin, Minus, PlaneTakeoff, Plus, Ship, Sparkles, Star, Ticket, TrainFront, Users,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText, GuestReviews, JourneyTracker, Hero, MapImage, PhotoCarousel, RouteTimeline, Thumb, ThinkingLoader, useSwipeTabs } from '../ui.jsx';

const TABS = [
  { key: 'overview', label: 'Overview', Icon: Compass },
  { key: 'flights', label: 'Flights', Icon: PlaneTakeoff },
  { key: 'stay', label: 'Stay', Icon: BedDouble },
  { key: 'visits', label: 'Visits', Icon: Landmark },
  { key: 'events', label: 'Events', Icon: Ticket },
];
const TRANSPORT_TAB = { key: 'transport', label: 'Transport', Icon: TrainFront };

const CATEGORY_INTRO = {
  flights: 'These round-trip flights are yours alone — they leave from your city and bring you home again. Star the ones that work for you; the most-starred flight among travelers from your city makes the plan, and your check reflects your own flight.',
  transport: 'How the whole group moves between the route\'s cities — star your favorite way to make each leg. The most-starred option wins that leg for everyone.',
  stay: 'Star every place you\'d be happy sleeping in. The most-starred stay wins, and Rovyn explains the vote.',
  visits: 'Star every sight you don\'t want to miss — Rovyn builds the day-by-day itinerary from the group\'s picks.',
  events: 'Star the experiences you\'d actually show up for. Starred events get an evening (or their scheduled slot) in the plan.',
};

const MODE_ICONS = { train: TrainFront, flight: PlaneTakeoff, bus: Bus, ferry: Ship };

/* "2026-08-14 09:40" → { date: "Thu, Aug 14", time: "09:40" } */
function splitWhen(s = '') {
  const [d, t] = s.split(' ');
  const parsed = d ? new Date(`${d}T${t || '00:00'}`) : null;
  const date = parsed && !isNaN(parsed)
    ? parsed.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    : (d || '');
  return { date, time: t || '' };
}

/* One direction of a round trip, unpacked from the old single crammed line:
   OUT  09:40  Thu, Aug 14   JFK → SPU (via VIE) */
function FlightLegRow({ label, when, route }) {
  const { date, time } = splitWhen(when);
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, padding: '6px 0', flexWrap: 'wrap' }}>
      <span style={{
        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.12em', width: 38, flexShrink: 0,
        color: label === 'OUT' ? 'var(--pine)' : 'var(--clay)',
      }}>
        {label}
      </span>
      <span style={{ fontSize: 15, fontWeight: 700, whiteSpace: 'nowrap' }}>{time}</span>
      <span className="muted" style={{ fontSize: 13, whiteSpace: 'nowrap' }}>{date}</span>
      <span style={{ fontSize: 13.5, color: 'var(--ink-soft)', marginLeft: 'auto' }}>{route}</span>
    </div>
  );
}

function FlightCard({ opt, starred, count, onStar, disabled, buddies, onOpen }) {
  return (
    <div onClick={onOpen}
         style={{ display: 'flex', gap: 16, ...starredWash(starred), borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start', cursor: 'pointer' }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ fontWeight: 700, fontSize: 16 }}>
          {opt.airline}
          {opt.live_price && (
            <span title="Real bookable flights with a live fare for your dates" style={{
              marginLeft: 10, fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
              color: 'var(--pine)', border: '1px solid var(--pine)', borderRadius: 999,
              padding: '2px 8px', verticalAlign: 2, whiteSpace: 'nowrap', display: 'inline-block',
            }}>
              LIVE PRICE
            </span>
          )}
        </p>
        <div style={{ margin: '6px 0 2px' }}>
          <FlightLegRow label="OUT" when={opt.depart} route={opt.route} />
          <FlightLegRow label="BACK" when={opt.return} route={opt.return_route || opt.route} />
        </div>
        <p className="muted" style={{ fontSize: 13 }}>
          {opt.stops === 0 ? 'nonstop' : `${opt.stops} stop${opt.stops > 1 ? 's' : ''}`} · {opt.duration}
        </p>
        <p style={{ fontSize: 14, color: 'var(--ink-soft)', marginTop: 4 }}>{opt.why}</p>
        <p style={{ marginTop: 8, display: 'flex', alignItems: 'baseline', gap: 14, flexWrap: 'wrap' }}>
          <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${opt.est_price_per_person}</span>
          <span className="muted" style={{ fontSize: 13 }}>{opt.live_price ? 'round trip · live fare' : 'est. round trip'}</span>
          {opt.link && <a className="btn-quiet" href={opt.link} target="_blank" rel="noreferrer" style={{ fontSize: 13 }} onClick={e => e.stopPropagation()}>compare on Expedia</a>}
        </p>
        {buddies.length > 0 && (
          <p style={{ marginTop: 8, display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 13.5, fontWeight: 600, color: 'var(--clay)' }}>
            <Star size={13} fill="var(--clay)" />
            {buddies[0]}{buddies.length > 1 ? ` +${buddies.length - 1} more` : ''} selected this flight!
          </p>
        )}
      </div>
      <StarButton starred={starred} count={count} onClick={onStar} disabled={disabled} />
    </div>
  );
}

/* Soft wash over a whole row/card once you've starred it — the star alone is
   easy to miss when scanning a long list. */
function starredWash(on, verticalPad = 22) {
  return on
    ? { background: 'rgba(190, 107, 63, 0.08)', margin: '0 -16px', borderRadius: 10,
        padding: `${verticalPad}px 16px`, transition: 'background 0.25s ease' }
    : { padding: `${verticalPad}px 0`, transition: 'background 0.25s ease' };
}

/* City switcher inside a category — one button per route city, like the tabs */
function CityPills({ cities, value, onChange }) {
  const pill = (active) => ({
    padding: '7px 16px', borderRadius: 999, fontSize: 13.5, fontWeight: 700,
    border: '1px solid', borderColor: active ? 'var(--pine)' : 'var(--line)',
    background: active ? 'var(--pine)' : 'transparent',
    color: active ? '#F6F2E8' : 'var(--ink-soft)',
    transition: 'all 0.18s ease', whiteSpace: 'nowrap',
  });
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '6px 0 18px' }}>
      <button type="button" style={pill(value === 'all')} onClick={() => onChange('all')}>
        All cities
      </button>
      {cities.map(c => (
        <button key={c.town} type="button" style={pill(value.toLowerCase() === c.town.toLowerCase())}
                onClick={() => onChange(c.town)}>
          {c.town}
          <span style={{ fontWeight: 400, opacity: 0.75 }}> · {c.nights}n</span>
        </button>
      ))}
    </div>
  );
}

/* Nights-per-city steppers under the route. Rovyn suggests the split; any
   traveler can rebalance it until the leader locks it (the leader always can).
   The draft lives HERE, not in board state — polling overwrites the board
   every few seconds and would clobber a half-finished edit. */
function RouteSplitEditor({ route, isLeader, onApply, onLock }) {
  const cities = route.cities || [];
  const total = route.duration_days || cities.reduce((s, c) => s + c.nights, 0);
  const serverSplit = cities.map(c => c.nights).join('|');
  const [draft, setDraft] = useState(cities.map(c => c.nights));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  // The server split changed — someone applied theirs; drop the local draft
  useEffect(() => {
    setDraft(cities.map(c => c.nights));
    setErr('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSplit]);

  const locked = Boolean(route.locked);
  const canEdit = isLeader || !locked;
  const allocated = draft.reduce((s, n) => s + n, 0);
  const dirty = draft.join('|') !== serverSplit;
  const bump = (i, delta) => setDraft(d => d.map((n, j) => (j === i ? Math.max(1, n + delta) : n)));

  async function run(fn) {
    setBusy(true);
    setErr('');
    try {
      await fn();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const stepBtn = disabled => ({
    width: 26, height: 26, borderRadius: 999, border: '1px solid var(--line)',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    color: 'var(--ink-soft)', background: 'transparent',
    opacity: disabled ? 0.35 : 1, flexShrink: 0,
  });

  return (
    <div style={{ marginTop: 16, maxWidth: 560 }}>
      <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
        Rovyn's suggested split{route.split_adjusted_by ? ` — adjusted by ${route.split_adjusted_by}` : ''}.
      </p>
      {canEdit ? (
        <>
          {cities.map((c, i) => (
            <div key={c.town} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '8px 0', borderTop: '1px dashed var(--line-soft)' }}>
              <span style={{ flex: 1, fontWeight: 700, fontSize: 14.5 }}>{c.town}</span>
              <button type="button" title="One night fewer" style={stepBtn(busy || draft[i] <= 1)}
                      disabled={busy || draft[i] <= 1} onClick={() => bump(i, -1)}>
                <Minus size={13} strokeWidth={2.4} />
              </button>
              <span style={{ minWidth: 62, textAlign: 'center', fontSize: 14 }}>
                {draft[i]} night{draft[i] === 1 ? '' : 's'}
              </span>
              <button type="button" title="One night more" style={stepBtn(busy)}
                      disabled={busy} onClick={() => bump(i, 1)}>
                <Plus size={13} strokeWidth={2.4} />
              </button>
            </div>
          ))}
          <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginTop: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13.5, fontWeight: 600, color: allocated === total ? 'var(--pine)' : 'var(--clay)' }}>
              {allocated} of {total} nights allocated
            </span>
            <button className="btn-quiet" disabled={busy || !dirty || allocated !== total}
                    onClick={() => run(() => onApply(Object.fromEntries(cities.map((c, i) => [c.town, draft[i]]))))}>
              Apply the new split
            </button>
          </div>
        </>
      ) : (
        <p className="muted" style={{ fontSize: 13.5, marginTop: 8, display: 'inline-flex', alignItems: 'center', gap: 7 }}>
          <Lock size={13} strokeWidth={2.2} /> The trip leader locked the day split.
        </p>
      )}
      {isLeader && (
        <p style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <button className="btn-quiet" style={{ fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 7 }}
                  disabled={busy} onClick={() => run(() => onLock(!locked))}>
            {locked
              ? <><LockOpen size={13} strokeWidth={2.2} /> Unlock the day split</>
              : <><Lock size={13} strokeWidth={2.2} /> Lock the day split</>}
          </button>
          {locked && <span className="muted" style={{ fontSize: 13 }}>Locked — only you can adjust it.</span>}
        </p>
      )}
      {err && <p style={{ color: 'var(--clay)', fontSize: 13.5, marginTop: 8 }}>{err}</p>}
    </div>
  );
}

const PACE_LABEL = { very: 'very busy', moderately: 'moderately busy', slightly: 'slightly busy' };

function StarButton({ starred, count, onClick, disabled }) {
  return (
    <button
      onClick={e => { e.stopPropagation(); onClick(); }}
      disabled={disabled}
      title={disabled ? 'Pick from your own departure city' : starred ? 'Unstar' : 'Star this'}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 7, flexShrink: 0,
        color: starred ? 'var(--clay)' : 'var(--muted)',
        fontWeight: 700, fontSize: 15, opacity: disabled ? 0.35 : 1,
        transition: 'transform 0.12s ease',
      }}
    >
      <Star size={20} strokeWidth={1.8} fill={starred ? 'var(--clay)' : 'none'} />
      {count > 0 && <span>{count}</span>}
    </button>
  );
}

/* A stay option: photo gallery (real hotels get their full LiteAPI gallery),
   the essentials, and reviews tucked behind a tap. The whole card opens the
   stay's full page. */
function StayCard({ opt, media, starred, count, onStar, onOpen }) {
  const photos = media?.photos || [];
  return (
    <div onClick={onOpen} style={{ ...starredWash(starred), borderTop: '1px solid var(--line-soft)', cursor: 'pointer' }}>
      {photos.length > 0
        ? <PhotoCarousel photos={photos} height={330} style={{ marginBottom: 16 }} />
        : <Hero image={opt.image} height={300} style={{ marginBottom: 16 }} />}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <div style={{ flex: 1 }}>
          <p style={{ fontWeight: 700, fontSize: 17 }}>
            {opt.name}
            {opt.live_price && (
              <span title="Real property with live availability for your dates" style={{
                marginLeft: 10, fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
                color: 'var(--pine)', border: '1px solid var(--pine)', borderRadius: 999,
                padding: '2px 8px', verticalAlign: 2, whiteSpace: 'nowrap', display: 'inline-block',
              }}>
                LIVE PRICE
              </span>
            )}
          </p>
          <p className="muted" style={{ fontSize: 14, margin: '2px 0 6px' }}>
            {opt.type}{opt.area ? ` · ${opt.area}` : ''} {opt.rating ? <>· <Star size={12} fill="var(--clay)" color="var(--clay)" style={{ verticalAlign: -1 }} /> {opt.rating}</> : null}
          </p>
          <p style={{ fontSize: 14.5, color: 'var(--ink-soft)' }}>{opt.why}</p>
          {(opt.amenities || []).length > 0 && (
            <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>{opt.amenities.slice(0, 4).join(' · ')}</p>
          )}
          <p style={{ marginTop: 8, display: 'flex', alignItems: 'baseline', gap: 14, flexWrap: 'wrap' }}>
            <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${opt.price_per_night}<span style={{ fontSize: 14 }}> / night</span></span>
            <span className="muted" style={{ fontSize: 13 }}>≈ ${opt.total_per_person} per person total</span>
            {opt.links?.booking && <a className="btn-quiet" href={opt.links.booking} target="_blank" rel="noreferrer" style={{ fontSize: 13 }} onClick={e => e.stopPropagation()}>{opt.live_price ? 'compare' : 'see similar'}</a>}
          </p>
        </div>
        <StarButton starred={starred} count={count} onClick={onStar} />
      </div>
      <GuestReviews media={media} fallbackRating={opt.rating} />
    </div>
  );
}

function CategoryFooter({ status, onDone, onNoPref, onReset }) {
  if (status) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginTop: 34 }}>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700, fontSize: 15 }}>
          <Check size={16} strokeWidth={2.6} />
          {status === 'done' ? 'Your picks are locked in' : 'You said you don\'t mind'}
        </span>
        <button className="btn-quiet" onClick={onReset}>Change my mind</button>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 22, marginTop: 34, flexWrap: 'wrap' }}>
      <button className="btn" onClick={onDone}>
        Lock in my picks <Check size={15} strokeWidth={2.5} />
      </button>
      <button className="btn-quiet" onClick={onNoPref}>
        I don't mind — happy with anything
      </button>
    </div>
  );
}

export default function Plan() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const me = auth.user();
  const [trip, setTrip] = useState(null);
  const [board, setBoard] = useState(null);
  const [myStars, setMyStars] = useState(new Set());
  const [counts, setCounts] = useState({});
  // Opening an option pushes a whole new screen — remember where the
  // traveler was so coming back lands on the same tab and city.
  const [tab, setTab] = useState(() => sessionStorage.getItem(`rovyn-plan-tab-${tripId}`) || 'overview');
  const [tabDir, setTabDir] = useState(0); // which way the content slides in
  const [error, setError] = useState('');
  const [finalizing, setFinalizing] = useState(false);
  const [stayMedia, setStayMedia] = useState(null); // null = not loaded yet
  const [cityFilter, setCityFilterRaw] = useState(() => sessionStorage.getItem(`rovyn-plan-city-${tripId}`) || 'all');
  const pollRef = useRef(null);
  const starsInitialized = useRef(false);
  const stayMediaRequested = useRef(false);
  const cityRestored = useRef(false);
  const tabBtnRefs = useRef({});

  function setCityFilter(v) {
    sessionStorage.setItem(`rovyn-plan-city-${tripId}`, v);
    setCityFilterRaw(v);
  }

  // Tab order is stable enough to wire gestures before the board loads
  const tabs = board?.options?.transport && board?.options?.route
    ? [TABS[0], TABS[1], TRANSPORT_TAB, ...TABS.slice(2)]
    : TABS;
  const tabIdx = Math.max(0, tabs.findIndex(t => t.key === tab));

  function goTab(key) {
    const to = tabs.findIndex(t => t.key === key);
    setTabDir(to > tabIdx ? 1 : to < tabIdx ? -1 : 0);
    sessionStorage.setItem(`rovyn-plan-tab-${tripId}`, key);
    setTab(key);
  }

  // Swiping the content area moves between the board's sections, exactly
  // like the Trips/Feed/Wishlist deck one level up
  const swipeRef = useSwipeTabs(tabs.length, tabIdx, i => goTab(tabs[i].key));

  // A remembered tab may not exist on this board (e.g. Transport on a
  // single-city trip) — fall back to the overview
  const hasBoard = Boolean(board?.options);
  useEffect(() => {
    if (hasBoard && !tabs.some(t => t.key === tab)) setTab('overview');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasBoard]);

  // On phones the tab strip scrolls sideways — keep the active pill in view
  useEffect(() => {
    tabBtnRefs.current[tab]?.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
  }, [tab]);

  // Entering a tab resets the city pills: Stay opens on the first city (you
  // pick one stay per city), Visits/Events open on the full list. The very
  // first run is skipped so the filter restored from the last visit survives.
  const hasRoute = Boolean(board?.options?.route);
  useEffect(() => {
    if (!hasRoute) return;
    if (!cityRestored.current) { cityRestored.current = true; return; }
    setCityFilter(tab === 'stay' ? board.options.route.cities[0].town : 'all');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, hasRoute]);

  useEffect(() => {
    async function poll() {
      try {
        const [status, opts] = await Promise.all([api.tripStatus(tripId), api.getOptions(tripId)]);
        if (status.has_final_plan || status.final_plan_generating) {
          clearInterval(pollRef.current);
          navigate(`/trips/${tripId}/final`);
          return;
        }
        if (!opts.options && !opts.generating) {
          navigate(`/trips/${tripId}`);
          return;
        }
        setTrip(status);
        setBoard(opts);
        setCounts(opts.star_counts || {});
        if (!starsInitialized.current) {
          setMyStars(new Set(opts.my_stars || []));
          starsInitialized.current = true;
        }
      } catch (err) {
        if (err.status === 401) navigate('/');
        else if (err.status === 403 || err.status === 404) navigate('/trips');
      }
    }
    poll();
    pollRef.current = setInterval(poll, 3500);
    return () => clearInterval(pollRef.current);
  }, [tripId, navigate]);

  // Photo galleries + guest reviews are fetched once, when the Stay tab is
  // first opened — the backend caches them on the trip after that.
  useEffect(() => {
    if (tab !== 'stay' || stayMediaRequested.current) return;
    stayMediaRequested.current = true;
    api.getStayMedia(tripId)
      .then(d => setStayMedia(d.media || {}))
      .catch(() => setStayMedia({}));
  }, [tab, tripId]);

  if (!trip || !board?.options) return <Page><Loading text="Laying out the planning board…" /></Page>;

  const options = board.options;
  const route = options.route;
  const myCats = trip.me.categories || {};
  const catStatus = c => myCats[c];
  const isSample = options.source === 'sample';

  async function star(category, optionId) {
    const starred = !myStars.has(optionId);
    // optimistic
    setMyStars(prev => {
      const next = new Set(prev);
      starred ? next.add(optionId) : next.delete(optionId);
      return next;
    });
    setCounts(prev => ({ ...prev, [optionId]: Math.max(0, (prev[optionId] || 0) + (starred ? 1 : -1)) }));
    setError('');
    try {
      await api.toggleStar(tripId, category, optionId, starred);
    } catch (err) {
      setError(err.message);
      setMyStars(prev => {
        const next = new Set(prev);
        starred ? next.delete(optionId) : next.add(optionId);
        return next;
      });
    }
  }

  function setStatus(category, status) {
    // Optimistic: the checkmark lands the moment you tap — the server call
    // settles in the background and only a rejection rolls it back.
    const prevMe = trip.me;
    const prevStars = myStars;
    setError('');
    setTrip(t => {
      const categories = { ...(t.me.categories || {}) };
      if (status === 'reset') delete categories[category];
      else categories[category] = status;
      return { ...t, me: { ...t.me, categories } };
    });
    if (status === 'no_preference') {
      // the server clears this category's stars — mirror it locally
      const ids = ['flights', 'transport'].includes(category)
        ? Object.values(options[category] || {}).flat().map(o => o.id)
        : (options[category] || []).map(o => o.id);
      setMyStars(prev => new Set([...prev].filter(id => !ids.includes(id))));
    }
    api.setCuration(tripId, category, status)
      .then(res => setTrip(t => ({ ...t, me: res.me, all_curated: res.all_curated })))
      .catch(err => {
        setError(err.message);
        setTrip(t => ({ ...t, me: prevMe }));
        if (status === 'no_preference') setMyStars(prevStars);
      });
  }

  // Tapping anywhere on a card opens the option's own screen — the seed
  // state renders it instantly, then live details stream in there.
  function openOption(category, opt) {
    navigate(`/trips/${tripId}/plan/option/${opt.id}`, {
      state: { category, opt, starred: myStars.has(opt.id), count: counts[opt.id] || 0 },
    });
  }

  async function finalize() {
    setFinalizing(true);
    setError('');
    try {
      await api.finalizePlan(tripId);
      navigate(`/trips/${tripId}/final`);
    } catch (err) {
      setError(err.message);
      setFinalizing(false);
    }
  }

  const doneCount = tabs.slice(1).filter(t => catStatus(t.key)).length;

  function renderStars(category, list, { disabled } = {}) {
    return list.map(opt => {
      const buddies = (board.star_names?.[opt.id] || []).filter(n => n !== me?.name);
      if (category === 'flights') {
        return (
          <FlightCard
            key={opt.id}
            opt={opt}
            starred={myStars.has(opt.id)}
            count={counts[opt.id] || 0}
            onStar={() => star(category, opt.id)}
            disabled={disabled}
            buddies={buddies}
            onOpen={() => openOption('flights', opt)}
          />
        );
      }
      return (
      <div key={opt.id} onClick={() => openOption(category, opt)}
           style={{ display: 'flex', gap: 16, ...starredWash(myStars.has(opt.id)), borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start', cursor: 'pointer' }}>
        <Thumb image={opt.image} size={76} />
        <div style={{ flex: 1 }}>
          <p style={{ fontWeight: 700, fontSize: 16 }}>
            {opt.name}
            {route && opt.city && (
              <span style={{
                marginLeft: 10, fontSize: 11, fontWeight: 700, letterSpacing: '0.08em',
                color: 'var(--pine)', border: '1px solid var(--line)', borderRadius: 999,
                padding: '2px 9px', verticalAlign: 2, whiteSpace: 'nowrap', display: 'inline-block',
              }}>
                {opt.city.split(',')[0].trim().toUpperCase()}
              </span>
            )}
          </p>
          <p style={{ fontSize: 14.5, color: 'var(--ink-soft)', margin: '3px 0' }}>{opt.description}</p>
          <p className="muted" style={{ fontSize: 13 }}>
            {opt.duration ? `${opt.duration} · ` : ''}{opt.date_hint ? `${opt.date_hint} · ` : ''}
            {opt.cost_per_person > 0 ? `$${opt.cost_per_person} per person` : 'free'}
          </p>
        </div>
        <StarButton
          starred={myStars.has(opt.id)}
          count={counts[opt.id] || 0}
          onClick={() => star(category, opt.id)}
          disabled={disabled}
        />
      </div>
      );
    });
  }

  function renderFlights() {
    const cities = Object.keys(options.flights || {});
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const myCity = norm(board.my_departure_city);
    const mine = cities.filter(c => norm(c) === myCity);
    const shown = mine.length ? mine : cities; // fallback: no questionnaire city matched
    const sameCityMates = trip.members.length > 1;
    const openJaw = route && route.cities.length > 1
      && route.cities[0].town !== route.cities[route.cities.length - 1].town;
    return (
      <>
        {openJaw && (
          <p style={{
            display: 'inline-flex', alignItems: 'center', gap: 8, marginBottom: 10,
            fontSize: 13.5, fontWeight: 700, color: 'var(--pine)',
          }}>
            <PlaneTakeoff size={14} strokeWidth={2.4} />
            Open-jaw round trip: land in {route.cities[0].town}, fly home from {route.cities[route.cities.length - 1].town}.
          </p>
        )}
        {shown.map(city => (
          <div key={city} style={{ marginBottom: 36 }}>
            <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <PlaneTakeoff size={13} strokeWidth={2.5} /> Your flights from {city}
            </p>
            {sameCityMates && (
              <p className="muted" style={{ fontSize: 13, fontStyle: 'italic' }}>
                Travel mates flying from {city} see these too — starring the same flight means flying together.
              </p>
            )}
            {renderStars('flights', options.flights[city])}
          </div>
        ))}
        {mine.length > 0 && cities.length > mine.length && (
          <p className="muted" style={{ fontSize: 13.5, fontStyle: 'italic' }}>
            Travelers from {cities.filter(c => !mine.includes(c)).join(', ')} are picking their own flights on their side.
          </p>
        )}
      </>
    );
  }

  function renderTransport() {
    const legs = route?.legs || [];
    return (
      <>
        {legs.map(leg => (
          <div key={leg.key} style={{ marginBottom: 36 }}>
            <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <TrainFront size={13} strokeWidth={2.5} /> {leg.key}
              {leg.date && <span className="muted" style={{ letterSpacing: 0, textTransform: 'none', fontWeight: 600 }}>· {leg.date}</span>}
            </p>
            {leg.real_durations && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
                Google puts this leg at
                {leg.real_durations.transit ? ` ${leg.real_durations.transit} by transit` : ''}
                {leg.real_durations.transit && leg.real_durations.drive ? ' ·' : ''}
                {leg.real_durations.drive ? ` ${leg.real_durations.drive} by car` : ''}
                {leg.real_durations.km ? ` · ${Math.round(leg.real_durations.km)} km` : ''}
              </p>
            )}
            {(options.transport[leg.key] || []).map(opt => {
              const ModeIcon = MODE_ICONS[(opt.mode || '').toLowerCase()] || TrainFront;
              const buddies = (board.star_names?.[opt.id] || []).filter(n => n !== me?.name);
              return (
                <div key={opt.id} onClick={() => openOption('transport', opt)}
                     style={{ display: 'flex', gap: 16, ...starredWash(myStars.has(opt.id), 20), borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start', cursor: 'pointer' }}>
                  <ModeIcon size={20} strokeWidth={1.7} color="var(--pine)" style={{ marginTop: 2, flexShrink: 0 }} />
                  <div style={{ flex: 1 }}>
                    <p style={{ fontWeight: 700, fontSize: 16 }}>
                      {opt.carrier || opt.mode}
                      <span className="muted" style={{ fontWeight: 400, marginLeft: 10, fontSize: 14, textTransform: 'capitalize' }}>{opt.mode}</span>
                    </p>
                    <p className="muted" style={{ fontSize: 14, margin: '4px 0' }}>
                      {opt.depart ? `Departs ${opt.depart} · ` : ''}{opt.duration}
                    </p>
                    <p style={{ fontSize: 14, color: 'var(--ink-soft)' }}>{opt.why}</p>
                    <p style={{ marginTop: 6, display: 'flex', alignItems: 'baseline', gap: 14, flexWrap: 'wrap' }}>
                      <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${opt.est_price_per_person}</span>
                      <span className="muted" style={{ fontSize: 13 }}>per person, one way</span>
                      {opt.link && <a className="btn-quiet" href={opt.link} target="_blank" rel="noreferrer" style={{ fontSize: 13 }} onClick={e => e.stopPropagation()}>compare on Rome2Rio</a>}
                    </p>
                    {buddies.length > 0 && (
                      <p style={{ marginTop: 8, display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 13.5, fontWeight: 600, color: 'var(--clay)' }}>
                        <Star size={13} fill="var(--clay)" />
                        {buddies[0]}{buddies.length > 1 ? ` +${buddies.length - 1} more` : ''} starred this leg!
                      </p>
                    )}
                  </div>
                  <StarButton
                    starred={myStars.has(opt.id)}
                    count={counts[opt.id] || 0}
                    onClick={() => star('transport', opt.id)}
                  />
                </div>
              );
            })}
          </div>
        ))}
      </>
    );
  }

  function renderStays() {
    const stays = options.stay || [];
    const card = opt => (
      <StayCard
        key={opt.id}
        opt={opt}
        media={stayMedia?.[opt.id]}
        starred={myStars.has(opt.id)}
        count={counts[opt.id] || 0}
        onStar={() => star('stay', opt.id)}
        onOpen={() => openOption('stay', opt)}
      />
    );
    if (!route) return stays.map(card);
    const town = s => (s || '').split(',')[0].trim().toLowerCase();
    const shownCities = cityFilter === 'all'
      ? route.cities
      : route.cities.filter(c => c.town.toLowerCase() === cityFilter.toLowerCase());
    const grouped = shownCities.map(c => ({
      city: c,
      stays: stays.filter(o => town(o.city) === c.town.toLowerCase()),
    }));
    const leftovers = cityFilter === 'all'
      ? stays.filter(o => !route.cities.some(c => town(o.city) === c.town.toLowerCase()))
      : [];
    return (
      <>
        {grouped.map(({ city, stays: cityStays }) => cityStays.length > 0 && (
          <div key={city.city} style={{ marginBottom: 30 }}>
            <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
              <BedDouble size={13} strokeWidth={2.5} /> {city.town}
              <span className="muted" style={{ letterSpacing: 0, textTransform: 'none', fontWeight: 600 }}>
                · {city.nights} night{city.nights === 1 ? '' : 's'}{city.start ? ` · ${city.start} → ${city.end}` : ''}
              </span>
            </p>
            {cityStays.map(card)}
          </div>
        ))}
        {leftovers.length > 0 && leftovers.map(card)}
      </>
    );
  }

  // Multi-city: which options a tab shows under the selected city pill
  function byCity(list) {
    if (!route || cityFilter === 'all') return list;
    const target = cityFilter.toLowerCase();
    return list.filter(o => (o.city || '').split(',')[0].trim().toLowerCase() === target);
  }

  function renderOverview() {
    return (
      <>
        {!route && (
          <MapImage
            load={() => api.routeMap(tripId)}
            alt={`Map of ${options.destination}`}
            style={{ marginBottom: 52 }}
            embed={{ mode: 'place', params: { q: options.destination } }}
            header={
              <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
                <MapPin size={13} strokeWidth={2.5} /> The destination
              </p>
            }
          />
        )}
        {route && (
          <section style={{ marginBottom: 52 }}>
            <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
              <TrainFront size={13} strokeWidth={2.5} /> The route
            </p>
            <MapImage
              load={() => api.routeMap(tripId)}
              alt="Route map"
              style={{ marginBottom: 16 }}
              embed={{ mode: 'directions', params: {
                origin: route.cities[0].city,
                destination: route.cities[route.cities.length - 1].city,
                ...(route.cities.length > 2
                  ? { waypoints: route.cities.slice(1, -1).map(c => c.city).join('|') } : {}),
                mode: 'transit',
              } }}
            />
            <RouteTimeline route={route} style={{ maxWidth: 560 }} />
            <RouteSplitEditor
              route={route}
              isLeader={trip.is_leader}
              onApply={async nights => {
                const res = await api.setRouteNights(tripId, nights);
                setBoard(b => ({ ...b, options: res.options }));
              }}
              onLock={async locked => {
                const res = await api.setRouteLock(tripId, locked);
                setBoard(b => ({ ...b, options: res.options }));
              }}
            />
            {route.reason && (
              <p className="muted" style={{ fontSize: 13.5, marginTop: 12, maxWidth: 520 }}>{route.reason}</p>
            )}
            <p className="muted" style={{ fontSize: 13.5, marginTop: 6, maxWidth: 520 }}>
              Drawn from the group's multi-city wishes. Star how you'd make each leg in the
              Transport tab, and pick a stay for every city in the Stay tab.
            </p>
          </section>
        )}

        <section style={{ marginBottom: 52 }}>
          <p className="eyebrow" style={{ marginBottom: 14 }}>How this works</p>
          {[
            ['Browse each tab', 'Flights (yours are personal, from your city), stay, visits and events — all generated around the group\'s answers.'],
            ['Star what you love', 'As many options as you like — the Visits and Events tabs suggest how many to pick for your pace. Stars are votes; change them anytime before the final plan.'],
            ['Finish every section', 'Lock in your picks — or tap “I don\'t mind” when you have no strong opinion.'],
            trip.solo
              ? ['When you\'re done', 'Rovyn builds the day-by-day itinerary from your starred picks. Every slot stays editable until you approve.']
              : [`When all ${trip.total_members} are done`, 'Rovyn builds the day-by-day itinerary from the group\'s starred picks. Majority wins, every decision comes with its vote count, and every slot stays editable until the group approves.'],
          ].map(([title, desc], i) => (
            <div key={title} style={{ display: 'flex', gap: 18, padding: '14px 0', borderTop: '1px solid var(--line-soft)' }}>
              <span className="serif" style={{ fontSize: 17, fontStyle: 'italic', color: 'var(--clay)', minWidth: 24 }}>{i + 1}.</span>
              <div>
                <p style={{ fontWeight: 700, fontSize: 15.5 }}>{title}</p>
                <p className="muted" style={{ fontSize: 14 }}>{desc}</p>
              </div>
            </div>
          ))}
        </section>

        <section style={{ marginBottom: 52 }}>
          <p className="eyebrow" style={{ marginBottom: 14 }}>Your checklist — {doneCount} of {trip.categories_total} sections</p>
          {tabs.slice(1).map(({ key, label, Icon }) => {
            const st = catStatus(key);
            return (
              <button key={key} className="opt-row" onClick={() => goTab(key)}>
                <Icon size={18} strokeWidth={1.7} color={st ? 'var(--pine)' : 'var(--muted)'} />
                <span className="opt-title" style={{ flex: 1 }}>{label}</span>
                {st ? (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: 'var(--pine)', fontSize: 14, fontWeight: 600 }}>
                    <Check size={15} strokeWidth={2.5} /> {st === 'done' ? 'locked in' : "don't mind"}
                  </span>
                ) : (
                  <span className="muted" style={{ fontSize: 14, fontStyle: 'italic', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    to do <ArrowRight size={14} />
                  </span>
                )}
              </button>
            );
          })}
        </section>

        {!trip.solo && (
        <section style={{ marginBottom: 52 }}>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
            <Users size={13} strokeWidth={2.5} /> Group progress
          </p>
          {trip.members.map(m => (
            <div key={m.user_id} className="opt-row" style={{ cursor: 'default' }}>
              <span className="opt-title" style={{ flex: 1 }}>{m.name}{m.user_id === me?.id ? ' (you)' : ''}</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ width: 120, height: 2, background: 'var(--line-soft)' }}>
                  <div style={{ width: `${(m.curated_count / trip.categories_total) * 100}%`, height: '100%', background: 'var(--pine)' }} />
                </div>
                <span className="muted" style={{ fontSize: 13, minWidth: 44 }}>{m.curated_count} of {trip.categories_total}</span>
              </div>
            </div>
          ))}
        </section>
        )}

        <hr className="rule" style={{ marginBottom: 36 }} />
        <section>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
            <Sparkles size={13} strokeWidth={2.5} /> The final plan
          </p>
          {finalizing ? (
            <ThinkingLoader messages={[
              'Rovyn is planning your trip…',
              'Counting everyone’s stars…',
              'Weaving your picks into the days…',
              'Rovyn is thinking…',
              'Balancing budgets and pace…',
            ]} />
          ) : trip.all_curated ? (
            trip.is_leader ? (
              <>
                <p className="muted" style={{ marginBottom: 18, maxWidth: 470 }}>
                  {trip.solo
                    ? 'You\'ve finished starring. Rovyn will weave your picks into the final plan — every call explained.'
                    : 'Everyone has finished starring. Rovyn will merge the group\'s votes into one plan — majority wins, every call explained.'}
                </p>
                <button className="btn" onClick={finalize}>Draft the final plan <ArrowRight size={16} /></button>
              </>
            ) : (
              <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
                Everyone's done — waiting for the trip leader to draft the final plan.
              </p>
            )
          ) : (
            <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
              {trip.solo
                ? `Unlocks when you finish all ${trip.categories_total} sections.`
                : `Unlocks when all ${trip.total_members} travelers finish all ${trip.categories_total} sections.`}
            </p>
          )}
        </section>
      </>
    );
  }

  return (
    <Page width={760}>
      <p className="eyebrow">{trip.name}</p>
      <h1 style={{ fontSize: 42, margin: '10px 0 6px' }}>
        Curate <em style={{ color: 'var(--pine)' }}>{options.destination}</em>
      </h1>
      <p className="muted" style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
        <MapPin size={13} /> {options.country || options.destination} · {options.dates?.start} → {options.dates?.end}
      </p>
      {isSample && (
        <p style={{ color: 'var(--clay)', fontSize: 13.5, marginTop: 8 }}>
          Sample options — add ANTHROPIC_API_KEY to Rovyn/.env for real, group-tailored suggestions.
        </p>
      )}

      <Hero image={options.image} height={200} style={{ marginTop: 18 }} />

      {route && <RouteTimeline route={route} style={{ marginTop: 22 }} />}

      <JourneyTracker status={trip.status} style={{ marginTop: 26 }} />

      {/* Tab bar — the active section is a filled pill; swipe the content to move between them */}
      <div className="tab-bar">
        {tabs.map(({ key, label, Icon }) => {
          const active = tab === key;
          const done = key !== 'overview' && catStatus(key);
          return (
            <button
              key={key}
              ref={el => { tabBtnRefs.current[key] = el; }}
              className={`tab-btn${active ? ' active' : ''}`}
              onClick={() => goTab(key)}
            >
              <Icon size={15} strokeWidth={2} />
              {label}
              {done && <Check size={13} strokeWidth={3} color={active ? '#F6F2E8' : 'var(--pine)'} />}
            </button>
          );
        })}
      </div>

      <ErrorText>{error}</ErrorText>

      <div ref={swipeRef}>
      <motion.div
        key={tab}
        initial={{ opacity: 0, x: tabDir ? tabDir * 42 : 0, y: tabDir ? 0 : 10 }}
        animate={{ opacity: 1, x: 0, y: 0 }}
        transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      >
        {tab === 'overview' && renderOverview()}
        {tab !== 'overview' && (
          <>
            <p className="muted" style={{ maxWidth: 560, marginBottom: 10 }}>{CATEGORY_INTRO[tab]}</p>
            {tab === 'flights' && (
              options.flights_source === 'liteapi' ? (
                <p style={{
                  display: 'inline-flex', alignItems: 'center', gap: 8, marginBottom: 10,
                  fontSize: 13.5, fontWeight: 700, color: 'var(--pine)',
                }}>
                  <Check size={14} strokeWidth={2.6} />
                  {options.flights_all_live === false
                    ? 'Real flights with live fares where marked LIVE PRICE — the rest are Rovyn estimates.'
                    : 'Real flights with live fares for your dates.'}
                </p>
              ) : (
                <p style={{ marginBottom: 10, fontSize: 13.5, color: 'var(--clay)' }}>
                  These flights and prices are AI estimates — the Expedia links show live fares.
                </p>
              )
            )}
            {tab === 'stay' && (
              options.stay_source === 'liteapi' ? (
                <p style={{
                  display: 'inline-flex', alignItems: 'center', gap: 8, marginBottom: 10,
                  fontSize: 13.5, fontWeight: 700, color: 'var(--pine)',
                }}>
                  <Check size={14} strokeWidth={2.6} />
                  {options.stay_all_live === false
                    ? 'Real hotels with live prices where marked LIVE PRICE — the rest are Rovyn estimates.'
                    : 'Real hotels with live prices and availability for your dates.'}
                </p>
              ) : (
                <p style={{ marginBottom: 10, fontSize: 13.5, color: 'var(--clay)' }}>
                  These stays and prices are AI estimates.
                  {' '}Tip: a “City, Country” destination (e.g. “Porto Cervo, Italy”) lets Rovyn fetch real hotels with live prices.
                </p>
              )
            )}
            {route && ['stay', 'visits', 'events'].includes(tab) && (
              <CityPills cities={route.cities} value={cityFilter} onChange={setCityFilter} />
            )}
            {tab === 'stay' && stayMedia === null && (
              <p className="muted" style={{ fontSize: 13, fontStyle: 'italic', marginBottom: 10 }}>
                Fetching photo galleries and guest reviews…
              </p>
            )}
            {(tab === 'visits' || tab === 'events') && board.recommended && (() => {
              const ids = new Set((options[tab] || []).map(o => o.id));
              const mine = [...myStars].filter(id => ids.has(id)).length;
              const target = board.recommended[tab];
              return (
                <p style={{
                  display: 'inline-flex', alignItems: 'center', gap: 8, marginBottom: 10,
                  fontSize: 14, fontWeight: 600, color: mine >= target ? 'var(--pine)' : 'var(--clay)',
                }}>
                  <Star size={14} fill="currentColor" />
                  Aim for about {target} {tab} for your {PACE_LABEL[board.my_pace] || 'moderately busy'} pace — you've starred {mine}.
                </p>
              );
            })()}
            {tab === 'flights' ? renderFlights()
              : tab === 'transport' ? renderTransport()
              : tab === 'stay' ? renderStays()
              : renderStars(tab, byCity(options[tab] || []))}
            <CategoryFooter
              status={catStatus(tab)}
              onDone={() => setStatus(tab, 'done')}
              onNoPref={() => setStatus(tab, 'no_preference')}
              onReset={() => setStatus(tab, 'reset')}
            />
          </>
        )}
      </motion.div>
      </div>
    </Page>
  );
}
