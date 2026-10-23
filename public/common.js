// Shared by the host and player screens.
const $ = (s, el = document) => el.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ordinal = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th'));
const fmt = n => Number(n || 0).toLocaleString('en-US');

const SHAPES = [
  '<svg viewBox="0 0 32 32"><path d="M16 4 30 28H2Z" fill="currentColor"/></svg>',
  '<svg viewBox="0 0 32 32"><path d="M16 2 30 16 16 30 2 16Z" fill="currentColor"/></svg>',
  '<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="13" fill="currentColor"/></svg>',
  '<svg viewBox="0 0 32 32"><rect x="4" y="4" width="24" height="24" rx="2" fill="currentColor"/></svg>',
];
const TICK = '<svg viewBox="0 0 32 32"><path d="M7 16.5l6 6L25.5 10" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CROSS = '<svg viewBox="0 0 32 32"><path d="M9 9l14 14M23 9 9 23" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/></svg>';

async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Something went wrong, please try again');
  return data;
}

// Countdown kept in step with the server: every reply says how many ms are left.
const Timer = {
  end: 0,
  set(ms) { if (ms != null) this.end = Date.now() + ms; else this.end = 0; },
  left() { return Math.max(0, this.end - Date.now()); },
  secs() { return Math.ceil(this.left() / 1000); },
};

// Long-poll loop: the server answers as soon as this screen's view changes.
const Poll = {
  gen: 0,
  start(params, onView) {
    const gen = ++this.gen;
    let etag = '';
    (async () => {
      while (gen === this.gen) {
        try {
          const p = params();
          const r = await fetch(`/api/state?pin=${encodeURIComponent(p.pin)}&token=${encodeURIComponent(p.token)}&etag=${etag}`, { cache: 'no-store' });
          if (!r.ok) throw new Error(r.status);
          const d = await r.json();
          if (gen !== this.gen) return;
          Conn.ok();
          Timer.set(d.ms);
          if (d.view) { etag = d.etag; onView(d.view); }
        } catch (e) {
          if (gen !== this.gen) return;
          Conn.lost();
          await sleep(1500);
        }
      }
    })();
  },
  stop() { this.gen++; },
};

const Conn = {
  el: null, bad: 0,
  lost() {
    if (++this.bad < 2) return;
    if (!this.el) { this.el = document.createElement('div'); this.el.className = 'conn'; this.el.textContent = 'Reconnecting…'; document.body.appendChild(this.el); }
    this.el.hidden = false;
  },
  ok() { this.bad = 0; if (this.el) this.el.hidden = true; },
};
