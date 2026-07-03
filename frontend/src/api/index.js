const BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

function token() {
  return localStorage.getItem('rovyn_token');
}

async function req(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`${BASE}${path}`, { ...options, headers });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401) auth.clear();
    const err = new Error(data.detail || 'Request failed');
    err.status = res.status;
    throw err;
  }
  return data;
}

export const auth = {
  save({ token: t, user }) {
    localStorage.setItem('rovyn_token', t);
    localStorage.setItem('rovyn_user', JSON.stringify(user));
  },
  user() {
    try { return JSON.parse(localStorage.getItem('rovyn_user')); } catch { return null; }
  },
  isSignedIn() { return Boolean(token()); },
  clear() {
    localStorage.removeItem('rovyn_token');
    localStorage.removeItem('rovyn_user');
  },
};

export const api = {
  signup: (email, password, name) =>
    req('/auth/signup', { method: 'POST', body: JSON.stringify({ email, password, name }) }),
  login: (email, password) =>
    req('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),

  myTrips: () => req('/trips'),
  createTrip: (name, destination = '') =>
    req('/trips', { method: 'POST', body: JSON.stringify({ name, destination }) }),
  joinTrip: (inviteCode) =>
    req('/trips/join', { method: 'POST', body: JSON.stringify({ invite_code: inviteCode }) }),
  tripStatus: (tripId) => req(`/trips/${tripId}`),
  deleteTrip: (tripId) => req(`/trips/${tripId}`, { method: 'DELETE' }),
  leaveTrip: (tripId) => req(`/trips/${tripId}/leave`, { method: 'POST' }),

  submitQuestionnaire: (tripId, data) =>
    req(`/trips/${tripId}/questionnaire`, { method: 'POST', body: JSON.stringify(data) }),

  getRecommendations: (tripId) => req(`/trips/${tripId}/recommendations`),
  generateRecommendations: (tripId) =>
    req(`/trips/${tripId}/recommendations`, { method: 'POST' }),

  castVote: (tripId, destinationName) =>
    req(`/trips/${tripId}/vote`, { method: 'POST', body: JSON.stringify({ destination_name: destinationName }) }),

  getItinerary: (tripId) => req(`/trips/${tripId}/itinerary`),
  generateItinerary: (tripId) => req(`/trips/${tripId}/itinerary`, { method: 'POST' }),

  getBookingHub: (tripId) => req(`/trips/${tripId}/hub`),

  // Planning board
  getOptions: (tripId) => req(`/trips/${tripId}/options`),
  generateOptions: (tripId) => req(`/trips/${tripId}/options`, { method: 'POST' }),
  toggleStar: (tripId, category, optionId, starred) =>
    req(`/trips/${tripId}/star`, { method: 'POST', body: JSON.stringify({ category, option_id: optionId, starred }) }),
  setCuration: (tripId, category, status) =>
    req(`/trips/${tripId}/curation`, { method: 'POST', body: JSON.stringify({ category, status }) }),

  // Final plan, approval, payment
  finalizePlan: (tripId) => req(`/trips/${tripId}/finalize`, { method: 'POST' }),
  getPlan: (tripId) => req(`/trips/${tripId}/plan`),
  approvePlan: (tripId) => req(`/trips/${tripId}/approve`, { method: 'POST' }),
  editPlan: (tripId, request) =>
    req(`/trips/${tripId}/edit`, { method: 'POST', body: JSON.stringify({ request }) }),
  reorderDays: (tripId, order) =>
    req(`/trips/${tripId}/reorder-days`, { method: 'POST', body: JSON.stringify({ order }) }),
  arrangeSlots: (tripId, day, order) =>
    req(`/trips/${tripId}/plan/day-slots`, { method: 'POST', body: JSON.stringify({ day, order }) }),
  editSlot: (tripId, day, slot, action, replacementId = '') =>
    req(`/trips/${tripId}/plan/slot`, {
      method: 'POST',
      body: JSON.stringify({ day, slot, action, replacement_id: replacementId }),
    }),
  getCheck: (tripId) => req(`/trips/${tripId}/check`),
  payShare: (tripId) => req(`/trips/${tripId}/pay`, { method: 'POST' }),
  getReceipt: (tripId) => req(`/trips/${tripId}/receipt`),

  suggestCities: (q) => req(`/places/suggest?q=${encodeURIComponent(q)}`),

  // Phase A booking checklist
  getBookings: (tripId) => req(`/trips/${tripId}/bookings`),
  confirmBooking: (tripId, itemKey, confirmation, note = '') =>
    req(`/trips/${tripId}/bookings/${encodeURIComponent(itemKey)}/confirm`, {
      method: 'POST', body: JSON.stringify({ confirmation, note }),
    }),
  unbookBooking: (tripId, itemKey) =>
    req(`/trips/${tripId}/bookings/${encodeURIComponent(itemKey)}/unbook`, { method: 'POST' }),

  // Social feed
  mediaUrl: (path) => `${BASE}${path}`,
  uploadMedia: async (file) => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${BASE}/social/media`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token()}` },
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');
    return data;
  },
  getFeed: (limit = 20, offset = 0) => req(`/social/feed?limit=${limit}&offset=${offset}`),
  createPost: (caption, media, tripId = '', vibes = []) =>
    req('/social/posts', { method: 'POST', body: JSON.stringify({ caption, media, trip_id: tripId, vibes }) }),
  deletePost: (postId) => req(`/social/posts/${postId}`, { method: 'DELETE' }),
  likePost: (postId, liked) =>
    req(`/social/posts/${postId}/like`, { method: 'POST', body: JSON.stringify({ liked }) }),
  getComments: (postId) => req(`/social/posts/${postId}/comments`),
  addComment: (postId, text) =>
    req(`/social/posts/${postId}/comments`, { method: 'POST', body: JSON.stringify({ text }) }),
  deleteComment: (postId, commentId) =>
    req(`/social/posts/${postId}/comments/${commentId}`, { method: 'DELETE' }),
  getSharedTrip: (postId) => req(`/social/posts/${postId}/trip`),
  copyCheck: (postId, startDate, endDate) =>
    req(`/social/posts/${postId}/copy-check`, {
      method: 'POST', body: JSON.stringify({ start_date: startDate, end_date: endDate }),
    }),
  copyTrip: (postId, name, startDate, endDate, adaptedPlan) =>
    req(`/social/posts/${postId}/copy`, {
      method: 'POST',
      body: JSON.stringify({ name, start_date: startDate, end_date: endDate, adapted_plan: adaptedPlan }),
    }),
};
