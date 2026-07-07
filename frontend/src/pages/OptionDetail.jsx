import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, Bus, Check, Clock, Globe, Loader2, MapPin, Phone,
  PlaneTakeoff, Ship, Star, TrainFront,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, ErrorText, Hero, MapImage, PhotoCarousel, GuestReviews } from '../ui.jsx';

const TITLES = {
  stay: 'The stay', flights: 'This flight', transport: 'This leg',
  visits: 'The visit', events: 'The event',
};
const MODE_ICONS = { train: TrainFront, flight: PlaneTakeoff, bus: Bus, ferry: Ship };

/* Mirror of the backend's _find_option — locate one option across categories */
function findOption(options, optionId) {
  for (const cat of ['stay', 'visits', 'events']) {
    for (const o of options[cat] || []) if (o.id === optionId) return [cat, o];
  }
  for (const cat of ['flights', 'transport']) {
    for (const group of Object.values(options[cat] || {})) {
      for (const o of group) if (o.id === optionId) return [cat, o];
    }
  }
  return [null, null];
}

/* "2026-08-14 09:40" → { date: "Thu, Aug 14", time: "09:40" } */
function splitWhen(s = '') {
  const [d, t] = s.split(' ');
  const parsed = d ? new Date(`${d}T${t || '00:00'}`) : null;
  const date = parsed && !isNaN(parsed)
    ? parsed.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    : (d || '');
  return { date, time: t || '' };
}

function LegRow({ label, when, route }) {
  const { date, time } = splitWhen(when);
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, padding: '8px 0', borderTop: '1px dashed var(--line-soft)', flexWrap: 'wrap' }}>
      <span style={{
        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.12em', width: 38, flexShrink: 0,
        color: label === 'OUT' ? 'var(--pine)' : 'var(--clay)',
      }}>
        {label}
      </span>
      <span style={{ fontSize: 16, fontWeight: 700, whiteSpace: 'nowrap' }}>{time}</span>
      <span className="muted" style={{ fontSize: 13.5, whiteSpace: 'nowrap' }}>{date}</span>
      <span style={{ fontSize: 14, color: 'var(--ink-soft)', marginLeft: 'auto' }}>{route}</span>
    </div>
  );
}

function FactRow({ icon: Icon, label, children }) {
  if (!children) return null;
  return (
    <div style={{ display: 'flex', gap: 14, padding: '10px 0', borderTop: '1px dashed var(--line-soft)', fontSize: 14 }}>
      <span className="muted" style={{ width: 110, flexShrink: 0, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 7 }}>
        {Icon && <Icon size={13} strokeWidth={2.2} />}{label}
      </span>
      <span style={{ color: 'var(--ink-soft)', minWidth: 0 }}>{children}</span>
    </div>
  );
}

/* Long real-world text (hotel descriptions) — clamped, full text behind a tap */
function ClampedText({ text, lines = 4 }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const long = text.length > 380 || text.split('\n').length > lines;
  return (
    <div style={{ margin: '4px 0 6px' }}>
      <p style={{
        fontSize: 14.5, color: 'var(--ink-soft)', whiteSpace: 'pre-line',
        ...(open || !long ? {} : {
          display: '-webkit-box', WebkitLineClamp: lines, WebkitBoxOrient: 'vertical', overflow: 'hidden',
        }),
      }}>
        {text}
      </p>
      {long && (
        <button className="btn-quiet" style={{ fontSize: 13, marginTop: 4 }} onClick={() => setOpen(o => !o)}>
          {open ? 'Show less' : 'Read more'}
        </button>
      )}
    </div>
  );
}

/* The Booking-style room table — real offers for the trip's dates */
function RoomOffers({ d }) {
  if (!d?.room_offers?.length) return null;
  const roomsNote = d.rooms_needed > 1 ? `${d.rooms_needed} rooms` : '1 room';
  return (
    <section style={{ marginTop: 18 }}>
      <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 2 }}>
        Rooms & rates · live for your dates
      </p>
      {d.room_offers.map((r, i) => (
        <div key={i} style={{ display: 'flex', gap: 14, alignItems: 'baseline', padding: '10px 0', borderTop: '1px dashed var(--line-soft)', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 180 }}>
            <p style={{ fontWeight: 700, fontSize: 14.5 }}>{r.name}</p>
            <p className="muted" style={{ fontSize: 13, marginTop: 2 }}>
              {[
                r.sleeps ? `sleeps ${r.sleeps}` : null,
                r.board || null,
              ].filter(Boolean).join(' · ')}
              {r.refundable
                ? <span style={{ color: 'var(--pine)', fontWeight: 600 }}>{r.board || r.sleeps ? ' · ' : ''}free cancellation</span>
                : <span>{r.board || r.sleeps ? ' · ' : ''}non-refundable</span>}
            </p>
          </div>
          <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
            <p style={{ fontWeight: 700, fontSize: 15 }}>${r.per_room_per_night}<span className="muted" style={{ fontWeight: 400, fontSize: 12.5 }}> / room / night</span></p>
            <p className="muted" style={{ fontSize: 12.5 }}>${r.total.toLocaleString()} total · {roomsNote}</p>
          </div>
        </div>
      ))}
    </section>
  );
}

