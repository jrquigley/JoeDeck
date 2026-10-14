// JoeDeck player: polls /api/now and runs the slideshow of images and websites.
(() => {
  const POLL_MS = 5000;
  const WEB_LOAD_TIMEOUT = 8000;   // show a website after this long even if it hasn't finished loading
  const stage = document.getElementById('stage');
  const layers = [document.getElementById('a'), document.getElementById('b')];
  const empty = document.getElementById('empty');
  let front = 0;
  let version = null, itemsKey = null, reloadToken = null;
  let items = [], order = [], pos = 0;
  let timer = null, refreshTimer = null, cleanupTimer = null;
  let generation = 0;
  let settings = { transition_ms: 800, transition_style: 'fade', fit: 'contain' };
  let shuffle = false;

  function applySettings(s) {
    settings = s;
    document.body.className = s.orientation || 'landscape';
    document.body.style.background = s.background || '#000';
    stage.style.background = s.background || '#000';
    stage.classList.toggle('slide', s.transition_style === 'slide');
    const ms = s.transition_style === 'none' ? 0 : Number(s.transition_ms) || 0;
    for (const l of layers) l.style.transitionDuration = ms + 'ms';
    for (const img of stage.querySelectorAll('.layer img')) img.style.objectFit = s.fit === 'cover' ? 'cover' : 'contain';
  }

  function showEmpty(info) {
    for (const l of layers) { l.classList.remove('on', 'out'); l.innerHTML = ''; }
    const port = info.port && info.port !== '80' ? ':' + info.port : '';
    const hosts = [info.hostname + '.local', ...(info.ips || [])];
    document.querySelector('#empty h1').textContent = `“${info.name}” is running`;
    document.getElementById('urls').innerHTML = hosts.map((h) => `<p><code>http://${h}${port}/</code></p>`).join('');
    empty.style.display = 'flex';
  }

  // Prepare an item inside `container` (a hidden layer) and resolve once it's ready to show.
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

  function newOrder() {
    order = items.map((_, i) => i);
    if (shuffle && order.length > 2) {
      const last = order.length ? order[order.length - 1] : -1;
      for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
      if (order[0] === last) order.push(order.shift());  // don't show the same item twice in a row
    }
    pos = 0;
  }

  async function showNext() {
    clearTimeout(timer); clearInterval(refreshTimer); clearTimeout(cleanupTimer);
    if (pos >= order.length) newOrder();
    const gen = generation;
    const item = items[order[pos++]];
    const back = layers[1 - front], old = layers[front];

    // Prepare the incoming layer at its starting position, without animating.
    back.classList.add('instant');
    back.classList.remove('on', 'out');
    back.innerHTML = '';
    let el;
    try {
      el = await build(item, back);
    } catch (e) {
      if (gen !== generation) return;
      timer = setTimeout(showNext, 2000);   // skip a broken item
      return;
    }
    if (gen !== generation) return;
    void back.offsetWidth;                    // commit the starting position
    back.classList.remove('instant');
    empty.style.display = 'none';
    back.classList.add('on');
    old.classList.remove('on');
    old.classList.add('out');
    front = 1 - front;
    const ms = settings.transition_style === 'none' ? 0 : Number(settings.transition_ms) || 0;
    cleanupTimer = setTimeout(() => {           // free the old item (stops old web pages running)
      old.classList.add('instant'); old.classList.remove('out'); old.innerHTML = '';
    }, ms + 150);

    if (item.type === 'web' && item.refresh_min > 0) {
      refreshTimer = setInterval(() => { if (el.isConnected) el.src = item.url; }, item.refresh_min * 60000);
    }
    if (items.length > 1) timer = setTimeout(showNext, item.duration * 1000);
    // a single item simply stays on screen indefinitely
  }

  function start(info) {
    generation++;
    clearTimeout(timer); clearInterval(refreshTimer);
    if (!items.length) { showEmpty(info); return; }
    newOrder();
    showNext();
  }

  async function poll() {
    try {
      const r = await fetch('/api/now', { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json();
        if (reloadToken !== null && d.reload_token !== reloadToken) { location.reload(); return; }
        reloadToken = d.reload_token;
        if (d.version !== version) {
          version = d.version;
          applySettings(d.settings);
          const key = JSON.stringify([d.now.items, d.now.shuffle]);
          if (key !== itemsKey) {
            itemsKey = key;
            items = d.now.items;
            shuffle = !!d.now.shuffle;
            start(d.info);
          }
        }
      }
    } catch (e) { /* server briefly unavailable: keep showing what we have */ }
    setTimeout(poll, POLL_MS);
  }

  poll();
})();
