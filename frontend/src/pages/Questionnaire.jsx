import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {
  ArrowLeft, ArrowRight, Backpack, Building2, Check, Coffee, Gauge, Home, Hotel,
  Landmark, MapPin, Mountain, Music, Palmtree, PlaneLanding, PlaneTakeoff, Plus,
  ShoppingBag, Shuffle, Star, TrainFront, TreePine, UtensilsCrossed, Waves, Flower2, X, Zap,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, ErrorText, CityInput, ThinkingLoader } from '../ui.jsx';

const VIBES = [
  { id: 'culture', label: 'Culture & history', Icon: Landmark },
  { id: 'nightlife', label: 'Nightlife & parties', Icon: Music },
  { id: 'nature', label: 'Nature expeditions', Icon: TreePine },
  { id: 'food', label: 'Food & drink', Icon: UtensilsCrossed },
  { id: 'beach', label: 'Beach & sun', Icon: Waves },
  { id: 'adventure', label: 'Adventure & sports', Icon: Mountain },
  { id: 'relaxation', label: 'Relaxation & wellness', Icon: Flower2 },
  { id: 'shopping', label: 'Shopping', Icon: ShoppingBag },
];

const TRIP_STYLES = [
  { id: 'single-city', label: 'One home base', desc: 'Settle into a single city, day trips at most', Icon: Building2 },
  { id: 'multi-city', label: 'Multi-city explorer', desc: 'Move between cities within the country', Icon: TrainFront },
  { id: 'no-preference', label: 'No preference', desc: 'Happy either way', Icon: Shuffle },
];

const ACCOMMODATIONS = [
  { id: 'hostel', label: 'Hostel', desc: 'Budget-friendly and social', Icon: Backpack },
  { id: 'boutique hotel', label: 'Boutique hotel', desc: 'Charming and unique', Icon: Hotel },
  { id: '4-star hotel', label: 'Four-star hotel', desc: 'Comfortable and reliable', Icon: Building2 },
  { id: 'luxury resort', label: 'Luxury resort', desc: 'Premium, all-inclusive', Icon: Palmtree },
  { id: 'airbnb/vacation rental', label: 'Airbnb or villa', desc: 'Home-like and spacious', Icon: Home },
];

const PACES = [
  { id: 'very', label: 'Very busy', desc: 'Pack the days — see and do everything', Icon: Zap },
  { id: 'moderately', label: 'Moderately busy', desc: 'A couple of anchors a day, room to breathe', Icon: Gauge },
  { id: 'slightly', label: 'Slightly busy', desc: 'One thing a day, the rest is vacation', Icon: Coffee },
];

const TOTAL = 8;

/* ---------- group availability heatmap (When2meet-style) ---------- */

