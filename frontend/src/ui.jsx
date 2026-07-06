import { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, Check, ChevronLeft, ChevronRight, Compass, LogOut, Loader2, MapPin } from 'lucide-react';
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

function NavLink({ to, label }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const active = pathname === to || pathname.startsWith(`${to}/`);
  return (
    <button
      onClick={() => navigate(to)}
      style={{
        fontSize: 13, fontWeight: 700, letterSpacing: '0.14em', textTransform: 'uppercase',
        color: active ? 'var(--clay)' : 'var(--pine)', padding: '4px 0',
      }}
    >
      {label}
    </button>
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
          <NavLink to="/feed" label="Feed" />
          <NavLink to="/wishlist" label="Wishlist" />
          <NavLink to="/trips" label="Trips" />
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

export function Page({ children, width = 680, nav = true }) {
  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: width }}>
        {nav && <Nav />}
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

/* Photo gallery — browse a stay's real photos in place with arrows and dots */
export function PhotoCarousel({ photos, height = 220, style }) {
  const [idx, setIdx] = useState(0);
  if (!photos?.length) return null;
  const i = Math.min(idx, photos.length - 1);
  const photo = photos[i];
  const next = photos[(i + 1) % photos.length];
  const go = (delta, e) => {
    e.stopPropagation();
    setIdx((i + delta + photos.length) % photos.length);
  };
  const arrowStyle = side => ({
    position: 'absolute', top: '50%', transform: 'translateY(-50%)', [side]: 10,
    width: 34, height: 34, borderRadius: '50%', display: 'inline-flex',
    alignItems: 'center', justifyContent: 'center', flexShrink: 0,
    background: 'rgba(35, 49, 43, 0.55)', color: '#F6F2E8',
    transition: 'background 0.2s ease',
  });
  return (
    <div style={{ position: 'relative', borderRadius: 4, overflow: 'hidden', background: 'var(--bg-soft)', ...style }}>
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
          <button type="button" aria-label="Previous photo" onClick={e => go(-1, e)} style={arrowStyle('left')}>
            <ChevronLeft size={18} strokeWidth={2.4} />
          </button>
          <button type="button" aria-label="Next photo" onClick={e => go(1, e)} style={arrowStyle('right')}>
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
