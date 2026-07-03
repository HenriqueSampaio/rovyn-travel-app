import { createClient } from '@supabase/supabase-js';

/* Present in frontend/.env.local when the app runs against Supabase.
   Absent → `supabase` is null and the app uses its built-in email/password
   auth against the FastAPI backend, exactly as before. */
const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const supabase = url && key ? createClient(url, key) : null;
