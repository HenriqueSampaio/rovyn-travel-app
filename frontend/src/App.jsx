import { useEffect, useReducer, useState } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { auth, authReady } from './api/index.js';
import Auth from './pages/Auth.jsx';
import SwipeShell from './SwipeShell.jsx';
import Join from './pages/Join.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Questionnaire from './pages/Questionnaire.jsx';
import Vote from './pages/Vote.jsx';
import Itinerary from './pages/Itinerary.jsx';
import Plan from './pages/Plan.jsx';
import OptionDetail from './pages/OptionDetail.jsx';
import FinalPlan from './pages/FinalPlan.jsx';
import NewPost from './pages/NewPost.jsx';
import PostTrip from './pages/PostTrip.jsx';

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
      {/* The three top-level pages live in one persistent swipe deck, so
          dragging between them slides the neighbor in under the finger */}
      <Route element={<Protected><SwipeShell /></Protected>}>
        <Route path="/trips" element={null} />
        <Route path="/feed" element={null} />
        <Route path="/wishlist" element={null} />
      </Route>
      <Route path="/trips/:tripId" element={<Protected><Dashboard /></Protected>} />
      <Route path="/trips/:tripId/questionnaire" element={<Protected><Questionnaire /></Protected>} />
      <Route path="/trips/:tripId/vote" element={<Protected><Vote /></Protected>} />
      <Route path="/trips/:tripId/itinerary" element={<Protected><Itinerary /></Protected>} />
      <Route path="/trips/:tripId/plan" element={<Protected><Plan /></Protected>} />
      <Route path="/trips/:tripId/plan/option/:optionId" element={<Protected><OptionDetail /></Protected>} />
      <Route path="/trips/:tripId/final" element={<Protected><FinalPlan /></Protected>} />
      <Route path="/feed/new" element={<Protected><NewPost /></Protected>} />
      <Route path="/feed/post/:postId/trip" element={<Protected><PostTrip /></Protected>} />
      <Route path="/wishlist/:itemId/trip" element={<Protected><PostTrip /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
