import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowRight, Check, ClipboardList, Copy, Crown, Link2, Loader2, Sparkles, Users,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, Loading, ErrorText } from '../ui.jsx';

export default function Dashboard() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const [trip, setTrip] = useState(null);
  const [error, setError] = useState('');
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState('');
  const pollRef = useRef(null);

  useEffect(() => {
    async function poll() {
      try {
        const data = await api.tripStatus(tripId);
        setTrip(data);
        if (data.has_recommendations) {
          clearInterval(pollRef.current);
          navigate(`/trips/${tripId}/vote`);
        }
      } catch (err) {
        if (err.status === 401) navigate('/');
        else if (err.status === 403 || err.status === 404) navigate('/trips');
      }
    }
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => clearInterval(pollRef.current);
  }, [tripId, navigate]);

  if (!trip) return <Page><Loading /></Page>;

  const inviteLink = `${window.location.origin}/join/${trip.invite_code}`;
  const progress = trip.total_members ? trip.submitted_count / trip.total_members : 0;

  function copy(text, which) {
    navigator.clipboard.writeText(text);
    setCopied(which);
    setTimeout(() => setCopied(''), 2000);
  }

  async function generate() {
    setGenerating(true);
    setError('');
    try {
      await api.generateRecommendations(tripId);
      navigate(`/trips/${tripId}/vote`);
    } catch (err) {
      setError(err.message);
      setGenerating(false);
    }
  }

  return (
    <Page>
      <p className="eyebrow">{trip.is_leader ? 'You lead this trip' : 'You are traveling on'}</p>
      <h1 style={{ fontSize: 44, margin: '10px 0 6px' }}>{trip.name}</h1>
      <p className="muted" style={{ marginBottom: 52 }}>
        {trip.total_members} traveler{trip.total_members === 1 ? '' : 's'} · {trip.submitted_count} of {trip.total_members} preferences shared
      </p>

      {/* Progress line */}
      <div style={{ height: 2, background: 'var(--line-soft)', marginBottom: 44 }}>
        <motion.div
          animate={{ width: `${progress * 100}%` }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
          style={{ height: '100%', background: 'var(--pine)' }}
        />
      </div>

      {/* Travelers */}
      <section style={{ marginBottom: 60 }}>
        <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <Users size={13} strokeWidth={2.5} /> Travelers
        </p>
        {trip.members.map(m => (
          <div key={m.user_id} className="opt-row" style={{ cursor: 'default' }}>
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className="opt-title">{m.name}</span>
              {m.is_leader && <Crown size={14} color="var(--clay)" />}
              {m.user_id === trip.leader_id && trip.is_leader && null}
            </div>
            {m.submitted ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: 'var(--pine)', fontSize: 14, fontWeight: 600 }}>
                <Check size={15} strokeWidth={2.5} /> Preferences in
              </span>
            ) : (
              <span className="muted" style={{ fontSize: 14, fontStyle: 'italic' }}>waiting…</span>
            )}
          </div>
        ))}
      </section>

      {/* My questionnaire */}
      {!trip.me.submitted && (
        <section style={{ marginBottom: 60 }}>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
            <ClipboardList size={13} strokeWidth={2.5} /> Your turn
          </p>
          <p className="muted" style={{ marginBottom: 20, maxWidth: 440 }}>
            Tell the group your dates, budget and travel style — it takes two minutes
            and shapes where you all end up.
          </p>
          <button className="btn" onClick={() => navigate(`/trips/${tripId}/questionnaire`)}>
            Share my preferences <ArrowRight size={16} />
          </button>
        </section>
      )}

      {/* Invite */}
      <section style={{ marginBottom: 60 }}>
        <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
          <Link2 size={13} strokeWidth={2.5} /> Bring more people
        </p>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 24, flexWrap: 'wrap' }}>
          <span className="serif" style={{ fontSize: 40, letterSpacing: 8, color: 'var(--pine)' }}>
            {trip.invite_code}
          </span>
          <div style={{ display: 'flex', gap: 18 }}>
            <button className="btn-quiet" onClick={() => copy(trip.invite_code, 'code')}>
              {copied === 'code' ? <Check size={15} /> : <Copy size={15} />} code
            </button>
            <button className="btn-quiet" onClick={() => copy(inviteLink, 'link')}>
              {copied === 'link' ? <Check size={15} /> : <Link2 size={15} />} invite link
            </button>
          </div>
        </div>
      </section>

      <hr className="rule" style={{ marginBottom: 44 }} />

      {/* Destination matching */}
      <section>
        <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
          <Sparkles size={13} strokeWidth={2.5} /> Destination matching
        </p>
        {trip.recommendations_generating || generating ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <Loader2 className="spin" size={22} strokeWidth={1.8} color="var(--pine)" />
            <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
              Reading everyone's answers and scouting destinations…
            </p>
          </div>
        ) : trip.all_submitted ? (
          trip.is_leader ? (
            <>
              <p className="muted" style={{ marginBottom: 20, maxWidth: 460 }}>
                Everyone's preferences are in. Rovyn will propose five destinations
                tuned to the whole group, ready for a vote.
              </p>
              <button className="btn" onClick={generate}>
                Reveal our destinations <ArrowRight size={16} />
              </button>
            </>
          ) : (
            <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
              Everyone's in — waiting for the trip leader to reveal the destinations.
            </p>
          )
        ) : (
          <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
            Unlocks once all {trip.total_members} traveler{trip.total_members === 1 ? ' has' : 's have'} shared preferences.
          </p>
        )}
        <ErrorText>{error}</ErrorText>
      </section>
    </Page>
  );
}
