import { motion } from 'framer-motion';
import { useLocation, useNavigate } from 'react-router-dom';
import { Check, Compass, LogOut, Loader2 } from 'lucide-react';
import { auth } from './api/index.js';

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
  const user = auth.user();
  return (
    <nav style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '28px 0 20px', borderBottom: '1px solid var(--line-soft)', marginBottom: 48,
    }}>
      <button onClick={() => navigate('/trips')} style={{ padding: 0 }}>
        <Wordmark />
      </button>
      {user && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <NavLink to="/feed" label="Feed" />
          <NavLink to="/trips" label="Trips" />
          <span className="muted" style={{ fontSize: 14 }}>{user.name}</span>
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
    <div style={{ minHeight: '100vh', padding: '0 28px' }}>
      <div style={{ maxWidth: width, margin: '0 auto', paddingBottom: 100 }}>
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

/* Journey tracker — where the group is in the planning process */
const JOURNEY_STEPS = [
  { key: 'preferences', label: 'Preferences' },
  { key: 'destination', label: 'Destination' },
  { key: 'curate', label: 'Star favorites' },
  { key: 'plan', label: 'Final plan' },
  { key: 'pay', label: 'Approve & pay' },
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
    <div style={{ display: 'flex', alignItems: 'center', gap: 0, margin: '0 0 44px', flexWrap: 'wrap', rowGap: 12, ...style }}>
      {JOURNEY_STEPS.map((step, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={step.key} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <div style={{
                width: 26, height: 1, margin: '0 8px',
                background: done || active ? 'var(--pine)' : 'var(--line)',
              }} />
            )}
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              fontSize: 11.5, fontWeight: 700, letterSpacing: '0.14em', textTransform: 'uppercase',
              color: done ? 'var(--pine)' : active ? 'var(--clay)' : 'var(--muted)',
              whiteSpace: 'nowrap',
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