function isoOf(year, month, day) {
  return `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function heatColor(count, submitted) {
  if (!count) return 'transparent';
  const ratio = submitted ? count / submitted : 0;
  return `rgba(46, 93, 79, ${(0.14 + 0.76 * ratio).toFixed(3)})`;
}

function MonthGrid({ year, month, todayIso, votes, start, end, onPick }) {
  const first = new Date(year, month, 1);
  const label = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells = [
    ...Array(first.getDay()).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  const submitted = votes.submitted || 0;
  return (
    <div>
      <p className="serif" style={{ fontSize: 16, fontStyle: 'italic', marginBottom: 8 }}>{label}</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 3 }}>
        {['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((d, i) => (
          <span key={`${d}${i}`} className="muted" style={{ fontSize: 10.5, fontWeight: 700, textAlign: 'center' }}>{d}</span>
        ))}
        {cells.map((day, i) => {
          if (!day) return <span key={`blank-${i}`} />;
          const iso = isoOf(year, month, day);
          const count = votes.counts?.[iso] || 0;
          const ratio = submitted ? count / submitted : 0;
          const past = iso < todayIso;
          const mine = start && (end ? iso >= start && iso <= end : iso === start);
          const who = votes.names?.[iso];
          return (
            <button
              key={iso}
              type="button"
              disabled={past}
              onClick={() => onPick(iso)}
              title={who?.length ? `${count} of ${submitted} — ${who.join(', ')}` : undefined}
              style={{
                aspectRatio: '1', minWidth: 0, borderRadius: 3, fontSize: 12.5, fontWeight: 600,
                background: past ? 'transparent' : heatColor(count, submitted),
                color: past ? 'var(--line)' : ratio > 0.55 ? '#F6F2E8' : 'var(--ink-soft)',
                boxShadow: mine
                  ? 'inset 0 0 0 2px var(--clay)'
                  : !past && !count ? 'inset 0 0 0 1px var(--line-soft)' : 'none',
                cursor: past ? 'default' : 'pointer',
              }}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DateHeatmap({ votes, start, end, onPick }) {
  const now = new Date();
  const todayIso = isoOf(now.getFullYear(), now.getMonth(), now.getDate());
  // Open on the first month with group picks still ahead of us
  const [page, setPage] = useState(() => {
    const upcoming = Object.keys(votes.counts || {}).filter(d => d >= todayIso).sort()[0];
    if (!upcoming) return 0;
    const [y, m] = upcoming.split('-').map(Number);
    return Math.max(0, (y - now.getFullYear()) * 12 + (m - 1) - now.getMonth());
  });
  const months = [0, 1].map(offset => {
    const d = new Date(now.getFullYear(), now.getMonth() + page + offset, 1);
    return { year: d.getFullYear(), month: d.getMonth() };
  });
  const submitted = votes.submitted || 0;
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
        <button className="btn-quiet" disabled={page <= 0} onClick={() => setPage(p => Math.max(0, p - 1))} aria-label="Earlier months">
          <ArrowLeft size={14} /> earlier
        </button>
        <button className="btn-quiet" disabled={page >= 22} onClick={() => setPage(p => Math.min(22, p + 1))} aria-label="Later months">
          later <ArrowRight size={14} />
        </button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 28 }}>
        {months.map(m => (
          <MonthGrid key={`${m.year}-${m.month}`} {...m} todayIso={todayIso}
                     votes={votes} start={start} end={end} onPick={onPick} />
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
        {submitted > 0 && (
          <>
            <span className="muted" style={{ fontSize: 12 }}>1 traveler</span>
            {Array.from({ length: Math.min(submitted, 5) }, (_, i) => (
              <span key={i} style={{
                width: 14, height: 14, borderRadius: 3,
                background: heatColor(i + 1, Math.min(submitted, 5)),
              }} />
            ))}
            <span className="muted" style={{ fontSize: 12 }}>all {submitted}</span>
            <span style={{ width: 18 }} />
          </>
        )}
        <span style={{ width: 14, height: 14, borderRadius: 3, boxShadow: 'inset 0 0 0 2px var(--clay)' }} />
        <span className="muted" style={{ fontSize: 12 }}>your window</span>
      </div>
    </div>
  );
}

function OptionRow({ selected, onClick, Icon, title, desc }) {
  return (
    <button className={`opt-row${selected ? ' selected' : ''}`} onClick={onClick} type="button">
      <Icon size={20} strokeWidth={1.7} color={selected ? 'var(--pine)' : 'var(--muted)'} />
      <span style={{ flex: 1 }}>
        <span className="opt-title" style={{ display: 'block' }}>{title}</span>
        {desc && <span className="opt-desc">{desc}</span>}
      </span>
      {selected && <Check size={17} strokeWidth={2.4} color="var(--pine)" />}
    </button>
  );
}

function Budget({ label, value, onChange, min, max }) {
  return (
    <div style={{ marginBottom: 30 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span className="eyebrow" style={{ fontSize: 11 }}>{label}</span>
        <span className="serif" style={{ fontSize: 30, color: 'var(--pine)' }}>${value.toLocaleString()}</span>
      </div>
      <input type="range" min={min} max={max} step={100} value={value}
             onChange={e => onChange(Number(e.target.value))} />
    </div>
  );
}

export default function Questionnaire() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const today = new Date().toISOString().split('T')[0];

  const [step, setStep] = useState(1);
  const [dir, setDir] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [dateVotes, setDateVotes] = useState(null);
  const [solo, setSolo] = useState(false); // solo trips drop the group framing
  const [cityIdeas, setCityIdeas] = useState(null); // suggestions + group picks for the multi-city panel
  const [cityQuery, setCityQuery] = useState('');
  const ideasRequested = useRef(false);
  const [form, setForm] = useState({
    dateStart: '', dateEnd: '', departureCity: '', returnElsewhere: false, returnCity: '',
    budgetMin: 500, budgetMax: 2000,
    tripStyle: '', multiCityPicks: [], vibes: [], accommodation: '', pace: '', specialRequirements: '',
  });

  useEffect(() => {
    api.getDateVotes(tripId)
      .then(setDateVotes)
      .catch(() => setDateVotes({ counts: {}, names: {}, submitted: 0, total_members: 0 }));
    api.tripStatus(tripId)
      .then(d => setSolo(Boolean(d.solo)))
      .catch(() => {});
  }, [tripId]);

  // Nearby-city ideas load the first time multi-city is chosen (the backend
  // caches the AI suggestions on the trip, so this is cheap after the first ask)
  useEffect(() => {
    if (form.tripStyle !== 'multi-city' || ideasRequested.current) return;
    ideasRequested.current = true;
    api.cityIdeas(tripId)
      .then(setCityIdeas)
      .catch(() => setCityIdeas({ anchor: '', suggestions: [], group_picks: [] }));
  }, [form.tripStyle, tripId]);

  const sameTown = (a, b) =>
    (a || '').split(',')[0].trim().toLowerCase() === (b || '').split(',')[0].trim().toLowerCase();

  function addCityPick(label) {
    const city = (label || '').trim();
    if (!city) return;
    setForm(f => sameTown(city, f.departureCity) || f.multiCityPicks.some(p => sameTown(p, city))
      ? f
      : { ...f, multiCityPicks: [...f.multiCityPicks, city] });
    setCityQuery('');
  }

  function removeCityPick(city) {
    setForm(f => ({ ...f, multiCityPicks: f.multiCityPicks.filter(p => p !== city) }));
  }

  // Tap a calendar day: first tap opens your window, second closes it,
  // tapping earlier than the start restarts.
  function pickDate(iso) {
    setError('');
    setForm(f => {
      if (!f.dateStart || f.dateEnd) return { ...f, dateStart: iso, dateEnd: '' };
      if (iso <= f.dateStart) return { ...f, dateStart: iso, dateEnd: '' };
      return { ...f, dateEnd: iso };
    });
  }

  function toggleVibe(id) {
    setForm(f => ({
      ...f,
      vibes: f.vibes.includes(id) ? f.vibes.filter(v => v !== id) : [...f.vibes, id],
    }));
  }

  function validate() {
    if (step === 1 && (!form.dateStart || !form.dateEnd)) return 'Pick both dates.';
    if (step === 1 && form.dateStart >= form.dateEnd) return 'The return date must come after the departure.';
    if (step === 2 && !form.departureCity.trim()) return 'Tell us where you fly from.';
    if (step === 4 && !form.tripStyle) return 'Pick a travel style — "no preference" counts.';
    if (step === 5 && form.vibes.length === 0) return 'Choose at least one.';
    if (step === 6 && !form.accommodation) return 'Choose where you would like to stay.';
    if (step === 7 && !form.pace) return 'Pick a pace — it shapes how full your days are.';
    return null;
  }

  function go(delta) {
    if (delta > 0) {
      const err = validate();
      if (err) { setError(err); return; }
    }
    setError('');
    setDir(delta);
    setStep(s => s + delta);
  }

  async function submit() {
    setLoading(true);
    setError('');
    try {
      await api.submitQuestionnaire(tripId, {
        budget_min: form.budgetMin,
        budget_max: form.budgetMax,
        travel_dates_start: form.dateStart,
        travel_dates_end: form.dateEnd,
        departure_city: form.departureCity.trim(),
        return_city: form.returnElsewhere ? form.returnCity.trim() : '',
        trip_style: form.tripStyle || 'no-preference',
        multi_city_picks: form.tripStyle === 'multi-city' ? form.multiCityPicks : [],
        vibes: form.vibes,
        accommodation_type: form.accommodation,
        pace: form.pace || 'moderately',
        special_requirements: form.specialRequirements,
      });
      navigate(`/trips/${tripId}`);
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  }

  const steps = {
    1: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>When could you get away?</h1>
        <p className="muted" style={{ marginBottom: 40 }}>
          {solo ? 'Pick the window that suits you.' : "We'll find the window everyone can make."}
        </p>
        <div className="two-col" style={{ columnGap: 40 }}>
          <Field label="From">
            <input type="date" min={today} value={form.dateStart}
                   onChange={e => setForm(f => ({ ...f, dateStart: e.target.value }))} />
          </Field>
          <Field label="Until">
            <input type="date" min={form.dateStart || today} value={form.dateEnd}
                   onChange={e => setForm(f => ({ ...f, dateEnd: e.target.value }))} />
          </Field>
        </div>
        <div style={{ marginTop: 8 }}>
          <p className="eyebrow" style={{ fontSize: 11, marginBottom: 6 }}>
            {solo ? 'Pick your days' : 'When the group can go'}
          </p>
          {dateVotes ? (
            <>
              <p className="muted" style={{ fontSize: 13.5, marginBottom: 14, maxWidth: 520 }}>
                {solo
                  ? 'Tap a day to start your window, tap another to close it.'
                  : dateVotes.submitted > 0
                    ? `${dateVotes.submitted} of ${dateVotes.total_members} travelers have picked dates — the greener a day, the more of you can make it. Tap a day to start your window, tap another to close it.`
                    : 'No one has picked dates yet — you set the tone. Tap a day to start your window, tap another to close it.'}
              </p>
              <DateHeatmap votes={dateVotes} start={form.dateStart} end={form.dateEnd} onPick={pickDate} />
            </>
          ) : (
            <p className="muted" style={{ fontSize: 13.5, fontStyle: 'italic' }}>Loading the group's calendar…</p>
          )}
        </div>
      </>
    ),
    2: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>Where do you fly from?</h1>
        <p className="muted" style={{ marginBottom: 40 }}>
          Rovyn plans your whole round trip — out from home and back again.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <PlaneTakeoff size={22} strokeWidth={1.6} color="var(--pine)" />
          <CityInput placeholder="New York, United States" value={form.departureCity}
                     onChange={v => setForm(f => ({ ...f, departureCity: v }))} />
        </div>
        <div style={{ marginTop: 34 }}>
          {form.returnElsewhere ? (
            <>
              <p className="eyebrow" style={{ fontSize: 11, marginBottom: 6 }}>And fly back to</p>
              <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                <PlaneLanding size={22} strokeWidth={1.6} color="var(--clay)" />
                <CityInput placeholder="Where your return flight lands" value={form.returnCity}
                           onChange={v => setForm(f => ({ ...f, returnCity: v }))} />
                <button className="btn-quiet" title="Never mind — return home"
                        onClick={() => setForm(f => ({ ...f, returnElsewhere: false, returnCity: '' }))}>
                  <X size={14} />
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="muted" style={{ fontSize: 13.5 }}>
                Your round trip ends where it starts{form.departureCity.trim() ? ` — back to ${form.departureCity.trim()}` : ''}.
              </p>
              <button className="btn-quiet" style={{ marginTop: 6 }}
                      onClick={() => setForm(f => ({ ...f, returnElsewhere: true }))}>
                <PlaneLanding size={14} /> I'm flying back somewhere else
              </button>
            </>
          )}
        </div>
      </>
    ),
    3: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>What feels comfortable to spend?</h1>
        <p className="muted" style={{ marginBottom: 40 }}>
          {solo ? 'For your whole trip — flights aside.' : 'Per person, for the whole trip — flights aside.'}
        </p>
        <Budget label="No less than" value={form.budgetMin} min={100} max={10000}
                onChange={v => setForm(f => ({ ...f, budgetMin: Math.min(v, f.budgetMax - 100) }))} />
        <Budget label="Up to" value={form.budgetMax} min={200} max={15000}
                onChange={v => setForm(f => ({ ...f, budgetMax: Math.max(v, f.budgetMin + 100) }))} />
      </>
    ),
    4: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>One base, or on the move?</h1>
        <p className="muted" style={{ marginBottom: 28 }}>Would you rather settle in or explore several cities?</p>
        {TRIP_STYLES.map(t => (
          <OptionRow key={t.id} selected={form.tripStyle === t.id} Icon={t.Icon}
                     title={t.label} desc={t.desc}
                     onClick={() => setForm(f => ({ ...f, tripStyle: t.id }))} />
        ))}
        <AnimatePresence initial={false}>
          {form.tripStyle === 'multi-city' && (
            <motion.div
              key="multi-city-picker"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.3 }}
              style={{ overflow: 'hidden' }}
            >
              <div style={{ paddingTop: 26 }}>
                <p className="eyebrow" style={{ fontSize: 11, marginBottom: 4 }}>Cities on your wishlist</p>
                <p className="muted" style={{ fontSize: 13.5, marginBottom: 14, maxWidth: 520 }}>
                  Add the places you'd love the route to include — optional. Rovyn weighs
                  {solo ? ' your picks' : ' the whole group\'s picks'} when it draws the route, the
                  travel legs and the stays.
                </p>

                {form.multiCityPicks.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 16 }}>
                    {form.multiCityPicks.map(c => (
                      <button key={c} type="button" className="ext-link" onClick={() => removeCityPick(c)}
                              title="Remove" style={{ padding: '7px 14px', fontSize: 13.5 }}>
                        <MapPin size={12} /> {c} <X size={12} />
                      </button>
                    ))}
                  </div>
                )}

                <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 22 }}>
                  <Plus size={18} strokeWidth={2} color="var(--pine)" />
                  <CityInput placeholder="Add a city — start typing…" value={cityQuery}
                             onChange={setCityQuery}
                             onPick={s => addCityPick(s.label)}
                             onCommit={addCityPick} />
                </div>

                {cityIdeas === null ? (
                  <ThinkingLoader messages={[
                    'Scouting cities nearby…',
                    'Checking what pairs well…',
                    'Rovyn is thinking…',
                  ]} />
                ) : (
                  <>
                    {(() => {
                      const pickedByMe = c => form.multiCityPicks.some(p => sameTown(p, c));
                      const groupOnly = (cityIdeas.group_picks || []).filter(g =>
                        !pickedByMe(g.city) && !(g.names || []).includes('You'));
                      const pickInfo = label => (cityIdeas.group_picks || []).find(g => sameTown(g.city, label));
                      const suggestions = (cityIdeas.suggestions || []).filter(s => !pickedByMe(s.label || s.city));
                      return (
                        <>
                          {groupOnly.length > 0 && (
                            <div style={{ marginBottom: 20 }}>
                              <p className="eyebrow" style={{ fontSize: 11, marginBottom: 2 }}>
                                Your group is eyeing
                              </p>
                              {groupOnly.map(g => (
                                <button key={g.city} type="button" className="opt-row"
                                        onClick={() => addCityPick(g.city)}>
                                  <Star size={16} strokeWidth={1.8} fill="var(--clay)" color="var(--clay)" />
                                  <span style={{ flex: 1 }}>
                                    <span className="opt-title" style={{ display: 'block', fontSize: 15.5 }}>{g.city}</span>
                                    <span className="opt-desc">{g.names.join(', ')} want{g.names.length === 1 ? 's' : ''} this stop</span>
                                  </span>
                                  <Plus size={15} color="var(--pine)" />
                                </button>
                              ))}
                            </div>
                          )}
                          {suggestions.length > 0 && (
                            <div>
                              <p className="eyebrow" style={{ fontSize: 11, marginBottom: 2 }}>
                                {cityIdeas.anchor ? `Pairs well with ${cityIdeas.anchor.split(',')[0]}` : 'Ideas to consider'}
                              </p>
                              {suggestions.map(s => {
                                const info = pickInfo(s.label || s.city);
                                const others = (info?.names || []).filter(n => n !== 'You');
                                return (
                                  <button key={s.label || s.city} type="button" className="opt-row"
                                          onClick={() => addCityPick(s.label || s.city)}>
                                    <MapPin size={16} strokeWidth={1.8} color="var(--muted)" />
                                    <span style={{ flex: 1 }}>
                                      <span className="opt-title" style={{ display: 'block', fontSize: 15.5 }}>
                                        {s.city}{s.country ? <span className="muted" style={{ fontWeight: 400 }}>, {s.country}</span> : null}
                                      </span>
                                      <span className="opt-desc">
                                        {[s.travel_hint, s.why].filter(Boolean).join(' · ')}
                                        {others.length > 0 && (
                                          <span style={{ color: 'var(--clay)', fontWeight: 600 }}>
                                            {s.travel_hint || s.why ? ' · ' : ''}{others.join(', ')} also want{others.length === 1 ? 's' : ''} this
                                          </span>
                                        )}
                                      </span>
                                    </span>
                                    <Plus size={15} color="var(--pine)" />
                                  </button>
                                );
                              })}
                            </div>
                          )}
                          {suggestions.length === 0 && groupOnly.length === 0 && (
                            <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15 }}>
                              {cityIdeas.anchor
                                ? 'No more ideas to offer — add any city with the search above.'
                                : `No destination is pinned yet, so add dream cities freely — Rovyn will weigh them once ${solo ? 'you pick' : 'the group picks'} where to go.`}
                            </p>
                          )}
                        </>
                      );
                    })()}
                  </>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </>
    ),
    5: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>What are you traveling for?</h1>
        <p className="muted" style={{ marginBottom: 28 }}>Choose everything that calls to you.</p>
        <div className="two-col" style={{ columnGap: 40 }}>
          {VIBES.map(v => (
            <OptionRow key={v.id} selected={form.vibes.includes(v.id)} Icon={v.Icon}
                       title={v.label} onClick={() => toggleVibe(v.id)} />
          ))}
        </div>
      </>
    ),
    6: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>Where will you rest?</h1>
        <p className="muted" style={{ marginBottom: 28 }}>Your preferred kind of stay.</p>
        {ACCOMMODATIONS.map(a => (
          <OptionRow key={a.id} selected={form.accommodation === a.id} Icon={a.Icon}
                     title={a.label} desc={a.desc}
                     onClick={() => setForm(f => ({ ...f, accommodation: a.id }))} />
        ))}
      </>
    ),
    7: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>How busy do you want to be?</h1>
        <p className="muted" style={{ marginBottom: 28 }}>
          This sets the rhythm of your days — and how many activities Rovyn will suggest you pick.
        </p>
        {PACES.map(p => (
          <OptionRow key={p.id} selected={form.pace === p.id} Icon={p.Icon}
                     title={p.label} desc={p.desc}
                     onClick={() => setForm(f => ({ ...f, pace: p.id }))} />
        ))}
      </>
    ),
    8: (
      <>
        <h1 style={{ fontSize: 36, marginBottom: 10 }}>
          {solo ? 'Anything Rovyn should know?' : 'Anything the group should know?'}
        </h1>
        <p className="muted" style={{ marginBottom: 40 }}>Dietary needs, accessibility, anything that matters. Optional.</p>
        <textarea
          placeholder="Vegetarian, traveling with a toddler, need visa-free destinations…"
          value={form.specialRequirements}
          onChange={e => setForm(f => ({ ...f, specialRequirements: e.target.value }))}
          style={{ minHeight: 110, resize: 'vertical' }}
        />
      </>
    ),
  };

  return (
    <Page>
      {/* progress */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginBottom: 56 }}>
        <span className="serif" style={{ fontSize: 15, fontStyle: 'italic', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
          {step} of {TOTAL}
        </span>
        <div style={{ flex: 1, height: 1, background: 'var(--line-soft)' }}>
          <motion.div
            animate={{ width: `${(step / TOTAL) * 100}%` }}
            transition={{ duration: 0.45, ease: 'easeOut' }}
            style={{ height: 2, marginTop: -0.5, background: 'var(--pine)' }}
          />
        </div>
      </div>

      <div style={{ minHeight: 380, position: 'relative' }}>
        <AnimatePresence mode="wait" custom={dir}>
          <motion.div
            key={step}
            custom={dir}
            initial={{ opacity: 0, x: dir * 46 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: dir * -46 }}
            transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
          >
            {steps[step]}
          </motion.div>
        </AnimatePresence>
      </div>

      <ErrorText>{error}</ErrorText>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 44 }}>
        {step > 1 ? (
          <button className="btn-quiet" onClick={() => go(-1)}>
            <ArrowLeft size={15} /> Back
          </button>
        ) : <span />}
        {step < TOTAL ? (
          <button className="btn" onClick={() => go(1)}>
            Continue <ArrowRight size={16} />
          </button>
        ) : (
          <button className="btn" onClick={submit} disabled={loading}>
            {loading ? 'Sending…' : solo ? 'Save my preferences' : 'Share with the group'} <Check size={16} strokeWidth={2.4} />
          </button>
        )}
      </div>
    </Page>
  );
}
