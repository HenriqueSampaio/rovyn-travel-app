import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowRight, BedDouble, Check, Compass, Landmark,
  MapPin, PlaneTakeoff, Sparkles, Star, Ticket, Users,
} from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText, JourneyTracker, Hero, Thumb, ThinkingLoader } from '../ui.jsx';

const TABS = [
  { key: 'overview', label: 'Overview', Icon: Compass },
  { key: 'flights', label: 'Flights', Icon: PlaneTakeoff },
  { key: 'stay', label: 'Stay', Icon: BedDouble },
  { key: 'visits', label: 'Visits', Icon: Landmark },
  { key: 'events', label: 'Events', Icon: Ticket },
];

const CATEGORY_INTRO = {
  flights: 'These flights are yours alone — they leave from your city. Star the ones that work for you; the most-starred flight among travelers from your city makes the plan, and your check reflects your own flight.',
  stay: 'Star every place you\'d be happy sleeping in. The most-starred stay wins, and Rovyn explains the vote.',
  visits: 'Star every sight you don\'t want to miss — Rovyn builds the day-by-day itinerary from the group\'s picks.',
  events: 'Star the experiences you\'d actually show up for. Starred events get an evening (or their scheduled slot) in the plan.',
};

const PACE_LABEL = { very: 'very busy', moderately: 'moderately busy', slightly: 'slightly busy' };

