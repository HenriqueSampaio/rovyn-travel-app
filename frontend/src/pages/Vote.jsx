import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowRight, BedDouble, Check, MapPin, PlaneTakeoff, Plus, Route, Sparkles, Ticket, Trophy, Vote as VoteIcon, X,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, ErrorText, Hero, ThinkingLoader, CityInput, TripAvatar } from '../ui.jsx';

export default function Vote() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const [recs, setRecs] = useState(null);
  const [trip, setTrip] = useState(null);
  const [error, setError] = useState('');
  const [building, setBuilding] = useState(false);
  const [moreBusy, setMoreBusy] = useState(false);
  const [addingFor, setAddingFor] = useState(null); // rec whose add-a-stop form is open
  const [stopQuery, setStopQuery] = useState('');
  const [stopErr, setStopErr] = useState(null);     // { name, message }
  const pollRef = useRef(null);
  const prevGenRef = useRef(false);

  useEffect(() => {
    api.getRecommendations(tripId)
      .then(d => {
        if (!d.recommendations) navigate(`/trips/${tripId}`);
        else setRecs(d.recommendations);
      })
      .catch(() => navigate(`/trips/${tripId}`));

    async function poll() {
      try {
        const data = await api.tripStatus(tripId);
        setTrip(data);
        if (data.has_options) {
          clearInterval(pollRef.current);
          navigate(`/trips/${tripId}/plan`);
          return;
        }
        // A travel mate asked Rovyn for more ideas — pull the grown list when it lands
        if (prevGenRef.current && !data.recommendations_generating) {
          api.getRecommendations(tripId)
            .then(d => d.recommendations && setRecs(d.recommendations))
            .catch(() => {});
        }
        prevGenRef.current = Boolean(data.recommendations_generating);
      } catch {
        /* transient */
      }
    }
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => clearInterval(pollRef.current);
  }, [tripId, navigate]);

  async function vote(name) {
    if (trip?.selected_destination) return;
    setError('');
    try {
      await api.castVote(tripId, name);
      setTrip(await api.tripStatus(tripId));
    } catch (err) {
      setError(err.message);
    }
  }

  async function build() {
    setBuilding(true);
    setError('');
    try {
      await api.generateOptions(tripId);
      navigate(`/trips/${tripId}/plan`);
    } catch (err) {
      setError(err.message);
      setBuilding(false);
    }
  }

  async function more() {
    setMoreBusy(true);
    setError('');
    try {
      const res = await api.moreRecommendations(tripId);
      setRecs(res.recommendations);
    } catch (err) {
      setError(err.message);
    } finally {
      setMoreBusy(false);
    }
  }

  async function addStop(destName, label) {
    const city = (label || '').trim();
    if (!city) return;
    setStopErr(null);
    try {
      const res = await api.addRouteStop(tripId, destName, city);
      setRecs(res.recommendations);
      setAddingFor(null);
      setStopQuery('');
    } catch (err) {
      setStopErr({ name: destName, message: err.message });
    }
  }

  if (!recs || !trip) return <Page><Loading text="Gathering destinations…" /></Page>;

  const solo = trip.solo;
  const winner = trip.selected_destination;
  const tallies = trip.vote_tallies || {};
  const isSample = recs.some(r => r.source === 'sample');

  return (
    <Page width={760}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 10 }}>
        <TripAvatar tripId={tripId} name={trip.name} photoUrl={trip.photo_url} size={64}
                    onChange={url => setTrip(t => ({ ...t, photo_url: url }))} />
        <div style={{ minWidth: 0 }}>
          <p className="eyebrow">{trip.name}</p>
          <h1 style={{ fontSize: 42, margin: '8px 0 0' }}>
            {winner
              ? <>Next stop, <em style={{ color: 'var(--pine)' }}>{winner}</em></>
              : solo ? 'Pick your destination' : 'Cast your vote'}
          </h1>
        </div>
      </div>
      <p className="muted" style={{ marginBottom: isSample ? 10 : 40, maxWidth: 520 }}>
        {winner
          ? solo ? 'Decided — pack your bags.' : 'The group has decided.'
          : solo
            ? 'Your pick decides it — choose the trip that calls to you.'
            : `${trip.votes_cast} of ${trip.total_members} votes in — you can change yours until the last vote lands.`}
      </p>
      {isSample && (
        <p style={{ color: 'var(--clay)', fontSize: 14, marginBottom: 40 }}>
          These are sample destinations — add ANTHROPIC_API_KEY to Rovyn/.env for matches
          tailored to {solo ? 'your answers' : 'your group\'s answers'}.
        </p>
      )}

      {winner && (
        <div style={{ margin: '0 0 48px' }}>
          {trip.options_generating || building ? (
            <ThinkingLoader messages={[
              `Rovyn is scouting ${winner}…`,
              'Assembling flights, stays and experiences…',
              'Rovyn is thinking…',
              'Pricing options for your dates…',
            ]} />
          ) : trip.is_leader ? (
            <button className="btn" onClick={build}>
              Open the planning board <ArrowRight size={16} />
            </button>
          ) : (
            <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17 }}>
              Waiting for the trip leader to open the planning board…
            </p>
          )}
        </div>
      )}

      <ErrorText>{error}</ErrorText>

      {recs.map((dest, i) => {
        const votes = tallies[dest.name] || 0;
        const mine = trip.me?.vote === dest.name;
        const isWinner = winner === dest.name;
        return (
          <motion.article
            key={dest.name}
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.08, duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
            style={{
              padding: '44px 0',
              borderTop: '1px solid var(--line)',
              opacity: winner && !isWinner ? 0.45 : 1,
              transition: 'opacity 0.4s ease',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 18, flexWrap: 'wrap' }}>
              <span className="serif" style={{ fontSize: 15, fontStyle: 'italic', color: 'var(--muted)' }}>
                {String(i + 1).padStart(2, '0')}
              </span>
              <h2 style={{ fontSize: 34 }}>{dest.name}</h2>
              <span className="muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 14 }}>
                <MapPin size={13} /> {dest.country}
              </span>
              <span style={{ flex: 1 }} />
              {isWinner && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: 'var(--clay)', fontWeight: 700, fontSize: 14 }}>
                  <Trophy size={15} /> Chosen
                </span>
              )}
            </div>

            <Hero image={dest.image} height={220} style={{ margin: '16px 0 4px' }} />

            <p className="serif" style={{ fontSize: 19, fontStyle: 'italic', color: 'var(--ink-soft)', margin: '14px 0 18px' }}>
              “{dest.tagline}”
            </p>

            {/* match line */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 14, rowGap: 6, flexWrap: 'wrap', marginBottom: 20 }}>
              <div style={{ flex: 1, minWidth: 120, maxWidth: 260, height: 2, background: 'var(--line-soft)' }}>
                <div style={{ width: `${dest.match_score}%`, height: '100%', background: 'var(--pine)' }} />
              </div>
              <span className="muted" style={{ fontSize: 13 }}>{dest.match_score}% {solo ? 'match' : 'group match'}</span>
              <span className="muted" style={{ fontSize: 13 }}>
                · ${(dest.estimated_budget_per_person?.min || 0).toLocaleString()}–{(dest.estimated_budget_per_person?.max || 0).toLocaleString()} pp
              </span>
            </div>

            <p style={{ fontSize: 15, color: 'var(--ink-soft)', maxWidth: 620, marginBottom: 14 }}>
              {dest.why_it_matches}
            </p>

            {Array.isArray(dest.route) && dest.route.length > 1 ? (
              // Structured multi-city route — the trip this card is pitching
              <p className="muted" style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', columnGap: 9, rowGap: 4, fontSize: 14, marginBottom: 22 }}>
                <Route size={15} strokeWidth={1.8} />
                {dest.route.map((stop, j) => (
                  <span key={`${stop.city}-${j}`} title={stop.why || undefined} style={{ whiteSpace: 'nowrap' }}>
                    <span style={{ color: 'var(--ink-soft)', fontWeight: 600 }}>
                      {(stop.city || '').split(',')[0].trim()}
                    </span>
                    {' '}{stop.nights}n{j < dest.route.length - 1 ? '  →' : ''}
                  </span>
                ))}
              </p>
            ) : dest.suggested_route ? (
              <p className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, marginBottom: 22 }}>
                <Route size={15} strokeWidth={1.8} /> {dest.suggested_route}
              </p>
            ) : null}

            {/* Love the route but missing a city? Add a stop before the vote closes.
                Hidden when the trip's night count can't fit another one. */}
            {!winner && Array.isArray(dest.route) && dest.route.length > 1 && (() => {
              const nights = dest.route.reduce((s, c) => s + (c.nights || 0), 0);
              const capacity = Math.max(2, Math.min(4, Math.floor(nights / 2)));
              if (dest.route.length >= capacity) return null;
              return addingFor === dest.name ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '-6px 0 22px', maxWidth: 440 }}>
                  <Plus size={16} strokeWidth={2.2} color="var(--pine)" style={{ flexShrink: 0 }} />
                  <CityInput placeholder="Which city belongs on this route?" value={stopQuery}
                             onChange={setStopQuery}
                             onPick={s => addStop(dest.name, s.label)}
                             onCommit={v => addStop(dest.name, v)} />
                  <button type="button" className="btn-quiet" title="Never mind"
                          onClick={() => { setAddingFor(null); setStopQuery(''); setStopErr(null); }}>
                    <X size={14} />
                  </button>
                </div>
              ) : (
                <button type="button" className="btn-quiet" style={{ fontSize: 13, margin: '-6px 0 22px' }}
                        onClick={() => { setAddingFor(dest.name); setStopQuery(''); setStopErr(null); }}>
                  <Plus size={13} strokeWidth={2.4} /> Add a stop to this route
                </button>
              );
            })()}
            {stopErr?.name === dest.name && (
              <p style={{ color: 'var(--clay)', fontSize: 13.5, margin: '-12px 0 20px' }}>{stopErr.message}</p>
            )}

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 26 }}>
              {dest.links?.stays?.airbnb && (
                <a className="ext-link" href={dest.links.stays.airbnb} target="_blank" rel="noreferrer">
                  <BedDouble size={14} /> Airbnb
                </a>
              )}
              {dest.links?.stays?.booking && (
                <a className="ext-link" href={dest.links.stays.booking} target="_blank" rel="noreferrer">
                  <BedDouble size={14} /> Booking.com
                </a>
              )}
              {Object.entries(dest.links?.flights || {}).map(([city, url]) => (
                <a key={city} className="ext-link" href={url} target="_blank" rel="noreferrer">
                  <PlaneTakeoff size={14} /> From {city}
                </a>
              ))}
              {dest.links?.activities?.getyourguide && (
                <a className="ext-link" href={dest.links.activities.getyourguide} target="_blank" rel="noreferrer">
                  <Ticket size={14} /> Tours
                </a>
              )}
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
              {!winner ? (
                <button className={mine ? 'btn' : 'btn-quiet'} onClick={() => vote(dest.name)}>
                  {mine ? <><Check size={15} strokeWidth={2.4} /> Your vote</> : <><VoteIcon size={15} /> Vote {dest.name}</>}
                </button>
              ) : null}
              {!solo && (
                <span className="muted" style={{ fontSize: 13, fontStyle: 'italic' }}>
                  {votes} vote{votes === 1 ? '' : 's'}
                </span>
              )}
            </div>
          </motion.article>
        );
      })}

      {/* Nothing on the ballot clicking? Rovyn will scout another batch. */}
      {!winner && (
        <div style={{ padding: '38px 0 8px', borderTop: '1px solid var(--line)' }}>
          {moreBusy || trip.recommendations_generating ? (
            <ThinkingLoader messages={[
              'Rovyn is out scouting again…',
              'Skipping what you already saw…',
              'Matching budgets, dates and vibes…',
              'Rovyn is thinking…',
            ]} />
          ) : (
            <>
              <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 17, marginBottom: 14 }}>
                {solo ? 'None of these calling to you?' : 'Nothing here the group loves?'}
              </p>
              <button className="btn-quiet" onClick={more}>
                <Sparkles size={15} /> Ask Rovyn for five more ideas
              </button>
            </>
          )}
        </div>
      )}
    </Page>
  );
}
