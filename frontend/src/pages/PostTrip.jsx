import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  AlertTriangle, ArrowLeft, ArrowRight, BedDouble, CalendarDays, Check, CheckCircle2,
  Copy, HelpCircle, Loader2, MapPin, PartyPopper, Sparkles, Sun, XCircle,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, Loading, ErrorText } from '../ui.jsx';

const EVENT_STATUS = {
  likely_available: { Icon: CheckCircle2, color: 'var(--pine)', label: 'Likely available' },
  seasonal: { Icon: Sun, color: 'var(--clay)', label: 'Seasonal' },
  date_specific: { Icon: XCircle, color: 'var(--clay)', label: 'Tied to the original dates' },
  unknown: { Icon: HelpCircle, color: 'var(--muted)', label: 'Verify locally' },
};

function Section({ title, children }) {
  return (
    <section style={{ marginBottom: 44 }}>
      <p className="eyebrow" style={{ marginBottom: 16 }}>{title}</p>
      {children}
    </section>
  );
}

function DayRow({ day }) {
  return (
    <div style={{ padding: '16px 0', borderBottom: '1px solid var(--line-soft)' }}>
      <p style={{ fontWeight: 700, fontSize: 14, marginBottom: 6 }}>
        Day {day.day}
        {day.date ? <span className="muted" style={{ fontWeight: 400 }}> · {day.date}</span> : null}
        {day.theme ? <span className="serif" style={{ fontWeight: 400, fontStyle: 'italic' }}> — {day.theme}</span> : null}
      </p>
      {['morning', 'afternoon', 'evening'].map(period => day[period] && (
        <p key={period} style={{ fontSize: 14, marginBottom: 3 }}>
          <span className="muted" style={{
            fontSize: 11, fontWeight: 700, letterSpacing: '0.12em',
            textTransform: 'uppercase', marginRight: 10,
          }}>{period}</span>
          {day[period]}
        </p>
      ))}
    </div>
  );
}

