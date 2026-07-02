import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, auth } from '../api/index.js';
import { Page, Loading, ErrorText } from '../ui.jsx';

/** Landing point for invite links: /join/CODE — joins after sign-in. */
export default function Join() {
  const { code } = useParams();
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;
    if (!auth.isSignedIn()) {
      navigate('/', { state: { from: `/join/${code}` }, replace: true });
      return;
    }
    api.joinTrip(code)
      .then(res => navigate(`/trips/${res.id}`, { replace: true }))
      .catch(err => setError(err.message));
  }, [code, navigate]);

  return (
    <Page>
      {error ? (
        <div style={{ paddingTop: 60 }}>
          <h1 style={{ fontSize: 34, marginBottom: 12 }}>Couldn't join that trip</h1>
          <ErrorText>{error}</ErrorText>
          <button className="btn-quiet" style={{ marginTop: 24 }} onClick={() => navigate('/trips')}>
            Back to your trips
          </button>
        </div>
      ) : (
        <Loading text="Joining the trip…" />
      )}
    </Page>
  );
}
