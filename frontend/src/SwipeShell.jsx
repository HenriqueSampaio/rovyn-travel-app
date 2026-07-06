import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { useLocation, useNavigate } from 'react-router-dom';
import { motion, animate, useTransform } from 'framer-motion';
import { swipeProgress } from './ui.jsx';
import Trips from './pages/Trips.jsx';
import Feed from './pages/Feed.jsx';
import Wishlist from './pages/Wishlist.jsx';

const ROUTES = ['/trips', '/feed', '/wishlist'];
const PAGES = [Trips, Feed, Wishlist];

/* Camera-roll navigation for the three top-level pages. All three stay
   mounted in a 300%-wide track; a horizontal drag moves the track live
   under the finger (writing to swipeProgress, which the nav underline
   also reads), then springs to the nearest page — or the next one on a
   quick flick. Vertical scrolling stays native: the gesture only takes
   over once a drag is clearly horizontal.

   Inactive slides are capped to one viewport of content and, during a
   gesture, shifted down by the current scroll offset so the incoming
   page's top lines up with what you can see. */
export default function SwipeShell() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const index = Math.max(0, ROUTES.indexOf(pathname));
  const [dragging, setDragging] = useState(false);
  const [shift, setShift] = useState(0);
  const ref = useRef(null);

  const x = useTransform(swipeProgress, p => `${-p * (100 / 3)}%`);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let g = null; // active gesture

    function settle(target) {
      animate(swipeProgress, target, {
        type: 'spring', stiffness: 320, damping: 34,
        onComplete: () => {
          if (target !== index) {
            // Apply route + un-shift + scroll reset in one paint, so the
            // incoming page doesn't jump between frames.
            flushSync(() => {
              setDragging(false);
              setShift(0);
              navigate(ROUTES[target]);
            });
            window.scrollTo(0, 0);
          } else {
            setDragging(false);
            setShift(0);
          }
        },
      });
    }

    function onStart(e) {
      if (e.touches.length !== 1) return;
      if (e.target.closest('[data-noswipe], input, textarea, select, video')) return;
      g = {
        x: e.touches[0].clientX, y: e.touches[0].clientY,
        base: swipeProgress.get(), locked: false, moves: [],
      };
    }

    function onMove(e) {
      if (!g) return;
      const t = e.touches[0];
      const dx = t.clientX - g.x;
      const dy = t.clientY - g.y;
      if (!g.locked) {
        if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return; // not decided yet
        if (Math.abs(dx) <= Math.abs(dy)) { g = null; return; } // vertical — let it scroll
        g.locked = true;
        setShift(window.scrollY);
        setDragging(true);
      }
      e.preventDefault();
      const w = el.clientWidth || window.innerWidth;
      let p = g.base - dx / w;
      if (p < 0) p *= 0.35; // rubber-band past the ends
      else if (p > 2) p = 2 + (p - 2) * 0.35;
      swipeProgress.set(p);
      g.moves.push({ t: e.timeStamp, x: t.clientX });
      if (g.moves.length > 5) g.moves.shift();
    }

    function onEnd() {
      if (!g) return;
      const { locked, base, moves } = g;
      g = null;
      if (!locked) return;
      const p = swipeProgress.get();
      let target = Math.round(p);
      // A quick flick advances one page even if the drag was short
      if (moves.length >= 2 && target === Math.round(base)) {
        const a = moves[0];
        const b = moves[moves.length - 1];
        const v = (b.x - a.x) / Math.max(b.t - a.t, 1); // px/ms, +ve = finger moving right
        if (Math.abs(v) > 0.4) target = Math.round(base) + (v < 0 ? 1 : -1);
      }
      settle(Math.min(ROUTES.length - 1, Math.max(0, target)));
    }

    el.addEventListener('touchstart', onStart, { passive: true });
    el.addEventListener('touchmove', onMove, { passive: false });
    el.addEventListener('touchend', onEnd, { passive: true });
    el.addEventListener('touchcancel', onEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onStart);
      el.removeEventListener('touchmove', onMove);
      el.removeEventListener('touchend', onEnd);
      el.removeEventListener('touchcancel', onEnd);
    };
  }, [index, navigate]);

  return (
    <div ref={ref} style={{ overflow: 'hidden', touchAction: 'pan-y' }}>
      <motion.div style={{ display: 'flex', width: '300%', x }}>
        {PAGES.map((Comp, i) => {
          const active = i === index;
          return (
            <div
              key={ROUTES[i]}
              aria-hidden={!active}
              style={{
                width: `${100 / 3}%`, flexShrink: 0,
                ...(active ? {} : {
                  maxHeight: '100vh', overflow: 'hidden',
                  transform: dragging && shift ? `translateY(${shift}px)` : undefined,
                }),
              }}
            >
              <Comp />
            </div>
          );
        })}
      </motion.div>
    </div>
  );
}
