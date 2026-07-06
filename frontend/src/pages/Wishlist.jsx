import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { ArrowRight, Bookmark, CalendarDays, MapPin, Trash2 } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, ErrorText } from '../ui.jsx';
import { timeAgo } from './Feed.jsx';

function WishlistCard({ item, onRemove }) {
  const navigate = useNavigate();

  async function remove() {
    if (!window.confirm(`Remove ${item.destination || 'this trip'} from your wishlist?`)) return;
    await api.removeWishlistItem(item.id);
    onRemove(item.id);
  }

  return (
    <motion.article
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      style={{
        padding: '24px 26px', borderRadius: 14, border: '1px solid var(--line)',
        marginBottom: 20, background: 'rgba(46,93,79,0.03)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <p className="serif" style={{ fontSize: 22, fontWeight: 600, flex: 1 }}>
          {item.destination || 'A trip worth keeping'}
        </p>
        <span className="muted" style={{ fontSize: 12.5, whiteSpace: 'nowrap' }}>
          saved {timeAgo(item.added_at)}
        </span>
        <button onClick={remove} title="Remove from wishlist" style={{ color: 'var(--muted)' }}>
          <Trash2 size={15} />
        </button>
      </div>

      <p className="muted" style={{ fontSize: 13.5, margin: '6px 0 0', display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <MapPin size={13} /> {item.author_name}'s itinerary
        </span>
        {item.duration_days && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <CalendarDays size={13} /> {item.duration_days} day{item.duration_days === 1 ? '' : 's'}
          </span>
        )}
      </p>

      {(item.summary || item.title) && (
        <p className="serif" style={{ fontSize: 15, fontStyle: 'italic', color: 'var(--ink-soft)', marginTop: 12 }}>
          {item.summary || `“${item.title}”`}
        </p>
      )}

      <button className="btn" style={{ marginTop: 18 }}
              onClick={() => navigate(`/wishlist/${item.id}/trip`)}>
        Open itinerary <ArrowRight size={15} />
      </button>
    </motion.article>
  );
}

export default function Wishlist() {
  const navigate = useNavigate();
  const [items, setItems] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.getWishlist().then(d => setItems(d.items)).catch(err => setError(err.message));
  }, []);

  if (items === null && !error) return <Page width={560}><Loading text="Opening your wishlist…" /></Page>;

  return (
    <Page width={560}>
      <p className="eyebrow">Your wishlist</p>
      <h1 style={{ fontSize: 38, margin: '10px 0 8px' }}>Trips for someday</h1>
      <p className="muted" style={{ fontSize: 14.5, marginBottom: 40 }}>
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

      {items && items.map(item => (
        <WishlistCard key={item.id} item={item}
                      onRemove={id => setItems(xs => xs.filter(x => x.id !== id))} />
      ))}
    </Page>
  );
}
