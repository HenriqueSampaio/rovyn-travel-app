import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  BedDouble, Lightbulb, MoonStar, PlaneTakeoff, Star, Sun, Sunset, Ticket, Wallet,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, HorizonArt } from '../ui.jsx';

const PERIODS = [
  { key: 'morning', label: 'Morning', Icon: Sun },
  { key: 'afternoon', label: 'Afternoon', Icon: Sunset },
  { key: 'evening', label: 'Evening', Icon: MoonStar },
];

function Day({ day, index }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: Math.min(0.15 + index * 0.08, 0.7) }}
      style={{ display: 'grid', gridTemplateColumns: '110px 1fr', gap: 28, padding: '34px 0', borderTop: '1px solid var(--line-soft)' }}
    >
      <div>
        <div className="serif" style={{ fontSize: 26, color: 'var(--pine)' }}>Day {day.day}</div>
        <div className="muted" style={{ fontSize: 13 }}>{day.date}</div>
        {day.city && <div className="muted" style={{ fontSize: 13, fontStyle: 'italic' }}>{day.city}</div>}
      </div>
      <div>
        <h3 className="serif" style={{ fontSize: 21, fontStyle: 'italic', marginBottom: 18 }}>{day.theme}</h3>
        {PERIODS.map(({ key, label, Icon }) => {
          const act = day[key];
          if (!act?.activity) return null;
          return (
            <div key={key} style={{ display: 'flex', gap: 14, marginBottom: 16 }}>
              <Icon size={17} strokeWidth={1.7} color="var(--clay)" style={{ marginTop: 4, flexShrink: 0 }} />
              <div>
                <p style={{ fontWeight: 600, fontSize: 15.5 }}>
                  {act.activity}
                  {act.bookable && (
                    <span style={{ marginLeft: 10, fontSize: 12, color: 'var(--clay)', fontWeight: 700, letterSpacing: '0.06em' }}>
                      BOOK AHEAD
                    </span>
                  )}
                </p>
                {act.description && <p className="muted" style={{ fontSize: 14 }}>{act.description}</p>}
                {act.cost_per_person > 0 && (
                  <p style={{ fontSize: 13, color: 'var(--pine)', fontWeight: 600 }}>${act.cost_per_person} per person</p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </motion.div>
  );
}

export default function Itinerary() {
  const navigate = useNavigate();
  const { tripId } = useParams();
  const [itinerary, setItinerary] = useState(null);
  const [hub, setHub] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => {
    async function load() {
      try {
        const data = await api.getItinerary(tripId);
        if (data.itinerary) {
          clearInterval(pollRef.current);
          setItinerary(data.itinerary);
          api.getBookingHub(tripId).then(setHub).catch(() => {});
        } else if (!data.generating && !data.destination) {
          navigate(`/trips/${tripId}`);
        }
      } catch {
        navigate(`/trips/${tripId}`);
      }
    }
    load();
    pollRef.current = setInterval(load, 3000);
    return () => clearInterval(pollRef.current);
  }, [tripId, navigate]);

  if (!itinerary) return <Page><Loading text="Sketching your days…" /></Page>;

  const hotel = itinerary.hotel || {};
  const costs = itinerary.cost_breakdown || {};
  const stays = hub?.stays || itinerary.booking_links?.stays || {};
  const flights = hub?.flights || itinerary.booking_links?.flights || {};
  const activities = hub?.activities || itinerary.booking_links?.activities || {};

  return (
    <Page width={760}>
      <p className="eyebrow">The plan</p>
      <h1 style={{ fontSize: 52, margin: '10px 0 6px' }}>{itinerary.destination}</h1>
      <p className="muted" style={{ marginBottom: 8 }}>
        {itinerary.country ? `${itinerary.country} · ` : ''}{itinerary.duration_days} days · {itinerary.dates?.start} → {itinerary.dates?.end}
      </p>
      <div style={{ margin: '20px -8px 40px' }}><HorizonArt height={130} /></div>

      {/* Booking hub */}
      <section style={{ marginBottom: 64 }}>
        <p className="eyebrow" style={{ marginBottom: 6 }}>Booking hub</p>
        <h2 style={{ fontSize: 27, marginBottom: 26 }}>Book the whole trip from here</h2>

        {Object.keys(flights).length > 0 && (
          <div style={{ marginBottom: 26 }}>
            <p style={{ fontWeight: 700, fontSize: 15, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
              <PlaneTakeoff size={16} strokeWidth={1.8} color="var(--pine)" /> Flights
            </p>
            <p className="muted" style={{ fontSize: 14, marginBottom: 12 }}>
              Expedia searches pre-filled with your dates and party size, one per departure city.
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
              {Object.entries(flights).map(([city, url]) => (
                <a key={city} className="ext-link" href={url} target="_blank" rel="noreferrer">From {city}</a>
              ))}
            </div>
          </div>
        )}

        <div style={{ marginBottom: 26 }}>
          <p style={{ fontWeight: 700, fontSize: 15, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
            <BedDouble size={16} strokeWidth={1.8} color="var(--pine)" /> Stay
          </p>
          <p className="muted" style={{ fontSize: 14, marginBottom: 12 }}>
            Compare {itinerary.destination} stays for your exact dates and group size.
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            {stays.airbnb && <a className="ext-link" href={stays.airbnb} target="_blank" rel="noreferrer">Airbnb</a>}
            {stays.booking && <a className="ext-link" href={stays.booking} target="_blank" rel="noreferrer">Booking.com</a>}
          </div>
        </div>

        <div style={{ marginBottom: 20 }}>
          <p style={{ fontWeight: 700, fontSize: 15, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Ticket size={16} strokeWidth={1.8} color="var(--pine)" /> Tours & activities
          </p>
          <p className="muted" style={{ fontSize: 14, marginBottom: 12 }}>
            {hub?.bookable_activities?.length
              ? `${hub.bookable_activities.length} activities in the plan are worth booking ahead.`
              : 'Guided tours, tickets and experiences for your dates.'}
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            {activities.getyourguide && <a className="ext-link" href={activities.getyourguide} target="_blank" rel="noreferrer">GetYourGuide</a>}
            {activities.viator && <a className="ext-link" href={activities.viator} target="_blank" rel="noreferrer">Viator</a>}
          </div>
        </div>

        {hub?.note && <p className="muted" style={{ fontSize: 12.5, maxWidth: 560 }}>{hub.note}</p>}
      </section>

      {/* Stay */}
      <section style={{ marginBottom: 64 }}>
        <p className="eyebrow" style={{ marginBottom: 18 }}>Where you'll rest</p>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
          <div>
            <h2 style={{ fontSize: 27, marginBottom: 4 }}>{hotel.name}</h2>
            <p className="muted" style={{ fontSize: 14, display: 'flex', alignItems: 'center', gap: 6 }}>
              {hotel.type}
              {hotel.rating ? <><Star size={13} fill="var(--clay)" color="var(--clay)" /> {hotel.rating}</> : null}
            </p>
            {(hotel.amenities || []).length > 0 && (
              <p className="muted" style={{ fontSize: 13.5, marginTop: 6 }}>{hotel.amenities.join(' · ')}</p>
            )}
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="serif" style={{ fontSize: 32, color: 'var(--pine)' }}>${hotel.price_per_night}<span style={{ fontSize: 15 }}> / night</span></div>
            <div className="muted" style={{ fontSize: 13 }}>≈ ${hotel.total_hotel_cost_per_person} per person total</div>
          </div>
        </div>
      </section>

      {/* Days */}
      <section style={{ marginBottom: 64 }}>
        <p className="eyebrow" style={{ marginBottom: 18 }}>Day by day</p>
        {(itinerary.days || []).map((day, i) => <Day key={day.day} day={day} index={i} />)}
      </section>

      {/* Costs */}
      <section style={{ marginBottom: 64 }}>
        <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 18 }}>
          <Wallet size={13} strokeWidth={2.5} /> Cost per person
        </p>
        {[
          ['Accommodation', costs.accommodation],
          ['Activities & tours', costs.activities],
          ['Meals (estimate)', costs.meals_estimate],
          ['Local transport', costs.transport_estimate],
        ].filter(([, v]) => v > 0).map(([label, value]) => (
          <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '12px 0', borderBottom: '1px solid var(--line-soft)', fontSize: 15 }}>
            <span className="muted">{label}</span>
            <span style={{ fontWeight: 600 }}>${value}</span>
          </div>
        ))}
        <div style={{ display: 'flex', justifyContent: 'space-between', padding: '18px 0 0', alignItems: 'baseline' }}>
          <span style={{ fontWeight: 700 }}>Total, flights aside</span>
          <span className="serif" style={{ fontSize: 32, color: 'var(--pine)' }}>${itinerary.total_cost_per_person}</span>
        </div>
      </section>

      {/* Tips */}
      {(itinerary.tips || []).length > 0 && (
        <section>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 18 }}>
            <Lightbulb size={13} strokeWidth={2.5} /> Good to know
          </p>
          {itinerary.tips.map((tip, i) => (
            <p key={i} className="serif" style={{ fontSize: 16.5, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 12, maxWidth: 620 }}>
              {tip}
            </p>
          ))}
        </section>
      )}
    </Page>
  );
}