function CopyFlow({ postId, plan }) {
  const navigate = useNavigate();
  const [phase, setPhase] = useState('idle'); // idle | dates | checking | report | creating
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [name, setName] = useState('');
  const [result, setResult] = useState(null); // { report, plan }
  const [error, setError] = useState('');

  function openDates() {
    // Default: same trip length, starting a month from now
    const s = new Date();
    s.setDate(s.getDate() + 30);
    const e = new Date(s);
    e.setDate(e.getDate() + Math.max(plan.duration_days || 5, 1));
    setStart(s.toISOString().slice(0, 10));
    setEnd(e.toISOString().slice(0, 10));
    setPhase('dates');
  }

  async function check(e) {
    e.preventDefault();
    setPhase('checking');
    setError('');
    try {
      const d = await api.copyCheck(postId, start, end);
      setResult(d);
      setName(`${plan.destination || 'My trip'} — copied`);
      setPhase('report');
    } catch (err) {
      setError(err.message);
      setPhase('dates');
    }
  }

  async function confirm(e) {
    e.preventDefault();
    setPhase('creating');
    setError('');
    try {
      const t = await api.copyTrip(postId, name.trim(), start, end, result.plan);
      navigate(`/trips/${t.id}/final`);
    } catch (err) {
      setError(err.message);
      setPhase('report');
    }
  }

  if (phase === 'idle') {
    return (
      <div style={{
        padding: '28px 26px', borderRadius: 14, border: '1px solid var(--line)',
        background: 'rgba(46,93,79,0.05)', marginBottom: 60,
      }}>
        <p className="serif" style={{ fontSize: 21, marginBottom: 8 }}>Make this trip yours</p>
        <p className="muted" style={{ fontSize: 14, marginBottom: 18 }}>
          Pick your own dates and Rovyn's AI will check the whole itinerary against them —
          which events still run, whether the day count fits — and adapt it for you.
        </p>
        <button className="btn clay" onClick={openDates}>
          <Copy size={15} /> Copy this itinerary
        </button>
      </div>
    );
  }

  if (phase === 'checking') {
    return (
      <div style={{ padding: '48px 26px', borderRadius: 14, border: '1px solid var(--line)', marginBottom: 60, textAlign: 'center' }}>
        <Loader2 className="spin" size={26} strokeWidth={1.6} color="var(--pine)" style={{ margin: '0 auto 14px' }} />
        <p className="serif" style={{ fontStyle: 'italic', fontSize: 16 }}>
          Checking the itinerary against your dates — events, season, trip length…
        </p>
      </div>
    );
  }

  if (phase === 'dates') {
    return (
      <form onSubmit={check} style={{
        padding: '28px 26px', borderRadius: 14, border: '1px solid var(--line)', marginBottom: 60,
      }}>
        <p className="serif" style={{ fontSize: 21, marginBottom: 18 }}>When would you go?</p>
        <div className="two-col" style={{ columnGap: 24 }}>
          <Field label="First day">
            <input type="date" required value={start} onChange={e => setStart(e.target.value)} />
          </Field>
          <Field label="Last day">
            <input type="date" required value={end} min={start} onChange={e => setEnd(e.target.value)} />
          </Field>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: -10, marginBottom: 20 }}>
          The original ran {plan.duration_days} day{plan.duration_days === 1 ? '' : 's'} — yours doesn't have to.
        </p>
        <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
          <button className="btn" disabled={!start || !end}>
            <Sparkles size={15} /> Check my dates
          </button>
          <button type="button" className="btn-quiet" onClick={() => setPhase('idle')}>Cancel</button>
        </div>
        <ErrorText>{error}</ErrorText>
      </form>
    );
  }

  // report / creating
  const { report } = result;
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ padding: '28px 26px', borderRadius: 14, border: '1px solid var(--line)', marginBottom: 60 }}
    >
      <p className="serif" style={{ fontSize: 21, marginBottom: 4 }}>
        {report.feasible ? 'Your version is ready' : 'This one needs attention'}
      </p>
      <p className="muted" style={{ fontSize: 13.5, marginBottom: 20 }}>
        {start} → {end} · {report.day_count?.new} days (original: {report.day_count?.original})
      </p>

      {report.duration_note && (
        <p style={{ fontSize: 14, marginBottom: 18, display: 'flex', gap: 9 }}>
          <CalendarDays size={16} color="var(--pine)" style={{ flexShrink: 0, marginTop: 3 }} />
          {report.duration_note}
        </p>
      )}

      {(report.events || []).length > 0 && (
        <div style={{ marginBottom: 18 }}>
          <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10 }}>Events on your dates</p>
          {report.events.map((ev, i) => {
            const s = EVENT_STATUS[ev.status] || EVENT_STATUS.unknown;
            return (
              <p key={i} style={{ fontSize: 13.5, marginBottom: 7, display: 'flex', gap: 9 }}>
                <s.Icon size={15} color={s.color} style={{ flexShrink: 0, marginTop: 3 }} />
                <span><strong>{ev.name}</strong> — {s.label}. <span className="muted">{ev.note}</span></span>
              </p>
            );
          })}
        </div>
      )}

      {(report.changes || []).length > 0 && (
        <div style={{ marginBottom: 18 }}>
          <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10 }}>What Rovyn adjusted</p>
          {report.changes.map((c, i) => (
            <p key={i} style={{ fontSize: 13.5, marginBottom: 6, display: 'flex', gap: 9 }}>
              <Check size={15} color="var(--pine)" style={{ flexShrink: 0, marginTop: 3 }} /> {c}
            </p>
          ))}
        </div>
      )}

      {(report.warnings || []).length > 0 && (
        <div style={{ marginBottom: 22 }}>
          <p className="eyebrow" style={{ fontSize: 10.5, marginBottom: 10, color: 'var(--clay)' }}>Worth double-checking</p>
          {report.warnings.map((w, i) => (
            <p key={i} style={{ fontSize: 13.5, marginBottom: 6, display: 'flex', gap: 9 }}>
              <AlertTriangle size={15} color="var(--clay)" style={{ flexShrink: 0, marginTop: 3 }} /> {w}
            </p>
          ))}
        </div>
      )}

      <form onSubmit={confirm} style={{ borderTop: '1px solid var(--line-soft)', paddingTop: 22 }}>
        <Field label="Name your trip">
          <input required value={name} onChange={e => setName(e.target.value)} />
        </Field>
        <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
          <button className="btn clay" disabled={phase === 'creating' || !name.trim()}>
            {phase === 'creating'
              ? <>Creating…</>
              : <>Create my trip <ArrowRight size={15} /></>}
          </button>
          <button type="button" className="btn-quiet" onClick={() => setPhase('dates')} disabled={phase === 'creating'}>
            Change dates
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 12 }}>
          It lands in your trips as a ready plan — you can still request changes or reorder days before approving.
        </p>
        <ErrorText>{error}</ErrorText>
      </form>
    </motion.div>
  );
}

