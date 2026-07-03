import { useEffect, useState } from 'react';
import { useNavigate, useLocation, Navigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { ArrowRight } from 'lucide-react';
import { api, auth } from '../api/index.js';
import { supabase } from '../lib/supabase.js';
import { Wordmark, HorizonArt, Field, ErrorText } from '../ui.jsx';

/* Brand marks — lucide carries no logos, so two tiny inline paths. */
function GoogleMark({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M21.35 11.1H12v3.7h5.4c-.5 2.5-2.7 3.9-5.4 3.9a6 6 0 1 1 0-12c1.5 0 2.9.55 4 1.45l2.8-2.8A9.9 9.9 0 0 0 12 2a10 10 0 1 0 0 20c5.2 0 9.6-3.65 9.6-9.35 0-.53-.1-1.05-.25-1.55z" />
    </svg>
  );
}

function AppleMark({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M16.7 12.9c0-2.4 2-3.6 2.1-3.7-1.1-1.7-2.9-1.9-3.5-1.9-1.5-.2-2.9.9-3.7.9-.8 0-1.9-.9-3.2-.85-1.6.03-3.1 1-4 2.4-1.7 3-.4 7.4 1.2 9.8.8 1.2 1.8 2.5 3.1 2.45 1.2-.05 1.7-.8 3.2-.8s1.9.8 3.2.77c1.3-.02 2.2-1.2 3-2.4a10.7 10.7 0 0 0 1.4-2.8c-.03-.01-2.7-1.05-2.8-4.07zM14.4 5.6c.66-.8 1.1-1.9 1-3-.95.04-2.1.63-2.8 1.43-.6.7-1.16 1.85-1 2.93 1.05.08 2.13-.54 2.8-1.36z" />
    </svg>
  );
}

export default function Auth() {
  const navigate = useNavigate();
  const location = useLocation();
  const [mode, setMode] = useState('login');
  const [form, setForm] = useState({ name: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);

  // A failed OAuth round-trip comes back with the reason in the URL hash.
  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const desc = params.get('error_description') || params.get('error');
    if (desc) {
      setError(desc.replace(/\+/g, ' '));
      window.history.replaceState(null, '', window.location.pathname);
    }
  }, []);

  if (auth.isSignedIn()) {
    // OAuth redirects land on "/" — recover the page the user was headed to.
    const next = localStorage.getItem('rovyn_next');
    localStorage.removeItem('rovyn_next');
    return <Navigate to={location.state?.from || next || '/trips'} replace />;
  }

  async function submit(e) {
    e.preventDefault();
    setError('');
    setNotice('');
    setLoading(true);
    try {
      if (auth.mode === 'supabase') {
        if (mode === 'signup') {
          if (!form.name.trim()) throw new Error('Please tell us your name.');
          const { data, error: err } = await supabase.auth.signUp({
            email: form.email.trim(),
            password: form.password,
            options: { data: { full_name: form.name.trim() } },
          });
          if (err) throw err;
          if (!data.session) {
            setNotice('Almost there — confirm the email we just sent you, then sign in.');
            return;
          }
        } else {
          const { error: err } = await supabase.auth.signInWithPassword({
            email: form.email.trim(),
            password: form.password,
          });
          if (err) throw err;
        }
        navigate(location.state?.from || '/trips');
      } else {
        const data = mode === 'signup'
          ? await api.signup(form.email.trim(), form.password, form.name.trim())
          : await api.login(form.email.trim(), form.password);
        auth.save(data);
        navigate(location.state?.from || '/trips');
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function oauth(provider) {
    setError('');
    // Survive the round-trip through the provider: remember where the user
    // was headed (e.g. a /join/CODE invite) before leaving the app.
    if (location.state?.from) localStorage.setItem('rovyn_next', location.state.from);
    const { error: err } = await supabase.auth.signInWithOAuth({
      provider,
      options: { redirectTo: window.location.origin },
    });
    if (err) setError(err.message);
  }

  return (
    <div style={{ minHeight: '100vh', display: 'grid', gridTemplateRows: '1fr auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '48px 28px' }}>
        <div style={{ width: '100%', maxWidth: 460 }}>
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
          >
            <Wordmark size={30} />
            <h1 style={{ fontSize: 44, margin: '36px 0 12px' }}>
              One trip,<br />
              <em style={{ color: 'var(--pine)' }}>planned together.</em>
            </h1>
            <p className="muted" style={{ fontSize: 16.5, maxWidth: 380, marginBottom: 44 }}>
              Gather your people, share what you each want, and let Rovyn turn it
              into a destination, an itinerary, and every booking — in one place.
            </p>

            {auth.mode === 'supabase' && (
              <div style={{ marginBottom: 36 }}>
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                  <button type="button" className="ext-link" onClick={() => oauth('google')}
                          style={{ flex: '1 1 180px', justifyContent: 'center' }}>
                    <GoogleMark /> Continue with Google
                  </button>
                  <button type="button" className="ext-link" onClick={() => oauth('apple')}
                          style={{ flex: '1 1 180px', justifyContent: 'center' }}>
                    <AppleMark /> Continue with Apple
                  </button>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 14, margin: '26px 0 0' }}>
                  <hr className="rule" style={{ flex: 1 }} />
                  <span className="muted" style={{ fontSize: 12.5, letterSpacing: '0.08em' }}>or with email</span>
                  <hr className="rule" style={{ flex: 1 }} />
                </div>
              </div>
            )}

            <div style={{ display: 'flex', gap: 28, marginBottom: 32 }}>
              {['login', 'signup'].map(m => (
                <button
                  key={m}
                  onClick={() => { setMode(m); setError(''); setNotice(''); }}
                  className="serif"
                  style={{
                    fontSize: 19,
                    fontStyle: mode === m ? 'italic' : 'normal',
                    color: mode === m ? 'var(--pine)' : 'var(--muted)',
                    borderBottom: mode === m ? '1px solid var(--pine)' : '1px solid transparent',
                    paddingBottom: 3,
                    transition: 'color 0.2s ease',
                  }}
                >
                  {m === 'login' ? 'Sign in' : 'Create account'}
                </button>
              ))}
            </div>

            <form onSubmit={submit}>
              <AnimatePresence mode="popLayout" initial={false}>
                {mode === 'signup' && (
                  <motion.div
                    key="name"
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.3 }}
                    style={{ overflow: 'hidden' }}
                  >
                    <Field label="Your name">
                      <input
                        placeholder="Ana Martins"
                        value={form.name}
                        onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                        autoComplete="name"
                      />
                    </Field>
                  </motion.div>
                )}
              </AnimatePresence>

              <Field label="Email">
                <input
                  type="email"
                  placeholder="you@example.com"
                  value={form.email}
                  onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                  autoComplete="email"
                />
              </Field>
              <Field label="Password">
                <input
                  type="password"
                  placeholder={mode === 'signup' ? 'At least 6 characters' : '••••••••'}
                  value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
                />
              </Field>

              <ErrorText>{error}</ErrorText>
              {notice && (
                <p className="serif" style={{ color: 'var(--pine)', fontSize: 15, fontStyle: 'italic', marginTop: 14 }}>
                  {notice}
                </p>
              )}

              <button className="btn" type="submit" disabled={loading} style={{ marginTop: 18 }}>
                {loading ? 'One moment…' : mode === 'signup' ? 'Start planning' : 'Sign in'}
                <ArrowRight size={17} strokeWidth={2.2} />
              </button>
            </form>
          </motion.div>
        </div>
      </div>
      <HorizonArt />
    </div>
  );
}
