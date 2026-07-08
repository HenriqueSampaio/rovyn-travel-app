import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { motion, AnimatePresence, animate, motionValue, useTransform } from 'framer-motion';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, Camera, Check, ChevronLeft, ChevronRight, Compass, LogOut, Loader2, MapPin, Quote } from 'lucide-react';
import { api, auth } from './api/index.js';

export function Wordmark({ size = 26 }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10 }}>
      <Compass size={size * 0.85} strokeWidth={1.6} color="var(--pine)" />
      <span className="serif" style={{ fontSize: size, fontWeight: 500, letterSpacing: '-0.02em' }}>
        Rovyn
      </span>
    </span>
  );
}

/* Shared position across the three top-level pages, as a fractional index
   (0 = Trips, 1 = Feed, 2 = Wishlist). The SwipeShell writes to it while a
   finger drags; the tab underline below reads from it — so both the page
   track and the indicator move together, mid-gesture included. */
export const swipeProgress = motionValue(0);
let progressSynced = false;

const TABS = [
  { to: '/trips', label: 'Trips' },
  { to: '/feed', label: 'Feed' },
  { to: '/wishlist', label: 'Wishlist' },
];

function TabRail() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const refs = useRef([]);
  const [metrics, setMetrics] = useState(TABS.map(() => ({ left: 0, width: 0 })));

  // Sub-pages count toward their tab (/trips/abc lights up Trips)
  const activeIdx = TABS.findIndex(t => pathname === t.to || pathname.startsWith(`${t.to}/`));

  useLayoutEffect(() => {
    function measure() {
      setMetrics(refs.current.map(el =>
        el ? { left: el.offsetLeft, width: el.offsetWidth } : { left: 0, width: 0 }
      ));
    }
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  // Keep the indicator in sync when arriving by tab click or deep link.
  // (After a swipe gesture the progress already sits on the target, so
  // this is a no-op there.) First sync ever jumps without animating.
  useEffect(() => {
    if (activeIdx === -1) return;
    if (!progressSynced) {
      progressSynced = true;
      swipeProgress.set(activeIdx);
      return;
    }
    if (swipeProgress.get() !== activeIdx) {
      const controls = animate(swipeProgress, activeIdx, { duration: 0.32, ease: [0.22, 1, 0.36, 1] });
      return () => controls.stop();
    }
  }, [activeIdx]);

  const left = useTransform(swipeProgress, [0, 1, 2], metrics.map(m => m.left));
  const width = useTransform(swipeProgress, [0, 1, 2], metrics.map(m => m.width));

  return (
    <span className="nav-tabs">
      {TABS.map((t, i) => (
        <button
          key={t.to}
          ref={el => { refs.current[i] = el; }}
          onClick={() => navigate(t.to)}
          style={{
            fontSize: 13, fontWeight: 700, letterSpacing: '0.14em', textTransform: 'uppercase',
            color: i === activeIdx ? 'var(--clay)' : 'var(--pine)',
            padding: '4px 0 7px', transition: 'color 0.25s ease',
          }}
        >
          {t.label}
        </button>
      ))}
      {activeIdx !== -1 && (
        <motion.span style={{
          position: 'absolute', bottom: 0, height: 2, borderRadius: 2,
          background: 'var(--clay)', left, width,
        }} />
      )}
    </span>
  );
}

export function Nav() {
  const navigate = useNavigate();
  const location = useLocation();
  const user = auth.user();
  // Any page inside a specific trip gets a way back home
  const insideTrip = /^\/trips\/.+/.test(location.pathname);
  return (
    <nav className="site-nav">
      <div className="nav-left">
        <button onClick={() => navigate('/trips')} style={{ padding: 0 }}>
          <Wordmark />
        </button>
        {insideTrip && (
          <button className="btn-quiet" onClick={() => navigate('/trips')}>
            <ArrowLeft size={15} strokeWidth={2} /> My trips
          </button>
        )}
      </div>
      {user && (
        <div className="nav-right">
          <TabRail />
          <span className="muted nav-username" style={{ fontSize: 14 }}>{user.name}</span>
          <button
            className="btn-quiet"
            onClick={() => { auth.clear(); navigate('/'); }}
            title="Sign out"
          >
            <LogOut size={15} strokeWidth={2} /> Sign out
          </button>
        </div>
      )}
    </nav>
  );
}

/* True inside the SwipeShell deck: the shell renders one static Nav above
   the sliding track, so pages in the deck must not render their own. */
export const InSwipeDeck = createContext(false);

/* Horizontal swipe between a page's tabs (planning board, final plan).
   Direction-locked like the top-level SwipeShell: the gesture only counts
   once it's clearly horizontal, vertical scrolling stays native, and
   anything inside [data-noswipe] (photo carousels, sliders) keeps its own
   gestures. Unlike the shell there is no live track — a successful swipe
   advances one tab and the page animates the change itself.

   Returns a callback ref for the swipeable container (both pages render it
   behind a loading screen, so a plain useRef would attach to nothing). */
export function useSwipeTabs(count, activeIndex, onGo) {
  const state = useRef(null);
  state.current = { count, activeIndex, onGo };
  const [el, setEl] = useState(null);
  useEffect(() => {
    if (!el) return;
    let g = null; // active gesture
    function onStart(e) {
      if (e.touches.length !== 1) return;
      if (e.target.closest('[data-noswipe], input, textarea, select, video')) return;
      g = { x: e.touches[0].clientX, y: e.touches[0].clientY, locked: false, dx: 0 };
    }
    function onMove(e) {
      if (!g) return;
      const t = e.touches[0];
      const dx = t.clientX - g.x;
      const dy = t.clientY - g.y;
      if (!g.locked) {
        if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return; // not decided yet
        if (Math.abs(dx) <= Math.abs(dy)) { g = null; return; } // vertical — let it scroll
        g.locked = true;
      }
      e.preventDefault(); // clearly horizontal — keep the page still
      g.dx = dx;
    }
    function onEnd() {
      if (!g) return;
      const { locked, dx } = g;
      g = null;
      if (!locked || Math.abs(dx) < 48) return;
      const { count, activeIndex, onGo } = state.current;
      const next = activeIndex + (dx < 0 ? 1 : -1);
      if (next >= 0 && next < count) onGo(next);
    }
    el.addEventListener('touchstart', onStart, { passive: true });
    el.addEventListener('touchmove', onMove, { passive: false });
    el.addEventListener('touchend', onEnd, { passive: true });
    el.addEventListener('touchcancel', onEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onStart);
      el.removeEventListener('touchmove', onMove);
      el.removeEventListener('touchend', onEnd);
      el.removeEventListener('touchcancel', onEnd);
    };
  }, [el]);
  return setEl;
}

