// JoeDeck player: polls /api/now and runs the slideshow of images and websites.
//
// Signs in a group run in LOCKSTEP: instead of counting on their own, they work out which
// item should be on screen from the clock (every Pi keeps network time) and the group's
// start time, so all of them change at the same moment. The next item is loaded a few
// seconds early so the switch itself is instant.
(() => {
  const POLL_MS = 5000;
  const WEB_LOAD_TIMEOUT = 8000;   // show a website after this long even if it hasn't finished loading
  const PRELOAD_MS = 6000;         // lockstep: start loading the next item this long before it's due
  const stage = document.getElementById('stage');
  const layers = [document.getElementById('a'), document.getElementById('b')];
  const empty = document.getElementById('empty');
  let front = 0;
  let version = null, itemsKey = null, reloadToken = null;
  let items = [], shuffle = false, sync = null;
  let order = [], pos = 0;            // free-running mode
  let shown = null, stepSeq = 0;      // lockstep mode
  let timers = [], refreshTimer = null, cleanupTimer = null;
  let generation = 0;
  let settings = { transition_ms: 800, transition_style: 'fade', fit: 'contain' };

  const later = (fn, ms) => { timers.push(setTimeout(fn, Math.max(0, ms))); };
  const clearAll = () => { timers.forEach(clearTimeout); timers = []; clearInterval(refreshTimer); };
  const transMs = () => (settings.transition_style === 'none' ? 0 : Number(settings.transition_ms) || 0);

  function applySettings(s) {
    settings = s;
    document.body.className = s.orientation || 'landscape';
    document.body.style.background = s.background || '#000';
    stage.style.background = s.background || '#000';
    stage.classList.toggle('slide', s.transition_style === 'slide');
    for (const l of layers) l.style.transitionDuration = transMs() + 'ms';
    for (const img of stage.querySelectorAll('.layer img')) img.style.objectFit = s.fit === 'cover' ? 'cover' : 'contain';
  }

  // Optional corner label with this sign's Wi-Fi address (updated on every check, so it follows network changes).
  function showWifi(on, ip) {
    const el = document.getElementById('wifiip');
    if (!el) return;
    el.style.display = on ? 'block' : 'none';
    if (!on) return;
    el.textContent = ip ? 'Wi-Fi ' + ip : 'Wi-Fi not connected';
    el.classList.toggle('off', !ip);
  }

  function showEmpty(info) {
    for (const l of layers) { l.classList.remove('on', 'out'); l.innerHTML = ''; }
    const port = info.port && info.port !== '80' ? ':' + info.port : '';
    const hosts = [info.hostname + '.local', ...(info.ips || [])];
    document.querySelector('#empty h1').textContent = `“${info.name}” is running`;
    document.getElementById('urls').innerHTML = hosts.map((h) => `<p><code>http://${h}${port}/</code></p>`).join('');
    empty.style.display = 'flex';
  }

  // Load an item inside `container` (a hidden layer); resolves once it's ready to show.
  function build(item, container) {
    return new Promise((resolve, reject) => {
      if (item.type === 'web') {
        const f = document.createElement('iframe');
        const z = (Number(item.zoom) || 100) / 100;
        f.style.width = (100 / z) + '%';
        f.style.height = (100 / z) + '%';
        f.style.transform = `scale(${z})`;
        // no allow-top-navigation: pages can't "break out" and replace the player
        f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups');
        f.setAttribute('allow', 'autoplay; fullscreen');
        f.setAttribute('scrolling', 'no');
        let done = false;
        const finish = () => { if (!done) { done = true; resolve(f); } };
        f.addEventListener('load', () => setTimeout(finish, 600));   // give the page a moment to draw
        setTimeout(finish, WEB_LOAD_TIMEOUT);
        f.src = item.url;
        container.appendChild(f);   // iframes only load once they're in the page
        return;
      }
      const img = new Image();
      img.onload = () => (img.decode ? img.decode().catch(() => {}) : Promise.resolve()).then(() => {
        container.appendChild(img); resolve(img);
      });
      img.onerror = reject;
      img.style.objectFit = settings.fit === 'cover' ? 'cover' : 'contain';
      img.src = item.url;
    });
  }

  // Put an item into the hidden layer (at its starting position, without animating).
  function prepare(item) {
    clearTimeout(cleanupTimer);
    const back = layers[1 - front];
    back.classList.add('instant');
    back.classList.remove('on', 'out');
    back.innerHTML = '';
    return build(item, back);
  }

  // Swap the prepared layer onto the screen.
  function reveal(item, el) {
    const back = layers[1 - front], old = layers[front];
    void back.offsetWidth;                    // commit the starting position
    back.classList.remove('instant');
    empty.style.display = 'none';
    back.classList.add('on');
    old.classList.remove('on');
    old.classList.add('out');
    front = 1 - front;
    cleanupTimer = setTimeout(() => {         // free the old item (stops old web pages running)
      old.classList.add('instant'); old.classList.remove('out'); old.innerHTML = '';
    }, transMs() + 150);
    clearInterval(refreshTimer);
    if (item.type === 'web' && item.refresh_min > 0) {
      refreshTimer = setInterval(() => { if (el.isConnected) el.src = item.url; }, item.refresh_min * 60000);
    }
  }

  // ------------------------------------------------------------ free-running (a single sign)
  function newOrder() {
    order = items.map((_, i) => i);
    if (shuffle && order.length > 2) {
      const last = order.length ? order[order.length - 1] : -1;
      for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
      if (order[0] === last) order.push(order.shift());  // don't show the same item twice in a row
    }
    pos = 0;
  }

  async function freeNext() {
    if (pos >= order.length) newOrder();
    const gen = generation;
    const item = items[order[pos++]];
    let el;
    try { el = await prepare(item); } catch (e) { if (gen === generation) later(freeNext, 2000); return; }
    if (gen !== generation) return;
    reveal(item, el);
    if (items.length > 1) later(freeNext, item.duration * 1000);
    // a single item simply stays on screen indefinitely
  }

  // ------------------------------------------------------------ lockstep (signs in a group)
  function seededOrder(n, seed) {   // same "random" order on every sign for a given loop
    let a = seed >>> 0;
    const rnd = () => { a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
    const o = [...Array(n).keys()];
    for (let i = n - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [o[i], o[j]] = [o[j], o[i]]; }
    return o;
  }

  // Which item should be on screen at time t (ms), and when it ends.
  function syncPos(t) {
    const durs = items.map((i) => i.duration * 1000);
    const cycle = durs.reduce((a, b) => a + b, 0);
    const since = t - sync.epoch_ms;
    const loop = Math.floor(since / cycle);
    let off = since - loop * cycle;
    const ord = shuffle && items.length > 2 ? seededOrder(items.length, loop + 7919) : [...items.keys()];
    for (let k = 0; k < ord.length; k++) {
      const d = durs[ord[k]];
      if (off < d) return { idx: ord[k], key: loop + ':' + k, endsAt: t + (d - off) };
      off -= d;
    }
    return { idx: ord[ord.length - 1], key: loop + ':end', endsAt: t + 1 };
  }

  async function syncStep() {
    const gen = generation, step = ++stepSeq;
    const cur = syncPos(Date.now());
    if (!shown || shown.key !== cur.key) {
      // not showing what the clock says (just started, or fell behind): show it now
      let el;
      try { el = await prepare(items[cur.idx]); } catch (e) { if (gen === generation) later(syncStep, 2000); return; }
      if (gen !== generation || step !== stepSeq) return;
      reveal(items[cur.idx], el);
      shown = { key: cur.key };
    }
    if (items.length < 2) return;
    const now = Date.now();
    const p = syncPos(now);
    const next = syncPos(p.endsAt + 1);
    const untilSwitch = p.endsAt - now;
    let ready = null;
    const load = () => (ready = ready || prepare(items[next.idx]).catch(() => null));
    // load the next item a few seconds early (but not while the last transition is still running)
    later(() => { if (gen === generation && step === stepSeq) load(); },
      Math.min(untilSwitch, Math.max(transMs() + 200, untilSwitch - PRELOAD_MS)));
    later(async () => {
      if (gen !== generation || step !== stepSeq) return;
      const el = await load();
      if (gen !== generation || step !== stepSeq) return;
      if (el) { reveal(items[next.idx], el); shown = { key: next.key }; }
      syncStep();
    }, untilSwitch);
  }

  // ------------------------------------------------------------
  function start(info) {
    generation++;
    clearAll();
    shown = null;
    if (!items.length) { showEmpty(info); return; }
    if (sync) syncStep();
    else { newOrder(); freeNext(); }
  }

  async function poll() {
    try {
      const r = await fetch('/api/now', { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json();
        if (reloadToken !== null && d.reload_token !== reloadToken) { location.reload(); return; }
        reloadToken = d.reload_token;
        showWifi(d.settings && d.settings.show_wifi_ip, d.wifi_ip);
        if (d.version !== version) {
          version = d.version;
          applySettings(d.settings);
          const key = JSON.stringify([d.now.items, d.now.shuffle, d.now.sync]);
          if (key !== itemsKey) {
            itemsKey = key;
            items = d.now.items;
            shuffle = !!d.now.shuffle;
            sync = d.now.sync && d.now.sync.epoch_ms ? d.now.sync : null;
            start(d.info);
          }
        }
      }
    } catch (e) { /* server briefly unavailable: keep showing what we have */ }
    // also check right after each minute ticks over, so scheduled changes happen together on every sign
    const toMinute = 60000 - (Date.now() % 60000) + 250;
    setTimeout(poll, Math.min(POLL_MS, toMinute));
  }

  poll();
})();