/* Real facilities list — first few visible, the rest behind a tap */
function Facilities({ list }) {
  const [all, setAll] = useState(false);
  if (!list?.length) return null;
  const shown = all ? list : list.slice(0, 10);
  return (
    <p className="muted" style={{ fontSize: 13.5, marginTop: 14 }}>
      {shown.join(' · ')}
      {list.length > 10 && (
        <button className="btn-quiet" style={{ fontSize: 13, marginLeft: 10 }} onClick={() => setAll(a => !a)}>
          {all ? 'fewer' : `+ ${list.length - 10} more`}
        </button>
      )}
    </p>
  );
}

function OpeningHours({ hours }) {
  if (!hours?.length) return null;
  return (
    <div style={{ display: 'flex', gap: 14, padding: '10px 0', borderTop: '1px dashed var(--line-soft)', fontSize: 14 }}>
      <span className="muted" style={{ width: 110, flexShrink: 0, fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 7 }}>
        <Clock size={13} strokeWidth={2.2} /> Hours
      </span>
      <div style={{ color: 'var(--ink-soft)' }}>
        {hours.map(h => <p key={h} style={{ fontSize: 13.5, padding: '1px 0' }}>{h}</p>)}
      </div>
    </div>
  );
}

const SOURCE_NOTES = {
  liteapi: 'Live property details and room rates for your exact dates, straight from the hotel’s booking system.',
  places: 'Live venue details via Google — hours and prices can change, so double-check the official site before you go.',
};

