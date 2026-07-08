import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { ArrowRight, Check, CheckCircle2, Compass, LogOut, MapPin, Plus, Search, Ticket, Trash2, Crown, Users, X } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, Loading, ErrorText, CityInput } from '../ui.jsx';

/* The three accents trip rows and status pills draw from — solid for text,
   tint for fills. `sun` is defined in index.css alongside pine and clay. */
const PALETTE = {
  pine: { solid: 'var(--pine)', tint: 'rgba(46, 93, 79, 0.11)' },
  clay: { solid: 'var(--clay)', tint: 'rgba(190, 107, 63, 0.13)' },
  sun: { solid: 'var(--sun)', tint: 'rgba(169, 128, 43, 0.15)' },
};
const DISC_CYCLE = ['pine', 'clay', 'sun'];

function Pill({ color, children }) {
  const c = PALETTE[color] || PALETTE.pine;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '3px 11px', borderRadius: 999, whiteSpace: 'nowrap',
      fontSize: 12, fontWeight: 700, background: c.tint, color: c.solid,
    }}>
      {children}
    </span>
  );
}

function Countdown({ dates }) {
  if (!dates?.start) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const start = new Date(`${dates.start}T00:00:00`);
  const end = dates.end ? new Date(`${dates.end}T00:00:00`) : start;
  const days = Math.round((start - today) / 86400000);
  let text, color = 'clay';
  if (days > 1) text = `✈ ${days} days to go`;
  else if (days === 1) text = '✈ Tomorrow!';
  else if (days === 0) text = '✈ Today — bon voyage!';
  else if (today <= end) { text = '🌴 Trip in progress'; color = 'pine'; }
  else { text = 'Trip completed'; color = 'sun'; }
  return <Pill color={color}>{text}</Pill>;
}

// How the trip's destination gets decided: pinned by the creator, or
// suggested by Rovyn from the questionnaires and voted on.
const DESTINATION_MODES = [
  { id: 'known', Icon: MapPin, label: 'We know where' },
  { id: 'suggest', Icon: Compass, label: 'Surprise us' },
];

function ModePills({ value, onChange }) {
  return (
    <div style={{ display: 'inline-flex', gap: 4, padding: 4, border: '1px solid var(--line)', borderRadius: 999 }}>
      {DESTINATION_MODES.map(({ id, Icon, label }) => {
        const active = value === id;
        return (
          <button key={id} type="button" onClick={() => onChange(id)}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 7,
                    padding: '8px 16px', borderRadius: 999, fontSize: 13.5, fontWeight: 700,
                    background: active ? 'var(--pine)' : 'transparent',
                    color: active ? '#F6F2E8' : 'var(--ink-soft)',
                    transition: 'all 0.18s ease', whiteSpace: 'nowrap',
                  }}>
            <Icon size={14} strokeWidth={2.2} /> {label}
          </button>
        );
      })}
    </div>
  );
}

/* Reveal-on-change wrapper for the conditional form sections */
function Unfold({ id, children }) {
  return (
    <motion.div key={id} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.25, ease: 'easeOut' }}
                style={{ overflow: 'hidden' }}>
      {children}
    </motion.div>
  );
}

// Where a trip sits in the journey sets its pill color: pine while the
// group gathers, sun through the deciding middle, clay at the finish line.
const STATUS_META = {
  waiting: { label: 'Gathering preferences', color: 'pine' },
  voting: { label: 'Voting on destinations', color: 'sun' },
  selected: { label: 'Destination chosen', color: 'sun' },
  curating: { label: 'Starring favorites', color: 'sun' },
  final_review: { label: 'Final plan ready', color: 'clay' },
  approved: { label: 'Approved — payment open', color: 'clay' },
};

/* One of the two entry doors: a tinted tappable card that opens its form.
   Both stay side by side on phones — no scrolling past one to reach the other. */