export function Page({ children, width = 680, nav = true }) {
  const inDeck = useContext(InSwipeDeck);
  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: width }}>
        {nav && !inDeck && <Nav />}
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        >
          {children}
        </motion.div>
      </div>
    </div>
  );
}

/* The trip's group picture — tap to change it, WhatsApp style. Any member
   can set one; without a photo it falls back to a tinted serif initial.
   Self-contained: it talks to the API itself and reports the new URL via
   onChange so the page can patch its own trip state. Rendered on the
   dashboard AND every phase page header (vote / plan / final), because the
   dashboard redirects away once planning has started. */
export function TripAvatar({ tripId, name, photoUrl, onChange, size = 76 }) {
  const inputRef = useRef(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

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

  const badge = Math.round(size * 0.36);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, flexShrink: 0 }}>
      <button
        type="button"
        title="Change trip photo"
        aria-label="Change trip photo"
        onClick={() => inputRef.current?.click()}
        disabled={busy}
        style={{ position: 'relative', width: size, height: size, borderRadius: '50%', padding: 0 }}
      >
        {photoUrl ? (
          <img
            src={api.mediaUrl(photoUrl)} alt={`${name} photo`}
            style={{ width: '100%', height: '100%', borderRadius: '50%', objectFit: 'cover', display: 'block' }}
          />
        ) : (
          <span className="serif" style={{
            width: '100%', height: '100%', borderRadius: '50%',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'rgba(46, 93, 79, 0.11)', color: 'var(--pine)',
            fontSize: Math.round(size * 0.42),
          }}>
            {((name || '').trim()[0] || '·').toUpperCase()}
          </span>
        )}
        <span style={{
          position: 'absolute', right: -3, bottom: -3, width: badge, height: badge,
          borderRadius: '50%', background: 'var(--pine)', color: '#F6F2E8',
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          border: '2px solid var(--bg)',
        }}>
          {busy
            ? <Loader2 className="spin" size={Math.round(badge * 0.5)} />
            : <Camera size={Math.round(badge * 0.5)} strokeWidth={2.2} />}
        </span>
      </button>
      {photoUrl && !busy && (
        <button className="muted" style={{ fontSize: 11.5 }}
                onClick={() => run(async () => {
                  await api.removeTripPhoto(tripId);
                  onChange?.(null);
                })}>
          remove
        </button>
      )}
      {err && (
        <span style={{ color: 'var(--clay)', fontSize: 11, maxWidth: size + 44, textAlign: 'center' }}>
          {err}
        </span>
      )}
      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp,image/gif"
        hidden
        onChange={e => {
          const f = e.target.files?.[0];
          e.target.value = ''; // re-picking the same file should fire again
          if (f) run(async () => {
            const { photo_url } = await api.setTripPhoto(tripId, f);
            onChange?.(photo_url);
          });
        }}
      />
    </div>
  );
}