export default function OptionDetail() {
  const navigate = useNavigate();
  const location = useLocation();
  const { tripId, optionId } = useParams();
  const seed = location.state || {};
  const [category, setCategory] = useState(seed.category || '');
  const [opt, setOpt] = useState(seed.opt || null);
  const [details, setDetails] = useState(null); // { data } | { err }
  const [media, setMedia] = useState(null);     // stay gallery + guest reviews
  const [isSample, setIsSample] = useState(false);
  const [starred, setStarred] = useState(Boolean(seed.starred));
  const [count, setCount] = useState(seed.count || 0);
  const [error, setError] = useState('');
  const detailsRequested = useRef(false);

  // The board stays the source of truth — a refresh or shared link lands
  // here with no seed, so everything re-derives from the options fetch.
  useEffect(() => {
    let dead = false;
    api.getOptions(tripId).then(res => {
      if (dead || !res.options) return;
      const [cat, o] = findOption(res.options, optionId);
      if (!o) { navigate(`/trips/${tripId}/plan`, { replace: true }); return; }
      setCategory(cat);
      setOpt(o);
      setIsSample(res.options.source === 'sample');
      setCount(res.star_counts?.[o.id] || 0);
      setStarred((res.my_stars || []).includes(o.id));
    }).catch(err => {
      if (err.status === 401) navigate('/');
      else navigate(`/trips/${tripId}/plan`, { replace: true });
    });
    return () => { dead = true; };
  }, [tripId, optionId, navigate]);

  // Live details — flights/transport have no live source, so they render
  // instantly from the board's own facts without a request.
  useEffect(() => {
    if (!category || detailsRequested.current) return;
    detailsRequested.current = true;
    if (category === 'flights' || category === 'transport') {
      setDetails({ data: { source: 'board' } });
      return;
    }
    api.getOptionDetails(tripId, optionId)
      .then(res => setDetails({ data: res.details }))
      .catch(err => setDetails({ err: err.message }));
  }, [category, tripId, optionId]);

  // Stays reuse the board's cached galleries + guest reviews
  useEffect(() => {
    if (category !== 'stay') return;
    api.getStayMedia(tripId)
      .then(res => setMedia(res.media?.[optionId] || null))
      .catch(() => {});
  }, [category, tripId, optionId]);

  async function toggleStar() {
    const next = !starred;
    setStarred(next);
    setCount(c => Math.max(0, c + (next ? 1 : -1)));
    setError('');
    try {
      await api.toggleStar(tripId, category, optionId, next);
    } catch (err) {
      setError(err.message);
      setStarred(!next);
      setCount(c => Math.max(0, c + (next ? -1 : 1)));
    }
  }

  function goBack() {
    // Seeded = we came from the board; history back re-mounts Plan on the
    // same tab (it remembers). A cold deep link goes to the board directly.
    if (seed.opt) navigate(-1);
    else navigate(`/trips/${tripId}/plan`);
  }

  if (!opt) return <Page><Loading text="Opening the details…" /></Page>;

  const d = details?.data;
  const loadingLive = !details && !['flights', 'transport'].includes(category);
  const title = opt.name || opt.airline || opt.carrier || opt.mode || 'Details';
  const city = (opt.city || '').split(',')[0].trim();
  const photos = (category === 'stay' ? media?.photos : d?.photos) || [];
  const ModeIcon = MODE_ICONS[(opt.mode || '').toLowerCase()] || TrainFront;

  const facts = [
    category === 'stay' && opt.type,
    category === 'stay' && opt.area,
    category === 'stay' && opt.rating ? `★ ${opt.rating}` : null,
    category === 'flights' && (opt.stops === 0 ? 'nonstop' : `${opt.stops} stop${opt.stops > 1 ? 's' : ''}`),
    category === 'flights' && opt.duration,
    category === 'transport' && opt.mode,
    category === 'transport' && opt.duration,
    ['visits', 'events'].includes(category) && opt.duration,
    ['visits', 'events'].includes(category) && opt.date_hint,
  ].filter(Boolean);

  const price =
    category === 'stay' ? [`$${opt.price_per_night}`, ' / night', `≈ $${opt.total_per_person} per person total`]
    : category === 'flights' ? [`$${opt.est_price_per_person}`, '', 'est. round trip']
    : category === 'transport' ? [`$${opt.est_price_per_person}`, '', 'per person, one way']
    : opt.cost_per_person > 0 ? [`$${opt.cost_per_person}`, '', 'per person'] : ['Free', '', ''];

  const compareLink = opt.links?.booking || opt.link;
  const compareLabel =
    category === 'flights' ? 'Live fares on Expedia'
    : category === 'transport' ? 'Live times on Rome2Rio'
    : category === 'stay' ? (opt.live_price ? 'Compare prices' : 'See booking options')
    : 'See booking options';
  const website = d?.website || opt.official_website;

  const reviewMedia = category === 'stay'
    ? (media || (d?.reviews?.length ? d : null))
    : (d?.reviews?.length ? d : null);

  return (
    <Page width={700}>
      <button className="btn-quiet" onClick={goBack} style={{ marginBottom: 22 }}>
        <ArrowLeft size={15} /> Back to the board
      </button>

      <p className="eyebrow">{TITLES[category] || 'Details'}{city ? ` · ${city}` : ''}</p>
      <h1 style={{ fontSize: 34, margin: '8px 0 4px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        {category === 'transport' && <ModeIcon size={26} strokeWidth={1.7} color="var(--pine)" />}
        {title}
        {category === 'stay' && opt.live_price && (
          <span title="Real property with live availability for your dates" style={{
            fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
            color: 'var(--pine)', border: '1px solid var(--pine)', borderRadius: 999,
            padding: '3px 9px', whiteSpace: 'nowrap',
          }}>
            LIVE PRICE
          </span>
        )}
      </h1>
      {facts.length > 0 && <p className="muted" style={{ fontSize: 14 }}>{facts.join(' · ')}</p>}

      {photos.length > 0
        ? <PhotoCarousel photos={photos} height={340} style={{ margin: '18px 0 4px' }} />
        : opt.image?.url && <Hero image={opt.image} height={300} style={{ margin: '18px 0 4px' }} />}

      {/* Price + actions — always visible, straight from the board */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 18, flexWrap: 'wrap', margin: '16px 0 4px' }}>
        <span>
          <span className="serif" style={{ fontSize: 28, color: 'var(--pine)' }}>{price[0]}</span>
          {price[1] && <span className="serif" style={{ fontSize: 15, color: 'var(--pine)' }}>{price[1]}</span>}
          {price[2] && <span className="muted" style={{ fontSize: 13, marginLeft: 10 }}>{price[2]}</span>}
        </span>
        <button className="btn" onClick={toggleStar} style={{ marginLeft: 'auto' }}>
          <Star size={15} fill={starred ? '#F6F2E8' : 'none'} />
          {starred ? 'Starred' : 'Star this'}{count > 0 ? ` · ${count}` : ''}
        </button>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap', marginBottom: 8 }}>
        {website && (
          <a className="btn-quiet" href={website} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>
            <Globe size={13} strokeWidth={2.2} /> Official site
          </a>
        )}
        {compareLink && (
          <a className="btn-quiet" href={compareLink} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>
            {compareLabel}
          </a>
        )}
        {d?.maps_url && (
          <a className="btn-quiet" href={d.maps_url} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>
            <MapPin size={13} strokeWidth={2.2} /> Open in Maps
          </a>
        )}
      </div>
      <ErrorText>{error}</ErrorText>

      {opt.why && <p className="serif muted" style={{ fontStyle: 'italic', fontSize: 15.5, margin: '8px 0 4px' }}>{opt.why}</p>}

      {/* Flights & transport: the board's own numbers, laid out properly */}
      {category === 'flights' && (
        <section style={{ marginTop: 14 }}>
          <LegRow label="OUT" when={opt.depart} route={opt.route} />
          <LegRow label="BACK" when={opt.return} route={opt.return_route || opt.route} />
          <p className="muted" style={{ fontSize: 13.5, marginTop: 10 }}>
            Rovyn’s estimate for planning — exact times, baggage and fare rules live on the airline and Expedia.
          </p>
        </section>
      )}
      {category === 'transport' && (
        <section style={{ marginTop: 14 }}>
          <FactRow icon={Clock} label="Departs">{opt.depart}</FactRow>
          <FactRow icon={Clock} label="Duration">{opt.duration}</FactRow>
          <p className="muted" style={{ fontSize: 13.5, marginTop: 10 }}>
            Rovyn’s estimate for planning — live schedules and fares are one tap away on Rome2Rio.
          </p>
        </section>
      )}

      {/* Live sections — only what the real sources actually returned */}
      {loadingLive && (
        <p className="muted" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '20px 0', fontStyle: 'italic' }}>
          <Loader2 className="spin" size={16} strokeWidth={2} color="var(--pine)" />
          Fetching live details…
        </p>
      )}
      {details?.err && <p style={{ color: 'var(--clay)', fontSize: 14, padding: '14px 0' }}>{details.err}</p>}

      {d && (
        <div style={{ marginTop: 6 }}>
          {(d.description || d.summary) && <ClampedText text={d.description || d.summary} />}
          {!d.description && !d.summary && ['visits', 'events'].includes(category) && opt.description && (
            <p style={{ fontSize: 14.5, color: 'var(--ink-soft)', margin: '4px 0 6px' }}>{opt.description}</p>
          )}

          <RoomOffers d={d} />
          <Facilities list={d.facilities} />

          <div style={{ marginTop: 14 }}>
            {(d.check_in || d.check_out) && (
              <FactRow icon={Check} label="Check-in / out">
                {d.check_in || '—'} / {d.check_out || '—'}
              </FactRow>
            )}
            <OpeningHours hours={d.hours} />
            <FactRow icon={MapPin} label="Address">{d.address}</FactRow>
            <FactRow icon={Phone} label="Phone">{d.phone}</FactRow>
          </div>

          {['stay', 'visits', 'events'].includes(category) && d.source !== 'board' && (
            <MapImage
              load={() => api.optionMap(tripId, optionId)}
              ratio={640 / 300}
              alt={`Map of ${title}`}
              style={{ marginTop: 18 }}
            />
          )}

          {reviewMedia && (
            <GuestReviews
              media={reviewMedia}
              fallbackRating={opt.rating}
              title={category === 'stay' ? 'Guest reviews' : 'Visitor reviews'}
              fallbackHeadline={category === 'stay' ? 'A recent stay' : 'A recent visit'}
            />
          )}

          {(() => {
            const note = SOURCE_NOTES[d.source]
              || (['flights', 'transport'].includes(category) ? null
                : isSample ? 'Sample board — connect LiteAPI and Google Places keys for live details.'
                : 'No live source answered for this one — showing the board’s own facts.');
            return note && (
              <p className="muted" style={{ fontSize: 12, marginTop: 18, fontStyle: 'italic' }}>{note}</p>
            );
          })()}
        </div>
      )}
    </Page>
  );
}