function ActionCard({ active, Icon, title, sub, color, onClick }) {
  const c = PALETTE[color];
  return (
    <button type="button" onClick={onClick} aria-expanded={active}
            style={{
              display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 12,
              padding: '18px 16px 15px', borderRadius: 16, textAlign: 'left',
              border: '1.5px solid', borderColor: active ? c.solid : 'transparent',
              background: c.tint,
              transition: 'border-color 0.2s ease, transform 0.15s ease',
            }}>
      <span style={{
        width: 38, height: 38, borderRadius: '50%', flexShrink: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        background: active ? c.solid : 'transparent',
        border: `1.5px solid ${c.solid}`,
        color: active ? '#F6F2E8' : c.solid,
        transition: 'background 0.2s ease, color 0.2s ease',
      }}>
        <Icon size={17} strokeWidth={2.2} />
      </span>
      <span>
        <span className="serif" style={{ display: 'block', fontSize: 19, color: 'var(--ink)' }}>{title}</span>
        <span className="muted" style={{ display: 'block', fontSize: 12.5, marginTop: 2 }}>{sub}</span>
      </span>
    </button>
  );
}

export default function Trips() {
  const navigate = useNavigate();
  const [trips, setTrips] = useState(null);
  const [mode, setMode] = useState(null); // null | 'create' | 'join'
  const [name, setName] = useState('');
  const [destMode, setDestMode] = useState('known');
  const [destination, setDestination] = useState('');
  const [solo, setSolo] = useState(false);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState(null);
  const [query, setQuery] = useState('');

  // One search across every trip, whatever its stage — sections keep their
  // labels and each shows its share of the matches.
  const q = query.trim().toLowerCase();
  const matchesQuery = t =>
    !q ||
    t.name.toLowerCase().includes(q) ||
    (t.selected_destination || '').toLowerCase().includes(q);

  useEffect(() => {
    api.myTrips()
      .then(d => {
        setTrips(d.trips);
        // A brand-new traveler has nothing to tap through — open the door for them
        if (d.trips.length === 0) setMode('create');
      })
      .catch(err => setError(err.message));
  }, []);

  async function removeTrip(t) {
    setBusy(true);
    setError('');
    try {
      if (t.is_leader) await api.deleteTrip(t.id);
      else await api.leaveTrip(t.id);
      setTrips(prev => prev.filter(x => x.id !== t.id));
      setConfirmId(null);
    } catch (err) {
      setError(err.message);
      setConfirmId(null);
    } finally {
      setBusy(false);
    }
  }

  async function create(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError('');
    try {
      // Empty destination is the API's signal for suggestion mode
      const t = await api.createTrip(name.trim(), destMode === 'suggest' ? '' : destination.trim(), solo);
      navigate(`/trips/${t.id}`);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  async function join(e) {
    e.preventDefault();
    if (!code.trim()) return;
    setBusy(true);
    setError('');
    try {
      const t = await api.joinTrip(code.trim());
      navigate(`/trips/${t.id}`);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  if (trips === null) return <Page><Loading /></Page>;

  return (
    <Page>
      <p className="eyebrow">Your journeys</p>
      <h1 style={{ fontSize: 42, margin: '10px 0 40px' }}>
        Where to <em style={{ color: 'var(--clay)' }}>next?</em>
      </h1>

      {trips.length > 0 && (
        <div style={{ marginBottom: 56 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 34, maxWidth: 420 }}>
            <Search size={16} strokeWidth={2} color="var(--muted)" style={{ flexShrink: 0 }} />
            <input
              placeholder="Search your trips…"
              aria-label="Search your trips"
              value={query}
              onChange={e => setQuery(e.target.value)}
            />
            {query && (
              <button type="button" aria-label="Clear search" title="Clear search"
                      onClick={() => setQuery('')}
                      style={{ color: 'var(--muted)', padding: 4, flexShrink: 0, display: 'inline-flex' }}>
                <X size={15} strokeWidth={2.2} />
              </button>
            )}
          </div>
          {[
            { key: 'planning', label: 'Organization in progress', Icon: Compass, list: trips.filter(t => t.status !== 'booked') },
            { key: 'confirmed', label: 'Confirmed', Icon: CheckCircle2, list: trips.filter(t => t.status === 'booked') },
          ].filter(section => section.list.length > 0).map(section => {
            const shown = section.list.filter(matchesQuery);
            return (
            <div key={section.key} style={{ marginBottom: 40 }}>
              <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <section.Icon size={13} strokeWidth={2.5} /> {section.label} · {shown.length}
              </p>
              {shown.length === 0 && (
                <p className="muted" style={{ fontSize: 13.5, marginTop: 10 }}>
                  No trips here match “{query.trim()}”.
                </p>
              )}
              {shown.map((t, i) => {
                const disc = PALETTE[DISC_CYCLE[i % DISC_CYCLE.length]];
                return (
                <motion.div
                  key={t.id}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.06, duration: 0.4 }}
                  className="opt-row"
                  style={{ cursor: 'pointer' }}
                  onClick={() => navigate(`/trips/${t.id}`)}
                >
                  {t.photo_url ? (
                    <img
                      src={api.mediaUrl(t.photo_url)} alt="" loading="lazy"
                      style={{ width: 44, height: 44, borderRadius: '50%', objectFit: 'cover', flexShrink: 0 }}
                    />
                  ) : (
                    <span className="serif" aria-hidden="true" style={{
                      width: 44, height: 44, borderRadius: '50%', flexShrink: 0,
                      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                      background: disc.tint, color: disc.solid, fontSize: 20,
                    }}>
                      {(t.name.trim()[0] || '·').toUpperCase()}
                    </span>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: '0 12px', flexWrap: 'wrap' }}>
                      <span className="opt-title serif" style={{ fontSize: 22 }}>{t.name}</span>
                      {t.selected_destination && (
                        <span style={{
                          display: 'inline-flex', alignItems: 'center', gap: 4,
                          color: 'var(--clay)', fontSize: 14, fontWeight: 700, whiteSpace: 'nowrap',
                        }}>
                          <MapPin size={13} strokeWidth={2.2} /> {t.selected_destination}
                        </span>
                      )}
                    </div>
                    <div className="opt-desc" style={{ display: 'flex', alignItems: 'center', gap: '4px 14px', flexWrap: 'wrap', marginTop: 6 }}>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
                        <Users size={13} /> {t.member_count} traveler{t.member_count === 1 ? '' : 's'}
                      </span>
                      {t.is_leader && (
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', color: 'var(--sun)', fontWeight: 700 }}>
                          <Crown size={13} /> You lead
                        </span>
                      )}
                      {t.status === 'booked'
                        ? <Countdown dates={t.dates} />
                        : <Pill color={STATUS_META[t.status]?.color || 'pine'}>{STATUS_META[t.status]?.label || t.status}</Pill>}
                    </div>
                  </div>
                  {confirmId === t.id ? (
                    <span style={{ display: 'inline-flex', gap: 14, alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                      <button
                        disabled={busy}
                        onClick={() => removeTrip(t)}
                        style={{ color: 'var(--clay)', fontWeight: 700, fontSize: 13.5 }}
                      >
                        {t.is_leader ? 'Delete for everyone?' : 'Leave this trip?'}
                      </button>
                      <button className="muted" style={{ fontSize: 13.5 }} onClick={() => setConfirmId(null)}>cancel</button>
                    </span>
                  ) : (
                    <button
                      title={t.is_leader ? 'Delete trip' : 'Leave trip'}
                      onClick={e => { e.stopPropagation(); setConfirmId(t.id); }}
                      style={{ color: 'var(--muted)', padding: 6, marginRight: 4 }}
                    >
                      {t.is_leader ? <Trash2 size={16} strokeWidth={1.8} /> : <LogOut size={16} strokeWidth={1.8} />}
                    </button>
                  )}
                  <ArrowRight size={18} color="var(--pine)" />
                </motion.div>
                );
              })}
            </div>
            );
          })}
        </div>
      )}

      {/* Two doors in, side by side even on a phone — tap one and its form unfolds */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 8 }}>
        <ActionCard
          Icon={Plus} color="pine" title="Start a trip" sub="Plan something new"
          active={mode === 'create'}
          onClick={() => setMode(m => m === 'create' ? null : 'create')}
        />
        <ActionCard
          Icon={Ticket} color="clay" title="Have an invite?" sub="Join with a code"
          active={mode === 'join'}
          onClick={() => setMode(m => m === 'join' ? null : 'join')}
        />
      </div>

      <AnimatePresence initial={false} mode="wait">
        {mode === 'create' && (
          <Unfold id="create-form">
            <form onSubmit={create} style={{ padding: '26px 4px 8px' }}>
              <Field label="Trip name">
                <input placeholder="Summer in the Algarve" value={name} autoFocus
                       onChange={e => setName(e.target.value)} />
              </Field>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 16, flexWrap: 'wrap', marginBottom: 14 }}>
                <p className="eyebrow" style={{ fontSize: 11 }}>Destination</p>
                <ModePills value={destMode} onChange={setDestMode} />
              </div>
              <AnimatePresence initial={false} mode="wait">
                {destMode === 'known' ? (
                  <Unfold id="known">
                    <Field label="Where to?">
                      <CityInput placeholder="Hvar, Croatia" value={destination} onChange={setDestination} />
                    </Field>
                  </Unfold>
                ) : (
                  <Unfold id="suggest">
                    <p className="muted" style={{ fontSize: 13, marginBottom: 26, maxWidth: 400 }}>
                      {solo
                        ? 'Rovyn reads your answers and proposes five matched destinations — you pick your favorite.'
                        : 'Rovyn reads everyone’s answers and proposes five matched destinations — the group votes.'}
                    </p>
                  </Unfold>
                )}
              </AnimatePresence>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', margin: '2px 0 26px' }}>
                <button type="button" onClick={() => setSolo(s => !s)}
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: 9,
                          fontSize: 13.5, fontWeight: 600,
                          color: solo ? 'var(--pine)' : 'var(--ink-soft)',
                        }}>
                  <span style={{
                    width: 17, height: 17, borderRadius: 5, border: '1.5px solid',
                    borderColor: solo ? 'var(--pine)' : 'var(--line)',
                    background: solo ? 'var(--pine)' : 'transparent',
                    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                    transition: 'all 0.15s ease', flexShrink: 0,
                  }}>
                    {solo && <Check size={12} strokeWidth={3.2} color="#F6F2E8" />}
                  </span>
                  Travelling solo?
                </button>
                <AnimatePresence>
                  {solo && (
                    <motion.span className="muted" style={{ fontSize: 12.5 }}
                                 initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                      No invites — this journey is all yours.
                    </motion.span>
                  )}
                </AnimatePresence>
              </div>
              <button className="btn" disabled={busy || !name.trim() || (destMode === 'known' && !destination.trim())}>
                Create <ArrowRight size={16} />
              </button>
            </form>
          </Unfold>
        )}

        {mode === 'join' && (
          <Unfold id="join-form">
            <form onSubmit={join} style={{ padding: '26px 4px 8px' }}>
              <Field label="Invite code">
                <input
                  placeholder="AB12CD"
                  value={code}
                  autoFocus
                  onChange={e => setCode(e.target.value.toUpperCase())}
                  maxLength={6}
                  style={{ letterSpacing: 6, fontWeight: 600 }}
                />
              </Field>
              <button className="btn clay" disabled={busy || !code.trim()}>
                Join <ArrowRight size={16} />
              </button>
            </form>
          </Unfold>
        )}
      </AnimatePresence>

      <ErrorText>{error}</ErrorText>
    </Page>
  );
}
