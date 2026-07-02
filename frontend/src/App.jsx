import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { auth } from './api/index.js';
import Auth from './pages/Auth.jsx';
import Trips from './pages/Trips.jsx';
import Join from './pages/Join.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Questionnaire from './pages/Questionnaire.jsx';
import Vote from './pages/Vote.jsx';
import Itinerary from './pages/Itinerary.jsx';
import Plan from './pages/Plan.jsx';
import FinalPlan from './pages/FinalPlan.jsx';

function Protected({ children }) {
  const location = useLocation();
  if (!auth.isSignedIn()) {
    return <Navigate to="/" replace state={{ from: location.pathname }} />;
  }
  return children;
}

export default function App() {
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
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
