import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, ImagePlus, Link2, X } from 'lucide-react';
import { api } from '../api/index.js';
import { Page, Field, ErrorText } from '../ui.jsx';

const VIBES = [
  { id: 'culture', label: 'Culture & history' },
  { id: 'nightlife', label: 'Nightlife & parties' },
  { id: 'nature', label: 'Nature expeditions' },
  { id: 'food', label: 'Food & drink' },
  { id: 'beach', label: 'Beach & sun' },
  { id: 'adventure', label: 'Adventure & sports' },
  { id: 'relaxation', label: 'Relaxation & wellness' },
  { id: 'shopping', label: 'Shopping' },
];

// Statuses whose trips have a finished plan worth sharing
const SHAREABLE = new Set(['final_review', 'approved', 'booked']);

export default function NewPost() {
  const navigate = useNavigate();
  const fileInput = useRef(null);
  const [files, setFiles] = useState([]); // { file, previewUrl, kind }
  const [caption, setCaption] = useState('');
  const [tripId, setTripId] = useState('');
  const [vibes, setVibes] = useState([]);
  const [trips, setTrips] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');

  useEffect(() => {
    api.myTrips().then(d => setTrips(d.trips)).catch(() => {});
  }, []);

  useEffect(() => () => files.forEach(f => URL.revokeObjectURL(f.previewUrl)), [files]);

  function addFiles(list) {
    const next = [...files];
    for (const file of list) {
      if (next.length >= 10) break;
      const kind = file.type.startsWith('video') ? 'video' : 'image';
      next.push({ file, previewUrl: URL.createObjectURL(file), kind });
    }
    setFiles(next);
  }

  function removeFile(i) {
    URL.revokeObjectURL(files[i].previewUrl);
    setFiles(files.filter((_, j) => j !== i));
  }

  function toggleVibe(id) {
    setVibes(v => v.includes(id) ? v.filter(x => x !== id) : [...v, id]);
  }

  async function submit(e) {
    e.preventDefault();
    if (files.length === 0 || busy) return;
    setBusy(true);
    setError('');
    try {
      const media = [];
      for (let i = 0; i < files.length; i++) {
        setProgress(`Uploading ${i + 1} of ${files.length}…`);
        const up = await api.uploadMedia(files[i].file);
        media.push({ url: up.url, media_type: up.media_type });
      }
      setProgress('Publishing…');
      await api.createPost(caption.trim(), media, tripId, vibes);
      navigate('/feed');
    } catch (err) {
      setError(err.message);
      setBusy(false);
      setProgress('');
    }
  }

  const shareable = trips.filter(t => SHAREABLE.has(t.status));

  return (
    <Page width={560}>
      <p className="eyebrow">Share a journey</p>
      <h1 style={{ fontSize: 38, margin: '10px 0 36px' }}>New post</h1>

      <form onSubmit={submit}>
        {/* Media */}
        <div style={{ marginBottom: 32 }}>
          <span className="eyebrow" style={{ display: 'block', marginBottom: 12, fontSize: 11 }}>
            Photos & video
          </span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            {files.map((f, i) => (
              <div key={f.previewUrl} style={{
                position: 'relative', width: 104, height: 104, borderRadius: 10,
                overflow: 'hidden', border: '1px solid var(--line)',
              }}>
                {f.kind === 'video'
                  ? <video src={f.previewUrl} muted style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  : <img src={f.previewUrl} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />}
                <button type="button" onClick={() => removeFile(i)} aria-label="Remove" style={{
                  position: 'absolute', top: 4, right: 4, width: 22, height: 22, borderRadius: '50%',
                  background: 'rgba(35,49,43,0.65)', color: '#F6F2E8',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <X size={13} />
                </button>
              </div>
            ))}
            {files.length < 10 && (
              <button type="button" onClick={() => fileInput.current?.click()} style={{
                width: 104, height: 104, borderRadius: 10, border: '1px dashed var(--line)',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                gap: 6, color: 'var(--pine)', fontSize: 12, fontWeight: 600,
              }}>
                <ImagePlus size={20} strokeWidth={1.6} /> Add
              </button>
            )}
          </div>
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/webm"
            multiple
            style={{ display: 'none' }}
            onChange={e => { addFiles(e.target.files); e.target.value = ''; }}
          />
          <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
            One photo, a carousel of up to 10, or a video.
          </p>
        </div>

        <Field label="Caption">
          <textarea
            rows={3}
            placeholder="Tell the story of this trip…"
            value={caption}
            onChange={e => setCaption(e.target.value)}
          />
        </Field>

        {/* Trip association */}
        <div style={{ marginBottom: 32 }}>
          <span className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 11 }}>
            <Link2 size={12} strokeWidth={2.5} /> Attach a Rovyn trip — optional
          </span>
          <p className="muted" style={{ fontSize: 13, marginBottom: 10 }}>
            Viewers can open the exact itinerary — stay, day-by-day plan, activities — and copy it for their own dates.
          </p>
          <select value={tripId} onChange={e => setTripId(e.target.value)}>
            <option value="">No trip — just the post</option>
            {shareable.map(t => (
              <option key={t.id} value={t.id}>
                {t.name}{t.selected_destination ? ` — ${t.selected_destination}` : ''}
              </option>
            ))}
          </select>
          {shareable.length === 0 && trips.length > 0 && (
            <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
              None of your trips has a finished plan yet — only trips with a final plan can be shared.
            </p>
          )}
        </div>

        {/* Vibe tags */}
        <div style={{ marginBottom: 40 }}>
          <span className="eyebrow" style={{ display: 'block', marginBottom: 6, fontSize: 11 }}>
            Vibes — helps the right travelers find it
          </span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
            {VIBES.map(v => {
              const on = vibes.includes(v.id);
              return (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => toggleVibe(v.id)}
                  style={{
                    padding: '8px 16px', borderRadius: 999, fontSize: 13, fontWeight: 600,
                    border: `1px solid ${on ? 'var(--pine)' : 'var(--line)'}`,
                    background: on ? 'rgba(46,93,79,0.10)' : 'transparent',
                    color: on ? 'var(--pine)' : 'var(--ink-soft)',
                  }}
                >
                  {v.label}
                </button>
              );
            })}
          </div>
          {tripId && vibes.length === 0 && (
            <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
              Leave empty and Rovyn will tag it from the trip's group preferences.
            </p>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
          <button className="btn" disabled={busy || files.length === 0}>
            {busy ? (progress || 'Publishing…') : <>Publish <ArrowRight size={16} /></>}
          </button>
          <button type="button" className="btn-quiet" onClick={() => navigate('/feed')} disabled={busy}>
            Cancel
          </button>
        </div>
        <ErrorText>{error}</ErrorText>
      </form>
    </Page>
  );
}
