import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { ArrowRight, Bookmark, CalendarDays, MapPin, Trash2 } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, ErrorText } from '../ui.jsx';
import { timeAgo } from './Feed.jsx';

/* At-a-glance itinerary: one dot per day on a connected line, with the
   day's theme underneath. Scrolls sideways when the trip runs long —
   data-noswipe keeps that scroll from triggering page-swipe navigation. */
function ItineraryTimeline({ days }) {
  if (!days?.length) return null;
  return (
    <div className="itin-strip" data-noswipe style={{ marginTop: 16 }}>
      {days.map((d, i) => (
        <div key={d.day ?? i} style={{ flex: '0 0 auto', width: 104 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <span style={{
              width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
              background: i === 0 ? 'var(--clay)' : 'var(--pine)',
            }} />
            {i < days.length - 1 && (
              <span style={{ flex: 1, height: 1, background: 'var(--line)', margin: '0 5px' }} />
            )}
          </div>
          <p style={{
            fontSize: 10.5, fontWeight: 700, letterSpacing: '0.14em',
            textTransform: 'uppercase', color: 'var(--pine)',
          }}>
            Day {d.day ?? i + 1}
          </p>
          {d.theme && (
            <p className="muted" style={{
              fontSize: 11.5, lineHeight: 1.35, marginTop: 2, paddingRight: 12,
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
            }}>
              {d.theme}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

export default function Wishlist() {
  const navigate = useNavigate();
  const [items, setItems] = useState(null);
  const [error, setError] = useState('');
  const [confirmId, setConfirmId] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.getWishlist().then(d => setItems(d.items)).catch(err => setError(err.message));
  }, []);

  async function remove(item) {
    setBusy(true);
    setError('');
    try {
      await api.removeWishlistItem(item.id);
      setItems(xs => xs.filter(x => x.id !== item.id));
    } catch (err) {
      setError(err.message);
    } finally {
      setConfirmId(null);
      setBusy(false);
    }
  }

  if (items === null && !error) return <Page width={560}><Loading text="Opening your wishlist…" /></Page>;

  return (
    <Page width={560}>
      <p className="eyebrow">Your wishlist</p>
      <h1 style={{ fontSize: 38, margin: '10px 0 8px' }}>Trips for someday</h1>
      <p className="muted" style={{ fontSize: 14.5, marginBottom: 48 }}>
        Itineraries you saved from the feed. When the time is right, open one and
        copy it onto your own dates.
      </p>

      <ErrorText>{error}</ErrorText>

      {items && items.length === 0 && (
        <div style={{ textAlign: 'center', padding: '70px 0' }}>
          <Bookmark size={26} strokeWidth={1.5} color="var(--muted)" style={{ marginBottom: 12 }} />
          <p className="serif" style={{ fontSize: 22, fontStyle: 'italic', marginBottom: 10 }}>
            Nothing saved yet.
          </p>
          <p className="muted" style={{ fontSize: 14.5, marginBottom: 22 }}>
            Tap the bookmark on any trip in the feed and it will wait for you here.
          </p>
          <button className="btn" onClick={() => navigate('/feed')}>Browse the feed</button>
        </div>
      )}

      {items && items.length > 0 && (
        <div>
          <p className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <Bookmark size={13} strokeWidth={2.5} /> Saved · {items.length}
          </p>
          {items.map((item, i) => (
            <motion.div
              key={item.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.06, duration: 0.4 }}
              className="opt-row"
              style={{ cursor: 'pointer' }}
              onClick={() => navigate(`/wishlist/${item.id}/trip`)}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="opt-title serif" style={{ fontSize: 22 }}>
                  {item.destination || 'A trip worth keeping'}
                </div>
                <div className="opt-desc" style={{ display: 'flex', alignItems: 'center', gap: '2px 14px', flexWrap: 'wrap', marginTop: 4 }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
                    <MapPin size={13} /> {item.author_name}'s itinerary
                  </span>
                  {item.duration_days && (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
                      <CalendarDays size={13} /> {item.duration_days} day{item.duration_days === 1 ? '' : 's'}
                    </span>
                  )}
                  <span style={{ whiteSpace: 'nowrap' }}>saved {timeAgo(item.added_at)}</span>
                </div>
                {(item.summary || item.title) && (
                  <p className="serif" style={{
                    fontSize: 14.5, fontStyle: 'italic', color: 'var(--ink-soft)', marginTop: 6,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {item.summary || `“${item.title}”`}
                  </p>
                )}
                <ItineraryTimeline days={item.days} />
              </div>
              {confirmId === item.id ? (
                <span style={{ display: 'inline-flex', gap: 14, alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                  <button
                    disabled={busy}
                    onClick={() => remove(item)}
                    style={{ color: 'var(--clay)', fontWeight: 700, fontSize: 13.5, whiteSpace: 'nowrap' }}
                  >
                    Remove?
                  </button>
                  <button className="muted" style={{ fontSize: 13.5 }} onClick={() => setConfirmId(null)}>cancel</button>
                </span>
              ) : (
                <button
                  title="Remove from wishlist"
                  onClick={e => { e.stopPropagation(); setConfirmId(item.id); }}
                  style={{ color: 'var(--muted)', padding: 6, marginRight: 4 }}
                >
                  <Trash2 size={16} strokeWidth={1.8} />
                </button>
              )}
              <ArrowRight size={18} color="var(--pine)" />
            </motion.div>
          ))}
        </div>
      )}
    </Page>
  );
}