function StarButton({ starred, count, onClick, disabled }) {
  return (
    <button
      onClick={onClick}
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

function CategoryFooter({ status, onDone, onNoPref, onReset, busy }) {
  if (status) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginTop: 34 }}>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--pine)', fontWeight: 700, fontSize: 15 }}>
          <Check size={16} strokeWidth={2.6} />
          {status === 'done' ? 'Your picks are locked in' : 'You said you don\'t mind'}
        </span>
        <button className="btn-quiet" onClick={onReset} disabled={busy}>Change my mind</button>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 22, marginTop: 34, flexWrap: 'wrap' }}>
      <button className="btn" onClick={onDone} disabled={busy}>
        Lock in my picks <Check size={15} strokeWidth={2.5} />
      </button>
      <button className="btn-quiet" onClick={onNoPref} disabled={busy}>
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
  const [tab, setTab] = useState('overview');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const pollRef = useRef(null);
  const starsInitialized = useRef(false);

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

  if (!trip || !board?.options) return <Page><Loading text="Laying out the planning board…" /></Page>;

  const options = board.options;
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

  async function setStatus(category, status) {
    setBusy(true);
    setError('');
    try {
      const res = await api.setCuration(tripId, category, status);
      setTrip(t => ({ ...t, me: res.me, all_curated: res.all_curated }));
      if (status === 'no_preference') {
        // server clears this category's stars
        const ids = category === 'flights'
          ? Object.values(options.flights || {}).flat().map(o => o.id)
          : (options[category] || []).map(o => o.id);
        setMyStars(prev => new Set([...prev].filter(id => !ids.includes(id))));
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
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

  const doneCount = TABS.slice(1).filter(t => catStatus(t.key)).length;

  function renderStars(category, list, { disabled } = {}) {
    return list.map(opt => {
      const buddies = (board.star_names?.[opt.id] || []).filter(n => n !== me?.name);
      return (
      <div key={opt.id} style={{ display: 'flex', gap: 16, padding: '22px 0', borderTop: '1px solid var(--line-soft)', alignItems: 'flex-start' }}>
        {category !== 'flights' && <Thumb image={opt.image} size={category === 'stay' ? 96 : 76} />}
        <div style={{ flex: 1 }}>
          {category === 'flights' ? (
            <>
              <p style={{ fontWeight: 700, fontSize: 16 }}>{opt.airline}
                <span className="muted" style={{ fontWeight: 400, marginLeft: 10, fontSize: 14 }}>{opt.route}</span>
              </p>
              <p className="muted" style={{ fontSize: 14, margin: '4px 0' }}>
                Out {opt.depart} · back {opt.return} · {opt.stops === 0 ? 'nonstop' : `${opt.stops} stop${opt.stops > 1 ? 's' : ''}`} · {opt.duration}
              </p>
              <p style={{ fontSize: 14, color: 'var(--ink-soft)' }}>{opt.why}</p>
              <p style={{ marginTop: 6, display: 'flex', alignItems: 'baseline', gap: 14 }}>
                <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${opt.est_price_per_person}</span>
                <span className="muted" style={{ fontSize: 13 }}>est. round trip</span>
                {opt.link && <a className="btn-quiet" href={opt.link} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>compare on Expedia</a>}
              </p>
              {buddies.length > 0 && (
                <p style={{ marginTop: 8, display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 13.5, fontWeight: 600, color: 'var(--clay)' }}>
                  <Star size={13} fill="var(--clay)" />
                  {buddies[0]}{buddies.length > 1 ? ` +${buddies.length - 1} more` : ''} selected this flight!
                </p>
              )}
            </>
          ) : category === 'stay' ? (
            <>
              <p style={{ fontWeight: 700, fontSize: 17 }}>
                {opt.name}
                {opt.live_price && (
                  <span title="Real property with live availability for your dates" style={{
                    marginLeft: 10, fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
                    color: 'var(--pine)', border: '1px solid var(--pine)', borderRadius: 999,
                    padding: '2px 8px', verticalAlign: 2,
                  }}>
                    LIVE PRICE
                  </span>
                )}
              </p>
              <p className="muted" style={{ fontSize: 14, margin: '2px 0 6px' }}>
                {opt.type} · {opt.area} {opt.rating ? <>· <Star size={12} fill="var(--clay)" color="var(--clay)" style={{ verticalAlign: -1 }} /> {opt.rating}</> : null}
              </p>
              <p style={{ fontSize: 14.5, color: 'var(--ink-soft)' }}>{opt.why}</p>
              {(opt.amenities || []).length > 0 && (
                <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>{opt.amenities.join(' · ')}</p>
              )}
              <p style={{ marginTop: 8, display: 'flex', alignItems: 'baseline', gap: 14 }}>
                <span className="serif" style={{ fontSize: 24, color: 'var(--pine)' }}>${opt.price_per_night}<span style={{ fontSize: 14 }}> / night</span></span>
                <span className="muted" style={{ fontSize: 13 }}>≈ ${opt.total_per_person} per person total</span>
                {opt.official_website && <a className="btn-quiet" href={opt.official_website} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>official site</a>}
                {opt.links?.booking && <a className="btn-quiet" href={opt.links.booking} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>{opt.live_price ? 'compare' : 'see similar'}</a>}
              </p>
            </>
          ) : (
            <>
              <p style={{ fontWeight: 700, fontSize: 16 }}>{opt.name}</p>
              <p style={{ fontSize: 14.5, color: 'var(--ink-soft)', margin: '3px 0' }}>{opt.description}</p>
              <p className="muted" style={{ fontSize: 13 }}>
                {opt.duration ? `${opt.duration} · ` : ''}{opt.date_hint ? `${opt.date_hint} · ` : ''}
                {opt.cost_per_person > 0 ? `$${opt.cost_per_person} per person` : 'free'}
              </p>
            </>
          )}
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
    return (
      <>
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

  function renderOverview() {
    return (
      <>
        <section style={{ marginBottom: 52 }}>
          <p className="eyebrow" style={{ marginBottom: 14 }}>How this works</p>
          {[
            ['Browse each tab', 'Flights (yours are personal, from your city), stay, visits and events — all generated around the group\'s answers.'],
            ['Star what you love', 'As many options as you like — the Visits and Events tabs suggest how many to pick for your pace. Stars are votes; change them anytime before the final plan.'],
            ['Finish every section', 'Lock in your picks — or tap “I don\'t mind” when you have no strong opinion.'],
            [`When all ${trip.total_members} are done`, 'Rovyn builds the day-by-day itinerary from the group\'s starred picks. Majority wins, every decision comes with its vote count, and every slot stays editable until the group approves.'],
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
          {TABS.slice(1).map(({ key, label, Icon }) => {
            const st = catStatus(key);
            return (
              <button key={key} className="opt-row" onClick={() => setTab(key)}>
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
                  Everyone has finished starring. Rovyn will merge the group's votes into one plan —
                  majority wins, every call explained.
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
              Unlocks when all {trip.total_members} travelers finish all {trip.categories_total} sections.
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

      <JourneyTracker status={trip.status} style={{ marginTop: 26 }} />

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 26, borderBottom: '1px solid var(--line)', marginBottom: 34, flexWrap: 'wrap' }}>
        {TABS.map(({ key, label, Icon }) => {
          const active = tab === key;
          const done = key !== 'overview' && catStatus(key);
          return (
            <button
              key={key}
              onClick={() => setTab(key)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 7,
                padding: '10px 2px 12px', fontSize: 14.5, fontWeight: 700,
                color: active ? 'var(--ink)' : 'var(--muted)',
                borderBottom: active ? '2px solid var(--pine)' : '2px solid transparent',
                marginBottom: -1,
              }}
            >
              <Icon size={15} strokeWidth={2} />
              {label}
              {done && <Check size={13} strokeWidth={3} color="var(--pine)" />}
            </button>
          );
        })}
      </div>

      <ErrorText>{error}</ErrorText>

      <motion.div key={tab} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3 }}>
        {tab === 'overview' && renderOverview()}
        {tab !== 'overview' && (
          <>
            <p className="muted" style={{ maxWidth: 560, marginBottom: 10 }}>{CATEGORY_INTRO[tab]}</p>
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
            {tab === 'flights' ? renderFlights() : renderStars(tab, options[tab] || [])}
            <CategoryFooter
              status={catStatus(tab)}
              busy={busy}
              onDone={() => setStatus(tab, 'done')}
              onNoPref={() => setStatus(tab, 'no_preference')}
              onReset={() => setStatus(tab, 'reset')}
            />
          </>
        )}
      </motion.div>
    </Page>
  );
}
