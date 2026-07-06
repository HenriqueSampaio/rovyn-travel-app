import { useEffect, useReducer, useState } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { auth, authReady } from './api/index.js';
import Auth from './pages/Auth.jsx';
import Trips from './pages/Trips.jsx';
import Join from './pages/Join.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Questionnaire from './pages/Questionnaire.jsx';
import Vote from './pages/Vote.jsx';
import Itinerary from './pages/Itinerary.jsx';
import Plan from './pages/Plan.jsx';
import FinalPlan from './pages/FinalPlan.jsx';
import Feed from './pages/Feed.jsx';
import NewPost from './pages/NewPost.jsx';
import PostTrip from './pages/PostTrip.jsx';
import Wishlist from './pages/Wishlist.jsx';

function Protected({ children }) {
  const location = useLocation();
  if (!auth.isSignedIn()) {
    return <Navigate to="/" replace state={{ from: location.pathname }} />;
  }
  return children;
}

export default function App() {
  // Supabase restores the session (and completes OAuth redirects)
  // asynchronously — hold routing until it has, so Protected pages
  // don't bounce a signed-in user to the sign-in screen.
  const [ready, setReady] = useState(auth.mode === 'local');
  const [, rerender] = useReducer(x => x + 1, 0);
  useEffect(() => {
    authReady.then(() => setReady(true));
    return auth.subscribe(rerender);
  }, []);
  if (!ready) return null;

  return (
    <Routes>
      <Route path="/" element={<Auth />} />
      <Route path="/join/:code" element={<Join />} />
      <Route path="/trips" element={<Protected><Trips /></Protected>} />
      <Route path="/trips/:tripId" element={<Protected><Dashboard /></Protected>} />
      <Route path="/trips/:tripId/questionnaire" element={<Protected><Questionnaire /></Protected>} />
      <Route path="/trips/:tripId/vote" element={<Protected><Vote /></Protected>} />
      <Route path="/trips/:tripId/itinerary" element={<Protected><Itinerary /></Protected>} />
      <Route path="/trips/:tripId/plan" element={<Protected><Plan /></Protected>} />
      <Route path="/trips/:tripId/final" element={<Protected><FinalPlan /></Protected>} />
      <Route path="/feed" element={<Protected><Feed /></Protected>} />
      <Route path="/feed/new" element={<Protected><NewPost /></Protected>} />
      <Route path="/feed/post/:postId/trip" element={<Protected><PostTrip /></Protected>} />
      <Route path="/wishlist" element={<Protected><Wishlist /></Protected>} />
      <Route path="/wishlist/:itemId/trip" element={<Protected><PostTrip /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
