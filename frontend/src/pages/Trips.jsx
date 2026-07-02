import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { ArrowRight, Plus, Ticket, Crown, Users } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, Loading, ErrorText } from '../ui.jsx';

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
  const [destination, setDestination] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.myTrips().then(d => setTrips(d.trips)).catch(err => setError(err.message));
  }, []);

  async function create(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError('');
    try {
      const t = await api.createTrip(name.trim(), destination.trim());
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
          {trips.map((t, i) => (
            <motion.button
              key={t.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.06, duration: 0.4 }}
              className="opt-row"
              onClick={() => navigate(`/trips/${t.id}`)}
            >
              <div style={{ flex: 1 }}>
                <div className="opt-title serif" style={{ fontSize: 22 }}>
                  {t.selected_destination ? `${t.name} — ${t.selected_destination}` : t.name}
                </div>
                <div className="opt-desc" style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 4 }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                    <Users size={13} /> {t.member_count} traveler{t.member_count === 1 ? '' : 's'}
                  </span>
                  {t.is_leader && (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                      <Crown size={13} /> You lead
                    </span>
                  )}
                  <span>{STATUS_LABEL[t.status] || t.status}</span>
                </div>
              </div>
              <ArrowRight size={18} color="var(--pine)" />
            </motion.button>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 56 }}>
        <form onSubmit={create}>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
            <Plus size={13} strokeWidth={2.5} /> Start a trip
          </p>
          <Field label="Trip name">
            <input placeholder="Summer in the Algarve" value={name} onChange={e => setName(e.target.value)} />
          </Field>
          <Field label="Destination — optional">
            <input placeholder="Hvar, Croatia" value={destination} onChange={e => setDestination(e.target.value)} />
          </Field>
          <p className="muted" style={{ fontSize: 13, marginTop: -16, marginBottom: 24 }}>
            {destination.trim()
              ? 'The group will plan around this destination — no destination vote.'
              : 'Leave blank and Rovyn will propose destinations for the group to vote on.'}
          </p>
          <button className="btn" disabled={busy || !name.trim()}>
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