export function Field({ label, children }) {
  return (
    <label style={{ display: 'block', marginBottom: 28 }}>
      <span className="eyebrow" style={{ display: 'block', marginBottom: 4, fontSize: 11 }}>{label}</span>
      {children}
    </label>
  );
}

export function Loading({ text = 'One moment…' }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, padding: '120px 0' }}>
      <Loader2 className="spin" size={30} strokeWidth={1.5} color="var(--pine)" />
      <p className="muted serif" style={{ fontStyle: 'italic', fontSize: 17 }}>{text}</p>
    </div>
  );
}

/* City input with autocomplete — typo-proofs destination/departure fields.
   Suggestions come from /places/suggest (Google Places when configured,
   otherwise the bundled GeoNames world-city list). Free text still allowed.
   onPick fires when a suggestion is chosen; onCommit fires on a plain Enter —
   both let pickers (multi-city wishlist) add-and-clear in one gesture. */
export function CityInput({ value, onChange, placeholder, autoFocus, onPick, onCommit }) {
  const [suggestions, setSuggestions] = useState([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const debounceRef = useRef(null);
  const reqIdRef = useRef(0);
  const pickedRef = useRef('');

  function fetchSuggestions(q) {
    const id = ++reqIdRef.current;
    api.suggestCities(q)
      .then(d => {
        if (id !== reqIdRef.current) return; // stale response
        setSuggestions(d.suggestions || []);
        setOpen((d.suggestions || []).length > 0);
        setActive(-1);
      })
      .catch(() => {});
  }

  function handleChange(text) {
    onChange(text);
    clearTimeout(debounceRef.current);
    if (text.trim().length < 2 || text === pickedRef.current) {
      setOpen(false);
      setSuggestions([]);
      return;
    }
    debounceRef.current = setTimeout(() => fetchSuggestions(text.trim()), 220);
  }

  function pick(s) {
    pickedRef.current = s.label;
    onChange(s.label);
    setOpen(false);
    setSuggestions([]);
    onPick?.(s);
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && (!open || active < 0) && onCommit) {
      e.preventDefault();
      if (value.trim()) onCommit(value.trim());
      setOpen(false);
      return;
    }
    if (!open || suggestions.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive(a => (a + 1) % suggestions.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive(a => (a <= 0 ? suggestions.length - 1 : a - 1));
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault();
      pick(suggestions[active]);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  }

  return (
    <span style={{ position: 'relative', display: 'block', flex: 1 }}>
      <input
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={e => handleChange(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onFocus={() => suggestions.length > 0 && value !== pickedRef.current && setOpen(true)}
        autoComplete="off"
      />
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 40,
          background: 'var(--bg)', border: '1px solid var(--line)', borderTop: 'none',
          boxShadow: '0 16px 40px rgba(35, 49, 43, 0.16)', maxHeight: 280, overflowY: 'auto',
        }}>
          {suggestions.map((s, i) => (
            <button
              key={s.label + i}
              type="button"
              onMouseDown={e => { e.preventDefault(); pick(s); }}
              onMouseEnter={() => setActive(i)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                padding: '11px 14px', textAlign: 'left', fontSize: 15,
                background: i === active ? 'rgba(46, 93, 79, 0.08)' : 'transparent',
                color: 'var(--ink)',
              }}
            >
              <MapPin size={13} strokeWidth={1.8} color="var(--pine)" style={{ flexShrink: 0 }} />
              <span>
                <strong>{s.city}</strong>
                {s.country ? <span className="muted">, {s.country}</span> : null}
              </span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

/* Animated waiting state — cycles through playful status lines while the AI works */
export function ThinkingLoader({ messages, centered = false, style }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 2600);
    return () => clearInterval(t);
  }, []);
  const msg = messages[tick % messages.length];
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 14,
      ...(centered ? { flexDirection: 'column', padding: '110px 0', textAlign: 'center' } : {}),
      ...style,
    }}>
      <motion.span
        animate={{ scale: [1, 1.15, 1] }}
        transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
        style={{ display: 'inline-flex' }}
      >
        <Compass className="spin-slow" size={centered ? 34 : 22} strokeWidth={1.5} color="var(--pine)" />
      </motion.span>
      <AnimatePresence mode="wait">
        <motion.p
          key={msg}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.35 }}
          className="serif muted"
          style={{ fontStyle: 'italic', fontSize: centered ? 18 : 17 }}
        >
          {msg}
        </motion.p>
      </AnimatePresence>
    </div>
  );
}

