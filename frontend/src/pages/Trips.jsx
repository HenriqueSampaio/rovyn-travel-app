import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { ArrowRight, Check, CheckCircle2, Compass, LogOut, MapPin, Plus, Ticket, Trash2, Crown, Users } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, Loading, ErrorText, CityInput } from '../ui.jsx';

function Countdown({ dates }) {
  if (!dates?.start) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const start = new Date(`${dates.start}T00:00:00`);
  const end = dates.end ? new Date(`${dates.end}T00:00:00`) : start;
  const days = Math.round((start - today) / 86400000);
  let text;
  if (days > 1) text = `✈ ${days} days to go`;
  else if (days === 1) text = '✈ Tomorrow!';
  else if (days === 0) text = '✈ Today — bon voyage!';
  else if (today <= end) text = '🌴 Trip in progress';
  else text = 'Trip completed';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--clay)', fontWeight: 700 }}>
      {text}
    </span>
  );
}

// How the trip's destination gets decided: pinned by the creator, or
// suggested by Rovyn from the questionnaires and voted on by the group.
const DESTINATION_MODES = [
  {
    id: 'known',
    Icon: MapPin,
    title: 'We know where we’re going',
    desc: 'Pin the destination now — the group plans around it',
  },
  {
    id: 'suggest',
    Icon: Compass,
    title: 'Let Rovyn suggest destinations',
    desc: 'Five matched places to vote on, drawn from everyone’s answers',
  },
];

function ModeRow({ selected, onClick, Icon, title, desc }) {
  return (
    <button className={`opt-row${selected ? ' selected' : ''}`} onClick={onClick} type="button">
      <Icon size={18} strokeWidth={1.7} color={selected ? 'var(--pine)' : 'var(--muted)'} />
      <span style={{ flex: 1 }}>
        <span className="opt-title" style={{ display: 'block', fontSize: 15.5 }}>{title}</span>
        <span className="opt-desc">{desc}</span>
      </span>
      {selected && <Check size={16} strokeWidth={2.4} color="var(--pine)" />}
    </button>
  );
}

const STATUS_LABEL = {
  waiting: 'Gathering preferences',
  voting: 'Voting on destinations',
  selected: 'Destination chosen',
  curating: 'Starring favorites',
  final_review: 'Final plan ready',
  approved: 'Approved — payment open',
  booked: 'Booked ✓',
};

export default function Trips() {
  const navigate = useNavigate();
  const [trips, setTrips] = useState(null);
  const [name, setName] = useState('');
  const [destMode, setDestMode] = useState('known');
  const [destination, setDestination] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState(null);

  useEffect(() => {
    api.myTrips().then(d => setTrips(d.trips)).catch(err => setError(err.message));
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
      const t = await api.createTrip(name.trim(), destMode === 'suggest' ? '' : destination.trim());
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
      <h1 style={{ fontSize: 42, margin: '10px 0 40px' }}>Where to next?</h1>

      {trips.length > 0 && (
        <div style={{ marginBottom: 64 }}>
          {[
            { key: 'planning', label: 'Organization in progress', Icon: Compass, list: trips.filter(t => t.status !== 'booked') },
            { key: 'confirmed', label: 'Confirmed', Icon: CheckCircle2, list: trips.filter(t => t.status === 'booked') },
          ].filter(section => section.list.length > 0).map(section => (
            <div key={section.key} style={{ marginBottom: 40 }}>
              <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <section.Icon size={13} strokeWidth={2.5} /> {section.label} · {section.list.length}
              </p>
              {section.list.map((t, i) => (
                <motion.div
                  key={t.id}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.06, duration: 0.4 }}
                  className="opt-row"
                  style={{ cursor: 'pointer' }}
                  onClick={() => navigate(`/trips/${t.id}`)}
                >
                  <div style={{ flex: 1 }}>
                    <div className="opt-title serif" style={{ fontSize: 22 }}>
                      {t.selected_destination ? `${t.name} — ${t.selected_destination}` : t.name}
                    </div>
                    <div className="opt-desc" style={{ display: 'flex', alignItems: 'center', gap: '2px 14px', flexWrap: 'wrap', marginTop: 4 }}>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
                        <Users size={13} /> {t.member_count} traveler{t.member_count === 1 ? '' : 's'}
                      </span>
                      {t.is_leader && (
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
                          <Crown size={13} /> You lead
                        </span>
                      )}
                      {t.status === 'booked' ? <Countdown dates={t.dates} /> : <span>{STATUS_LABEL[t.status] || t.status}</span>}
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
              ))}
            </div>
          ))}
        </div>
      )}

      <div className="two-col" style={{ rowGap: 56 }}>
        <form onSubmit={create}>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
            <Plus size={13} strokeWidth={2.5} /> Start a trip
          </p>
          <Field label="Trip name">
            <input placeholder="Summer in the Algarve" value={name} onChange={e => setName(e.target.value)} />
          </Field>
          <p className="eyebrow" style={{ fontSize: 11, marginBottom: 4 }}>Destination</p>
          {DESTINATION_MODES.map(m => (
            <ModeRow key={m.id} selected={destMode === m.id} Icon={m.Icon}
                     title={m.title} desc={m.desc}
                     onClick={() => setDestMode(m.id)} />
          ))}
          {destMode === 'known' ? (
            <div style={{ marginTop: 22 }}>
              <Field label="Where to?">
                <CityInput placeholder="Hvar, Croatia" value={destination} onChange={setDestination} />
              </Field>
            </div>
          ) : (
            <p className="muted" style={{ fontSize: 13, margin: '18px 0 28px', maxWidth: 400 }}>
              Once everyone answers the questionnaire, Rovyn proposes five destinations
              matched to the group's vibes, budget and dates — then you all vote.
            </p>
          )}
          <button className="btn" disabled={busy || !name.trim() || (destMode === 'known' && !destination.trim())}>
            Create <ArrowRight size={16} />
          </button>
        </form>

        <form onSubmit={join}>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
            <Ticket size={13} strokeWidth={2.5} /> Have an invite?
          </p>
          <Field label="Invite code">
            <input
              placeholder="AB12CD"
              value={code}
              onChange={e => setCode(e.target.value.toUpperCase())}
              maxLength={6}
              style={{ letterSpacing: 6, fontWeight: 600 }}
            />
          </Field>
          <button className="btn clay" disabled={busy || !code.trim()}>
            Join <ArrowRight size={16} />
          </button>
        </form>
      </div>

      <ErrorText>{error}</ErrorText>
    </Page>
  );
}
