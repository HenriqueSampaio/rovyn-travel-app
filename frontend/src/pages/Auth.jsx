import { useState } from 'react';
import { useNavigate, useLocation, Navigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { ArrowRight } from 'lucide-react';
import { api, auth } from '../api/index.js';
import { Wordmark, HorizonArt, Field, ErrorText } from '../ui.jsx';

export default function Auth() {
  const navigate = useNavigate();
  const location = useLocation();
  const [mode, setMode] = useState('login');
  const [form, setForm] = useState({ name: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  if (auth.isSignedIn()) {
    return <Navigate to={location.state?.from || '/trips'} replace />;
  }

  async function submit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const data = mode === 'signup'
        ? await api.signup(form.email.trim(), form.password, form.name.trim())
        : await api.login(form.email.trim(), form.password);
      auth.save(data);
      navigate(location.state?.from || '/trips');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
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

            <div style={{ display: 'flex', gap: 28, marginBottom: 32 }}>
              {['login', 'signup'].map(m => (
                <button
                  key={m}
                  onClick={() => { setMode(m); setError(''); }}
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