export default function PostTrip() {
  const { postId } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getSharedTrip(postId).then(setData).catch(err => setError(err.message));
  }, [postId]);

  if (error) return <Page width={620}><ErrorText>{error}</ErrorText></Page>;
  if (!data) return <Page width={620}><Loading text="Opening the itinerary…" /></Page>;

  const { plan } = data;
  const days = plan.itinerary?.days || [];
  const stay = plan.stay || {};

  return (
    <Page width={620}>
      <button className="btn-quiet" onClick={() => navigate('/feed')} style={{ marginBottom: 28 }}>
        <ArrowLeft size={15} /> Back to the feed
      </button>

      <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <MapPin size={13} strokeWidth={2.5} /> {data.author_name}'s trip
      </p>
      <h1 style={{ fontSize: 40, margin: '10px 0 6px' }}>
        {plan.destination}{plan.country ? `, ${plan.country}` : ''}
      </h1>
      <p className="muted" style={{ fontSize: 14.5, marginBottom: 36 }}>
        {plan.dates?.start} → {plan.dates?.end} · {plan.duration_days} days
        {plan.itinerary?.title ? ` · “${plan.itinerary.title}”` : ''}
      </p>

      {plan.summary && (
        <p className="serif" style={{ fontSize: 17.5, fontStyle: 'italic', color: 'var(--ink-soft)', marginBottom: 44 }}>
          {plan.summary}
        </p>
      )}

      <CopyFlow postId={postId} plan={plan} />

      {stay.name && (
        <Section title="Where they stayed">
          <div style={{ display: 'flex', gap: 12, alignItems: 'baseline' }}>
            <BedDouble size={17} color="var(--pine)" style={{ alignSelf: 'center' }} />
            <div>
              <p style={{ fontWeight: 700, fontSize: 16 }}>{stay.name}</p>
              <p className="muted" style={{ fontSize: 13.5 }}>
                {[stay.type, stay.area, stay.price_per_night ? `$${stay.price_per_night}/night` : null,
                  stay.rating ? `★ ${stay.rating}` : null].filter(Boolean).join(' · ')}
              </p>
            </div>
          </div>
        </Section>
      )}

      {days.length > 0 && (
        <Section title="Day by day">
          {days.map(d => <DayRow key={d.day} day={d} />)}
        </Section>
      )}

      {(plan.visits || []).length > 0 && (
        <Section title="Visits & sights">
          {plan.visits.map((v, i) => (
            <p key={i} style={{ fontSize: 14.5, marginBottom: 8 }}>
              <strong>{v.name}</strong>
              {v.cost_per_person ? <span className="muted"> · ${v.cost_per_person}/person</span> : null}
              {v.description ? <span className="muted"> — {v.description}</span> : null}
            </p>
          ))}
        </Section>
      )}

      {(plan.events || []).length > 0 && (
        <Section title="Events they hit">
          {plan.events.map((ev, i) => (
            <p key={i} style={{ fontSize: 14.5, marginBottom: 8, display: 'flex', gap: 9 }}>
              <PartyPopper size={15} color="var(--clay)" style={{ flexShrink: 0, marginTop: 4 }} />
              <span>
                <strong>{ev.name}</strong>
                {ev.date_hint ? <span className="muted"> · {ev.date_hint}</span> : null}
                {ev.description ? <span className="muted"> — {ev.description}</span> : null}
              </span>
            </p>
          ))}
        </Section>
      )}

      {(plan.tips || []).length > 0 && (
        <Section title="Their tips">
          {plan.tips.map((t, i) => (
            <p key={i} className="muted" style={{ fontSize: 14, marginBottom: 6 }}>— {t}</p>
          ))}
        </Section>
      )}
    </Page>
  );
}
