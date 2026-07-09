import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  Bookmark, ChevronLeft, ChevronRight, Heart, Map, MessageCircle, Plus, Send, Sparkles, Trash2,
} from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Loading, ErrorText, Disc, Pill, PALETTE_CYCLE } from '../ui.jsx';

/* Stable accent per author — the same person gets the same disc color everywhere */
const authorColor = name =>
  PALETTE_CYCLE[((name || '').charCodeAt(0) || 0) % PALETTE_CYCLE.length];

const VIBE_LABEL = {
  culture: 'Culture & history', nightlife: 'Nightlife & parties', nature: 'Nature',
  food: 'Food & drink', beach: 'Beach & sun', adventure: 'Adventure',
  relaxation: 'Relaxation', shopping: 'Shopping',
};

export function timeAgo(iso) {
  const then = new Date(iso);
  const mins = Math.max(0, Math.floor((Date.now() - then.getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return then.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function MediaCarousel({ media }) {
  const [index, setIndex] = useState(0);
  const many = media.length > 1;
  const item = media[index];
  return (
    <div data-noswipe style={{
      position: 'relative', borderRadius: 14, overflow: 'hidden',
      background: 'var(--bg-soft)', border: '1px solid var(--line-soft)',
    }}>
      {item.media_type === 'video' ? (
        <video
          key={item.url}
          src={api.mediaUrl(item.url)}
          controls
          playsInline
          style={{ width: '100%', maxHeight: 560, display: 'block', background: '#111' }}
        />
      ) : (
        <img
          key={item.url}
          src={api.mediaUrl(item.url)}
          alt=""
          style={{ width: '100%', maxHeight: 560, objectFit: 'cover', display: 'block' }}
        />
      )}
      {many && (
        <>
          {index > 0 && (
            <button onClick={() => setIndex(i => i - 1)} aria-label="Previous"
                    style={carouselArrow('left')}><ChevronLeft size={18} /></button>
          )}
          {index < media.length - 1 && (
            <button onClick={() => setIndex(i => i + 1)} aria-label="Next"
                    style={carouselArrow('right')}><ChevronRight size={18} /></button>
          )}
          <div style={{
            position: 'absolute', bottom: 10, left: 0, right: 0,
            display: 'flex', justifyContent: 'center', gap: 6,
          }}>
            {media.map((_, i) => (
              <span key={i} style={{
                width: 6, height: 6, borderRadius: '50%',
                background: i === index ? '#F6F2E8' : 'rgba(246,242,232,0.45)',
              }} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function carouselArrow(side) {
  return {
    position: 'absolute', top: '50%', transform: 'translateY(-50%)', [side]: 10,
    width: 34, height: 34, borderRadius: '50%',
    background: 'rgba(35,49,43,0.55)', color: '#F6F2E8',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  };
}

function Comments({ post, onCount }) {
  const [comments, setComments] = useState(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.getComments(post.id).then(d => setComments(d.comments)).catch(() => setComments([]));
  }, [post.id]);

  async function submit(e) {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const d = await api.addComment(post.id, text.trim());
      setComments(c => [...(c || []), d.comment]);
      onCount(d.comment_count);
      setText('');
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    await api.deleteComment(post.id, id);
    setComments(c => {
      const next = c.filter(x => x.id !== id);
      onCount(next.length);
      return next;
    });
  }

  return (
    <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--line-soft)' }}>
      {comments === null && <p className="muted" style={{ fontSize: 13 }}>Loading comments…</p>}
      {comments && comments.map(c => (
        <div key={c.id} style={{ display: 'flex', gap: 10, marginBottom: 10, alignItems: 'baseline' }}>
          <span style={{ fontWeight: 700, fontSize: 13.5, whiteSpace: 'nowrap' }}>{c.author_name}</span>
          <span style={{ fontSize: 14, flex: 1 }}>{c.text}</span>
          <span className="muted" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>{timeAgo(c.created_at)}</span>
          {(c.is_mine || post.is_mine) && (
            <button onClick={() => remove(c.id)} title="Delete comment" style={{ color: 'var(--muted)' }}>
              <Trash2 size={13} />
            </button>
          )}
        </div>
      ))}
      <form onSubmit={submit} style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 8 }}>
        <input
          placeholder="Add a comment…"
          value={text}
          onChange={e => setText(e.target.value)}
          style={{ fontSize: 14, padding: '8px 2px' }}
        />
        <button type="submit" disabled={busy || !text.trim()} title="Send"
                style={{ color: text.trim() ? 'var(--pine)' : 'var(--muted)' }}>
          <Send size={17} />
        </button>
      </form>
    </div>
  );
}

function PostCard({ post, onDelete }) {
  const navigate = useNavigate();
  const [liked, setLiked] = useState(post.liked_by_me);
  const [likes, setLikes] = useState(post.like_count);
  const [commentCount, setCommentCount] = useState(post.comment_count);
  const [showComments, setShowComments] = useState(false);
  const [saved, setSaved] = useState(post.saved_by_me);
  const likeBusy = useRef(false);
  const saveBusy = useRef(false);

  async function toggleLike() {
    if (likeBusy.current) return;
    likeBusy.current = true;
    const next = !liked;
    setLiked(next);
    setLikes(n => n + (next ? 1 : -1));
    try {
      const d = await api.likePost(post.id, next);
      setLikes(d.like_count);
    } catch {
      setLiked(!next);
      setLikes(n => n + (next ? -1 : 1));
    } finally {
      likeBusy.current = false;
    }
  }

  async function toggleSave() {
    if (saveBusy.current) return;
    saveBusy.current = true;
    const next = !saved;
    setSaved(next);
    try {
      await api.savePost(post.id, next);
    } catch {
      setSaved(!next);
    } finally {
      saveBusy.current = false;
    }
  }

  async function remove() {
    if (!window.confirm('Delete this post?')) return;
    await api.deletePost(post.id);
    onDelete(post.id);
  }

  return (
    <motion.article
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      style={{ marginBottom: 56 }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
        <Disc label={post.author_name} color={authorColor(post.author_name)} size={34} />
        <span className="serif" style={{ fontSize: 18, fontWeight: 600 }}>{post.author_name}</span>
        {post.destination && (
          <span className="muted" style={{ fontSize: 13.5 }}>· {post.destination}</span>
        )}
        <span className="muted" style={{ fontSize: 12.5, marginLeft: 'auto' }}>{timeAgo(post.created_at)}</span>
        {post.is_mine && (
          <button onClick={remove} title="Delete post" style={{ color: 'var(--muted)' }}>
            <Trash2 size={15} />
          </button>
        )}
      </div>

      <MediaCarousel media={post.media} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginTop: 12 }}>
        <button onClick={toggleLike} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <Heart size={20} strokeWidth={2}
                 color={liked ? 'var(--clay)' : 'var(--ink-soft)'}
                 fill={liked ? 'var(--clay)' : 'none'} />
          <span style={{ fontSize: 14, fontWeight: 600 }}>{likes}</span>
        </button>
        <button onClick={() => setShowComments(s => !s)} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <MessageCircle size={19} strokeWidth={2} color="var(--ink-soft)" />
          <span style={{ fontSize: 14, fontWeight: 600 }}>{commentCount}</span>
        </button>
        {post.has_trip && (
          <>
            <button
              onClick={toggleSave}
              title={saved ? 'On your wishlist — tap to remove' : 'Save to wishlist for later'}
              style={{ display: 'flex', alignItems: 'center' }}
            >
              <Bookmark size={19} strokeWidth={2}
                        color={saved ? 'var(--pine)' : 'var(--ink-soft)'}
                        fill={saved ? 'var(--pine)' : 'none'} />
            </button>
            <button
              className="btn-quiet"
              style={{ marginLeft: 'auto' }}
              onClick={() => navigate(`/feed/post/${post.id}/trip`)}
            >
              <Map size={15} /> View this trip's itinerary
            </button>
          </>
        )}
      </div>

      {post.caption && (
        <p style={{ marginTop: 10, fontSize: 15 }}>
          <span style={{ fontWeight: 700, marginRight: 8 }}>{post.author_name}</span>
          {post.caption}
        </p>
      )}
      {post.vibes.length > 0 && (
        <p style={{ marginTop: 6, fontSize: 12.5, color: 'var(--pine)', fontWeight: 600 }}>
          {post.vibes.map(v => `#${(VIBE_LABEL[v] || v).toLowerCase().replace(/[^a-z]+/g, '')}`).join('  ')}
        </p>
      )}

      {showComments && <Comments post={post} onCount={setCommentCount} />}
    </motion.article>
  );
}

export default function Feed() {
  const navigate = useNavigate();
  const [posts, setPosts] = useState(null);
  const [meta, setMeta] = useState({ personalized: false, top_vibes: [] });
  const [error, setError] = useState('');

  useEffect(() => {
    api.getFeed(50, 0)
      .then(d => { setPosts(d.posts); setMeta({ personalized: d.personalized, top_vibes: d.top_vibes }); })
      .catch(err => setError(err.message));
  }, []);

  if (posts === null && !error) return <Page width={560}><Loading text="Gathering stories…" /></Page>;

  return (
    <Page width={560}>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap', marginBottom: 8 }}>
        <div>
          <p className="eyebrow">The feed</p>
          <h1 style={{ fontSize: 38, margin: '10px 0 0' }}>
            Trips worth <em style={{ color: 'var(--clay)' }}>stealing</em>
          </h1>
        </div>
        <button className="btn" onClick={() => navigate('/feed/new')}>
          <Plus size={16} /> Share
        </button>
      </div>
      {meta.personalized && meta.top_vibes.length > 0 && (
        <p className="muted" style={{ fontSize: 13.5, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 40 }}>
          <Sparkles size={14} color="var(--clay)" />
          Tuned to your taste:
          {meta.top_vibes.map((v, i) => (
            <Pill key={v} color={PALETTE_CYCLE[i % PALETTE_CYCLE.length]}>
              {(VIBE_LABEL[v] || v).toLowerCase()}
            </Pill>
          ))}
        </p>
      )}
      {!meta.personalized && <div style={{ marginBottom: 40 }} />}

      <ErrorText>{error}</ErrorText>

      {posts && posts.length === 0 && (
        <div style={{ textAlign: 'center', padding: '80px 0' }}>
          <p className="serif" style={{ fontSize: 22, fontStyle: 'italic', marginBottom: 10 }}>Nothing here yet.</p>
          <p className="muted" style={{ fontSize: 14.5 }}>Be the first — share a photo or video from a trip.</p>
        </div>
      )}

      {posts && posts.map(p => (
        <PostCard key={p.id} post={p} onDelete={id => setPosts(ps => ps.filter(x => x.id !== id))} />
      ))}
    </Page>
  );
}