/* Journey tracker — where the group is in the planning process */
const JOURNEY_STEPS = [
  { key: 'preferences', label: 'Preferences' },
  { key: 'destination', label: 'Destination' },
  { key: 'curate', label: 'Star favorites' },
  { key: 'plan', label: 'Final plan' },
  { key: 'pay', label: 'Approve & book' },
];

export function journeyIndex(status) {
  switch (status) {
    case 'waiting': return 0;
    case 'voting': return 1;
    case 'selected':
    case 'curating': return 2;
    case 'final_review': return 3;
    case 'approved': return 4;
    case 'booked': return 5; // everything done
    default: return 0;
  }
}

export function JourneyTracker({ status, style }) {
  const current = journeyIndex(status);
  return (
    <div className="journey" style={style}>
      {JOURNEY_STEPS.map((step, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={step.key} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <div className="journey-link" style={{
                background: done || active ? 'var(--pine)' : 'var(--line)',
              }} />
            )}
            <span className="journey-step" style={{
              color: done ? 'var(--pine)' : active ? 'var(--clay)' : 'var(--muted)',
            }}>
              {done
                ? <Check size={13} strokeWidth={3} />
                : <span style={{
                    width: 7, height: 7, borderRadius: '50%',
                    background: active ? 'var(--clay)' : 'var(--line)',
                    display: 'inline-block',
                  }} />}
              {step.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function ErrorText({ children }) {
  if (!children) return null;
  return <p style={{ color: 'var(--clay)', fontSize: 14, marginTop: 14 }}>{children}</p>;
}

/* Multi-city route at a glance: one band per city, width proportional to the
   nights spent there, with dates underneath. Reads route = {cities: [...]}
   as produced by the backend's route planner. */
export function RouteTimeline({ route, style }) {
  const cities = route?.cities || [];
  if (cities.length < 2) return null;
  const fmt = iso => {
    if (!iso) return '';
    const d = new Date(`${iso}T00:00:00`);
    return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  };
  const shades = ['var(--pine)', 'var(--clay)'];
  return (
    <div style={{ display: 'flex', gap: 3, alignItems: 'stretch', ...style }}>
      {cities.map((c, i) => (
        <div key={`${c.city}-${i}`} style={{ flex: `${Math.max(c.nights, 1)} 1 0`, minWidth: 0 }}>
          <div style={{
            height: 5, borderRadius: 99, background: shades[i % shades.length],
            opacity: i % 2 ? 0.75 : 0.9,
          }} />
          <p style={{
            fontWeight: 700, fontSize: 13.5, marginTop: 7,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {c.town || (c.city || '').split(',')[0]}
          </p>
          <p className="muted" style={{ fontSize: 11.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {c.nights} night{c.nights === 1 ? '' : 's'}{c.start ? ` · ${fmt(c.start)} → ${fmt(c.end)}` : ''}
          </p>
        </div>
      ))}
    </div>
  );
}

/* The Maps Embed API browser key (separate, referrer-restricted, free) —
   fetched once per session; null means static maps only, no tap-to-explore. */
let embedKeyPromise = null;
function mapsEmbedKey() {
  if (!embedKeyPromise) {
    embedKeyPromise = api.mapsEmbedKey().then(r => r.key || null).catch(() => null);
  }
  return embedKeyPromise;
}

/* Full-screen interactive Google map (Maps Embed API) behind a tap on the
   static picture — pan, zoom, Street View, live transit directions. */
function MapSheet({ title, src, onClose }) {
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 90, background: 'var(--bg, #F6F2E8)',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 18px' }}>
        <p className="eyebrow" style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 8 }}>
          <MapPin size={13} strokeWidth={2.5} /> {title}
        </p>
        <button className="btn-quiet" onClick={onClose}>Close</button>
      </div>
      <iframe
        title={title}
        src={src}
        style={{ flex: 1, width: '100%', border: 0 }}
        allowFullScreen
        loading="lazy"
        referrerPolicy="no-referrer-when-downgrade"
      />
    </div>
  );
}

function embedUrl(key, embed) {
  const params = new URLSearchParams({ key, ...embed.params });
  return `https://www.google.com/maps/embed/v1/${embed.mode}?${params}`;
}

/* A Rovyn-styled map from the backend's Static Maps proxy. `load` returns a
   blob URL (api.routeMap / api.optionMap — session-cached, so re-mounts are
   instant). Reserves its aspect ratio while loading; vanishes quietly if no
   map exists — maps are an enhancement, never a hole in the page.
   With an `embed` prop ({mode, params} for the Maps Embed API) and a
   configured browser key, tapping the picture opens the interactive map. */
export function MapImage({ load, ratio = 2, alt = 'Map', style, header = null, embed = null }) {
  const [src, setSrc] = useState(null);
  const [failed, setFailed] = useState(false);
  const [embedKey, setEmbedKey] = useState(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let dead = false;
    load().then(url => { if (!dead) setSrc(url); })
      .catch(() => { if (!dead) setFailed(true); });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!embed) return;
    let dead = false;
    mapsEmbedKey().then(k => { if (!dead) setEmbedKey(k); });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(embed)]);
  if (failed) return null; // the header vanishes with the map — no orphans
  const interactive = Boolean(embed && embedKey && src);
  return (
    <>
      {header}
      {src ? (
        <div style={{ position: 'relative', ...style }}>
          <motion.img
            src={src} alt={alt} draggable={false}
            onClick={() => interactive && setOpen(true)}
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.35 }}
            style={{ width: '100%', height: 'auto', display: 'block', borderRadius: 4,
                     cursor: interactive ? 'pointer' : 'default' }}
          />
          {interactive && (
            <span style={{
              position: 'absolute', right: 10, bottom: 10, padding: '4px 12px',
              borderRadius: 999, background: 'rgba(35, 49, 43, 0.62)', color: '#F6F2E8',
              fontSize: 11.5, fontWeight: 700, pointerEvents: 'none',
            }}>
              tap to explore
            </span>
          )}
        </div>
      ) : (
        <div className="map-loading" style={{ aspectRatio: `${ratio} / 1`, ...style }} />
      )}
      {open && interactive && (
        <MapSheet title={alt} src={embedUrl(embedKey, embed)} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

/* Wide place photo with photographer credit (renders nothing without an image) */
export function Hero({ image, height = 230, style }) {
  if (!image?.url) return null;
  return (
    <figure style={{ margin: 0, position: 'relative', ...style }}>
      <img
        className="hero-img"
        src={image.url} alt={image.alt || ''} loading="lazy"
        style={{ width: '100%', height, objectFit: 'cover', borderRadius: 4, display: 'block' }}
      />
      {image.credit && (
        <figcaption style={{
          position: 'absolute', right: 10, bottom: 8, fontSize: 11,
          color: 'rgba(246, 242, 232, 0.9)', textShadow: '0 1px 4px rgba(0, 0, 0, 0.55)',
        }}>
          Photo: {image.credit} · Pexels
        </figcaption>
      )}
    </figure>
  );
}

/* Photo gallery — browse a stay's real photos in place. Swipe on touch,
   arrows on desktop. data-noswipe keeps the tab-level swipe hands-off. */
export function PhotoCarousel({ photos, height = 220, style }) {
  const [idx, setIdx] = useState(0);
  const touch = useRef(null);
  if (!photos?.length) return null;
  const i = Math.min(idx, photos.length - 1);
  const photo = photos[i];
  const next = photos[(i + 1) % photos.length];
  const go = delta => setIdx((i + delta + photos.length) % photos.length);
  const onTouchStart = e => {
    touch.current = { x: e.touches[0].clientX, y: e.touches[0].clientY };
  };
  const onTouchEnd = e => {
    const t = touch.current;
    touch.current = null;
    if (!t || photos.length < 2) return;
    const dx = e.changedTouches[0].clientX - t.x;
    const dy = e.changedTouches[0].clientY - t.y;
    if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) * 1.2) go(dx < 0 ? 1 : -1);
  };
  const arrowStyle = side => ({
    position: 'absolute', top: '50%', transform: 'translateY(-50%)', [side]: 10,
    width: 34, height: 34, borderRadius: '50%', display: 'inline-flex',
    alignItems: 'center', justifyContent: 'center', flexShrink: 0,
    background: 'rgba(35, 49, 43, 0.55)', color: '#F6F2E8',
    transition: 'background 0.2s ease',
  });
  return (
    <div
      data-noswipe
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
      style={{ position: 'relative', borderRadius: 4, overflow: 'hidden', background: 'var(--bg-soft)', ...style }}
    >
      <motion.img
        className="hero-img"
        key={photo.url}
        src={photo.url}
        alt={photo.caption || 'hotel photo'}
        title={photo.caption || undefined}
        initial={{ opacity: 0.35 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.3 }}
        loading="lazy"
        style={{ width: '100%', height, objectFit: 'cover', display: 'block' }}
      />
      {photos.length > 1 && (
        <>
          {/* warm the next photo so browsing feels instant */}
          <img src={next.url} alt="" aria-hidden="true" style={{ display: 'none' }} />
          <button type="button" aria-label="Previous photo"
                  onClick={e => { e.stopPropagation(); go(-1); }} style={arrowStyle('left')}>
            <ChevronLeft size={18} strokeWidth={2.4} />
          </button>
          <button type="button" aria-label="Next photo"
                  onClick={e => { e.stopPropagation(); go(1); }} style={arrowStyle('right')}>
            <ChevronRight size={18} strokeWidth={2.4} />
          </button>
          <span style={{
            position: 'absolute', right: 10, top: 8, padding: '3px 10px', borderRadius: 999,
            background: 'rgba(35, 49, 43, 0.55)', color: '#F6F2E8', fontSize: 11.5, fontWeight: 700,
          }}>
            {i + 1} / {photos.length}
          </span>
          <span style={{
            position: 'absolute', left: 0, right: 0, bottom: 8, display: 'flex',
            justifyContent: 'center', gap: 6,
          }}>
            {photos.map((p, d) => (
              <button
                key={p.url}
                type="button"
                aria-label={`Photo ${d + 1}`}
                onClick={e => { e.stopPropagation(); setIdx(d); }}
                style={{
                  width: 7, height: 7, borderRadius: '50%', padding: 0,
                  background: d === i ? '#F6F2E8' : 'rgba(246, 242, 232, 0.45)',
                  boxShadow: '0 1px 3px rgba(0, 0, 0, 0.35)',
                }}
              />
            ))}
          </span>
        </>
      )}
    </div>
  );
}

/* Guest reviews under a stay or venue — LiteAPI guest feedback (pros/cons)
   or Google reviews (text). Fully tucked away until the traveler asks: the
   closed state is a single quiet line, never a wall of review text. */
export function GuestReviews({ media, fallbackRating, defaultOpen = false,
                               title = 'Guest reviews', fallbackHeadline = 'A recent stay' }) {
  const [open, setOpen] = useState(defaultOpen);
  const reviews = media?.reviews || [];
  if (reviews.length === 0) return null;
  const rating = media?.rating10 ?? fallbackRating;
  const summary = [
    rating != null ? `${Number(rating).toFixed(1)}/10` : null,
    media?.review_count > 0 ? `${media.review_count.toLocaleString()} reviews` : `${reviews.length} reviews`,
  ].filter(Boolean).join(' · ');

  if (!open) {
    return (
      <button className="btn-quiet" style={{ fontSize: 13, marginTop: 10 }}
              onClick={e => { e.stopPropagation(); setOpen(true); }}>
        <Quote size={12} strokeWidth={2.5} /> Read {title.toLowerCase()}
        <span className="muted" style={{ fontWeight: 600 }}>{summary}</span>
      </button>
    );
  }
  return (
    <div style={{ marginTop: 14 }}>
      <p className="eyebrow" style={{ fontSize: 10.5, display: 'flex', alignItems: 'center', gap: 7, marginBottom: 2 }}>
        <Quote size={12} strokeWidth={2.5} /> {title}
        {rating != null && <span style={{ color: 'var(--clay)' }}>{Number(rating).toFixed(1)}/10</span>}
        {media?.review_count > 0 && (
          <span className="muted" style={{ letterSpacing: 0, textTransform: 'none', fontWeight: 600 }}>
            · {media.review_count.toLocaleString()} reviews
          </span>
        )}
      </p>
      {reviews.map((r, i) => (
        <div key={`${r.author}-${r.date}-${i}`} style={{ padding: '10px 0', borderTop: '1px dashed var(--line-soft)' }}>
          <p style={{ fontSize: 13.5 }}>
            <strong>{r.headline || fallbackHeadline}</strong>
            <span className="muted">
              {' '}— {r.author}
              {r.traveler_type ? ` · ${r.traveler_type}` : ''}
              {r.date ? ` · ${r.date}` : ''}
              {r.score10 != null ? ` · rated ${Number(r.score10).toFixed(1)}/10` : ''}
            </span>
          </p>
          {r.pros && (
            <p style={{ fontSize: 13.5, color: 'var(--ink-soft)', marginTop: 2 }}>
              <span style={{ color: 'var(--pine)', fontWeight: 700 }}>+</span> {r.pros}
            </p>
          )}
          {r.cons && (
            <p style={{ fontSize: 13.5, color: 'var(--ink-soft)', marginTop: 2 }}>
              <span style={{ color: 'var(--clay)', fontWeight: 700 }}>−</span> {r.cons}
            </p>
          )}
          {r.text && <p style={{ fontSize: 13.5, color: 'var(--ink-soft)', marginTop: 2 }}>{r.text}</p>}
        </div>
      ))}
      {!defaultOpen && (
        <button className="btn-quiet" style={{ fontSize: 13 }}
                onClick={e => { e.stopPropagation(); setOpen(false); }}>
          Hide reviews
        </button>
      )}
    </div>
  );
}

/* Small list thumbnail (renders nothing without an image) */
export function Thumb({ image, size = 84, style }) {
  if (!image?.thumb) return null;
  return (
    <img
      className="thumb"
      src={image.thumb} alt={image.alt || ''} loading="lazy"
      title={image.credit ? `Photo: ${image.credit} · Pexels` : undefined}
      style={{
        width: Math.round(size * 1.4), height: size, objectFit: 'cover',
        borderRadius: 3, flexShrink: 0, ...style,
      }}
    />
  );
}

/* Abstract horizon — sun over ridgelines, in-line so it inherits the palette */
export function HorizonArt({ height = 190 }) {
  return (
    <svg viewBox="0 0 640 200" style={{ width: '100%', height, display: 'block' }} aria-hidden="true">
      <defs>
        <linearGradient id="sunG" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#BE6B3F" stopOpacity="0.9" />
          <stop offset="100%" stopColor="#BE6B3F" stopOpacity="0.45" />
        </linearGradient>
      </defs>
      <circle cx="430" cy="86" r="46" fill="url(#sunG)" />
      {/* back ridge */}
      <path d="M0 150 Q 90 96 180 128 T 360 118 T 540 132 T 640 120 L 640 200 L 0 200 Z"
            fill="#2E5D4F" opacity="0.30" />
      {/* mid ridge */}
      <path d="M0 168 Q 120 122 230 150 T 430 144 T 640 152 L 640 200 L 0 200 Z"
            fill="#2E5D4F" opacity="0.55" />
      {/* front ridge */}
      <path d="M0 188 Q 150 152 300 176 T 640 178 L 640 200 L 0 200 Z"
            fill="#1F4238" opacity="0.9" />
      {/* birds */}
      <path d="M240 70 q 7 -7 14 0 M254 70 q 7 -7 14 0" stroke="#23312B" strokeWidth="2"
            fill="none" strokeLinecap="round" opacity="0.55" />
      <path d="M300 52 q 6 -6 12 0 M312 52 q 6 -6 12 0" stroke="#23312B" strokeWidth="2"
            fill="none" strokeLinecap="round" opacity="0.4" />
    </svg>
  );
}
