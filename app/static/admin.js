// JoeDeck admin UI (no framework, no build step).
// Works the same on every Pi: calls for *this* Pi go to /api/..., calls for another Pi go to /d/<id>/api/...
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  let ME = null;           // logged-in user + this Pi
  let NET = null;          // {self, devices, found}
  let DEV = null;          // id of the sign being managed
  let S = null;            // that sign's state
  let curPl = null;        // selected playlist id
  let rules = [];          // schedule draft
  let rulesDirty = false;
  let firstRoute = true;

  // ================================================================ utils
  function toast(msg, err = false) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'show' + (err ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.className = ''), err ? 4500 : 2600);
  }

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401 && !url.startsWith('/d/')) { location.href = '/login'; throw new Error('Please log in'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  const isAdmin = () => ME && ME.role === 'admin';
  const prefix = (id) => (id === ME.node.id ? '' : `/d/${id}`);
  const base = () => prefix(DEV);
  let MODE = 'sign', GID = null, GROUPS = [];   // the content editor works on a sign or on a group
  const cbase = () => (MODE === 'group' ? `/api/groups/${GID}` : `${prefix(DEV)}/api`);
  const devInfo = (id) => (NET.devices.find((d) => d.id === id) || { id, name: id, online: false });
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const ago = (ts) => {
    if (!ts) return 'never';
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : fmtTime(ts);
  };
  function ask(dlg) {
    return new Promise((res) => {
      dlg.returnValue = '';
      dlg.showModal();
      dlg.addEventListener('close', () => res(dlg.returnValue === 'ok'), { once: true });
    });
  }
  async function loadNet() { NET = await api('GET', '/api/network'); return NET; }
  const vt = (v) => String(v || '0').replace(/^v/i, '').split('.').map((x) => parseInt(x, 10) || 0);
  const newer = (a, b) => { const x = vt(a), y = vt(b); for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
  let UPD = null;   // latest-release info from GitHub (via this sign)
  async function loadUpdate(force = false) {
    try { UPD = await api(force ? 'POST' : 'GET', force ? '/api/update/check' : '/api/update'); } catch (_) { UPD = null; }
    return UPD;
  }
  const outdated = (d) => UPD && UPD.latest && d.version && newer(UPD.latest, d.version);
  function renderBanner() {
    const old = NET ? NET.devices.filter(outdated) : [];
    $('updBanner').hidden = !(isAdmin() && old.length);
    if (old.length) $('updBannerText').textContent = `JoeDeck v${UPD.latest} is available – ${old.length} sign${old.length === 1 ? '' : 's'} can be updated.`;
  }

  // ================================================================ routing
  async function route() {
    const [view, id] = location.hash.replace(/^#\/?/, '').split('/');
    const v = ['signs', 'sign', 'groups', 'group', 'activity', 'users', 'network'].includes(view) ? view : 'signs';
    document.querySelectorAll('.view').forEach((el) => (el.hidden = true));
    const navv = v === 'sign' ? 'signs' : v === 'group' ? 'groups' : v;
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === navv));
    try {
      await loadNet();
      if (firstRoute && v === 'signs' && NET.devices.length === 1) { firstRoute = false; location.hash = `#/sign/${NET.devices[0].id}`; return; }
      firstRoute = false;
      if (v === 'sign' && id) await showDevice(id);
      else if (v === 'group' && id) await showGroup(id);
      else if (v === 'groups') await showGroups();
      else if (v === 'activity') await showActivity();
      else if (v === 'users' && isAdmin()) await showUsers();
      else if (v === 'network' && isAdmin()) await showNetwork();
      else await showSigns();
    } catch (e) { toast(e.message, true); }
  }
  window.addEventListener('hashchange', () => {
    if (rulesDirty && !confirm('You have unsaved schedule changes. Leave anyway?')) return;
    rulesDirty = false;
    route();
  });

  // ================================================================ signs overview
  async function showSigns() {
    $('v-signs').hidden = false;
    const list = $('signList');
    if (!NET.devices.length) { list.innerHTML = '<div class="empty">You don’t have access to any signs yet. Ask an admin.</div>'; return; }
    list.innerHTML = NET.devices.map((d) => `
      <button class="sign" data-id="${d.id}">
        <div class="pic" id="pic-${d.id}">${d.online ? 'Loading…' : 'Offline'}</div>
        <div class="body">
          <b><span class="dot ${d.online ? 'on' : 'off'}"></span>${esc(d.name)}${d.self ? ' <span class="pill">this Pi</span>' : ''}</b>
          <span id="sum-${d.id}">${d.online ? '&nbsp;' : 'Last seen ' + esc(ago(d.last_seen))}</span>
          <span>${esc(d.ip || '')}</span>
        </div>
      </button>`).join('');
    await Promise.all(NET.devices.filter((d) => d.online).map(async (d) => {
      try {
        const s = await api('GET', prefix(d.id) + '/api/summary');
        $(`pic-${d.id}`).innerHTML = s.first_type === 'web' ? `<div class="webthumb" style="width:100%;height:100%">${GLOBE}<span>website</span></div>`
          : s.thumb ? `<img src="${prefix(d.id)}${s.thumb}" alt="">` : 'Nothing to show yet';
        $(`sum-${d.id}`).textContent = `${s.playlist_name} · ${s.count} item${s.count === 1 ? '' : 's'} · ${s.source}`;
      } catch (e) { $(`pic-${d.id}`).textContent = 'Not reachable'; }
    }));
  }
  $('signList').addEventListener('click', (e) => {
    const b = e.target.closest('.sign'); if (b) location.hash = `#/sign/${b.dataset.id}`;
  });

  // ================================================================ one sign
  function setMode(mode) {
    MODE = mode;
    $('v-device').classList.toggle('group-mode', mode === 'group');
    $('btnBack').textContent = mode === 'group' ? '← All groups' : '← All signs';
    $('btnBack').href = mode === 'group' ? '#/groups' : '#/signs';
  }

  async function showDevice(id) {
    if (DEV !== id || MODE !== 'sign') { curPl = null; rulesDirty = false; }
    setMode('sign');
    DEV = id;
    const d = devInfo(id);
    $('v-device').hidden = false;
    $('devName').textContent = d.name;
    $('devStatus').innerHTML = `<span class="dot ${d.online ? 'on' : 'off'}"></span>${d.online ? 'Online' : 'Offline – last seen ' + esc(ago(d.last_seen))}`;
    $('btnPreview').href = d.self ? '/player' : `http://${d.ip}${d.port && d.port !== 80 ? ':' + d.port : ''}/player`;
    $('btnUpdateSign').hidden = !(isAdmin() && outdated(d));
    if ($('btnUpdateSign').hidden === false) $('btnUpdateSign').textContent = `Update to v${UPD.latest}`;
    await refreshDevice();
    if (isAdmin()) loadWifi();
  }

  async function showGroup(gid) {
    if (GID !== gid || MODE !== 'group') { curPl = null; rulesDirty = false; }
    setMode('group');
    GID = gid;
    DEV = ME.node.id;       // uploads and thumbnails go through this sign (the library is shared anyway)
    $('v-device').hidden = false;
    await refreshDevice();
    const gr = S.group;
    $('devName').textContent = `Group: ${gr.name}`;
    const names = gr.members.map((m) => devInfo(m).name);
    $('devStatus').innerHTML = `${gr.enabled ? '' : '<span class="pill">off</span> '}${names.length} sign${names.length === 1 ? '' : 's'}: ${esc(names.join(', ') || 'none yet')}`;
  }

  async function refreshDevice() {
    S = await api('GET', cbase() + '/state');
    if (!S.playlists.some((p) => p.id === curPl)) curPl = S.default_playlist;
    if (!rulesDirty) rules = structuredClone(S.schedules);
    renderDevice();
  }

  async function act(fn, okMsg) {
    try { await fn(); if (okMsg) toast(okMsg); }
    catch (e) { toast(e.message, true); }
    if (DEV && !$('v-device').hidden) await refreshDevice().catch((e) => toast(e.message, true));
  }

  const mediaById = () => Object.fromEntries(S.media.map((m) => [m.id, m]));
  const thumbUrl = (m) => base() + (m.thumb ? '/thumbs/' + m.thumb : '/media/' + m.file);
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (_) { return u; } };
  const GLOBE = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.8 2.6 15.2 0 18M12 3c-2.6 2.8-2.6 15.2 0 18"/></svg>';
  const thumbHtml = (m, small = false) => (m.type === 'web'
    ? `<div class="webthumb${small ? ' sm' : ''}">${GLOBE}${small ? '' : `<span>${esc(host(m.url))}</span>`}</div>`
    : `<img ${small ? '' : 'loading="lazy"'} src="${thumbUrl(m)}" alt="">`);
  const playlist = () => S.playlists.find((p) => p.id === curPl) || S.playlists[0];
  const fmtDur = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
    : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s}s`);

  function renderDevice() { renderStatus(); renderDisplay(); renderLibrary(); renderPlaylists(); renderRules(); }

  function renderStatus() {
    const n = S.now;
    $('nowLine').innerHTML = `Now showing: <b>${esc(n.playlist_name)}</b> · ${esc(n.source)} · ${n.items.length} item${n.items.length === 1 ? '' : 's'}`;
    $('clock').textContent = S.server_time;
    if (S.device) $('devInfo').textContent = `${S.device.hostname} · ${S.device.ips.join(', ')} · clock ${S.server_time}`;
    const inGroup = MODE === 'sign' && S.group;
    $('groupNotice').hidden = !inGroup;
    if (inGroup) {
      $('groupNoticeText').innerHTML = `This sign is in the group <b>${esc(S.group.name)}</b>, so it shows the group's content, in sync with the other signs in it. The content below is its own and is used again if it leaves the group.`;
      $('groupNoticeLink').href = `#/group/${S.group.id}`;
    }
  }

  function setSeg(id, value) {
    for (const b of $(id).querySelectorAll('button')) b.classList.toggle('on', b.dataset.v === value);
  }

  function renderDisplay() {
    const s = S.settings;
    setSeg('mode', s.mode);
    setSeg('orientation', s.orientation);
    setSeg('fit', s.fit);
    setSeg('transitionStyle', s.transition_style || 'fade');
    const sel = $('singleMedia');
    sel.innerHTML = '<option value="">— pick an item —</option>' +
      S.media.map((m) => `<option value="${m.id}" ${m.id === s.single_media ? 'selected' : ''}>${esc(m.name)}${m.type === 'web' ? ' (website)' : ''}</option>`).join('');
    sel.style.display = s.mode === 'single' ? '' : 'none';
    if (document.activeElement !== $('transition')) $('transition').value = s.transition_ms;
    if (document.activeElement !== $('defaultDuration')) $('defaultDuration').value = s.default_duration;
    $('background').value = s.background || '#000000';
  }

  function renderLibrary() {
    const pl = playlist();
    $('addToPlName').textContent = pl ? pl.name : '';
    const lib = $('library');
    if (!S.media.length) { lib.innerHTML = '<div class="empty" style="grid-column:1/-1">Nothing here yet. Upload images or add a website.</div>'; return; }
    const counts = {};
    for (const p of S.playlists) for (const id of new Set(p.items.map((it) => it.media))) counts[id] = (counts[id] || 0) + 1;
    const mine = (m) => isAdmin() || m.uploader === ME.username;   // who may rename/delete shared items
    lib.innerHTML = S.media.slice().reverse().map((m) => `
      <div class="card" data-id="${m.id}">
        ${thumbHtml(m)}
        <div class="meta" title="${mine(m) ? (m.type === 'web' ? 'Click to edit' : 'Click to rename') : ''}">${esc(m.name)}<br><small>${m.type === 'web' ? `website · ${m.zoom}%` : `${m.width}×${m.height}`}${counts[m.id] ? ` · in ${counts[m.id]} playlist${counts[m.id] > 1 ? 's' : ''}` : ''}${m.local === false ? ' · <i>copying…</i>' : ''}</small></div>
        <div class="acts">
          <button data-a="add" title="Add to “${esc(pl ? pl.name : '')}”">+ Playlist</button>
          ${m.type === 'web' && mine(m) ? '<button data-a="edit">Edit</button>' : ''}
          <button data-a="only">Show only this</button>
          ${mine(m) ? '<button data-a="del" class="danger">Delete</button>' : ''}
        </div>
      </div>`).join('');
  }

  function renderPlaylists() {
    const pl = playlist();
    $('plTabs').innerHTML = S.playlists.map((p) =>
      `<button data-id="${p.id}" class="${p.id === pl.id ? 'on' : ''}">${esc(p.name)} ${p.id === S.default_playlist ? '<span class="pill">default</span>' : ''}</button>`).join('');
    $('plDefault').disabled = pl.id === S.default_playlist;
    $('plDelete').disabled = S.playlists.length <= 1;
    $('plShuffle').checked = !!pl.shuffle;
    const media = mediaById();
    if (!pl.items.length) {
      $('plItems').innerHTML = '<div class="empty">This playlist is empty. Use “+ Playlist” on items in the library above.</div>';
      $('plTotal').textContent = '';
      return;
    }
    $('plItems').innerHTML = pl.items.map((it, i) => {
      const m = media[it.media];
      return `<div class="item" data-i="${i}">
        <span class="num">${i + 1}</span>
        ${thumbHtml(m, true)}
        <span class="n">${esc(m.name)}${m.type === 'web' ? ` <span class="hint">· ${esc(host(m.url))}</span>` : ''}</span>
        ${pl.items.length > 1 ? `<label><input type="number" min="1" step="1" value="${it.duration}" data-a="dur"> s</label>` : '<span class="hint" style="margin:0">shown indefinitely</span>'}
        <button class="icon" data-a="up" ${i === 0 ? 'disabled' : ''} title="Move up">↑</button>
        <button class="icon" data-a="down" ${i === pl.items.length - 1 ? 'disabled' : ''} title="Move down">↓</button>
        <button class="icon danger" data-a="rm" title="Remove from playlist">✕</button>
      </div>`;
    }).join('');
    const total = pl.items.reduce((a, it) => a + Number(it.duration), 0);
    $('plTotal').textContent = pl.items.length > 1 ? `${pl.items.length} items · one full loop takes ${fmtDur(total)}${pl.shuffle ? ' · shuffled' : ''}` : '';
  }

  function renderRules() {
    const plOpts = (sel) => S.playlists.map((p) => `<option value="${p.id}" ${p.id === sel ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
    $('rules').innerHTML = rules.length ? rules.map((r, i) => `
      <div class="rule ${r.enabled ? '' : 'off'}" data-i="${i}">
        <div class="row" style="margin:0">
          <label><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}> On</label>
          <input data-f="name" placeholder="Rule name (e.g. Lunch menu)" value="${esc(r.name)}" style="flex:1;min-width:160px">
          <label>play <select data-f="playlist">${plOpts(r.playlist)}</select></label>
          <button class="icon" data-a="up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="icon" data-a="down" ${i === rules.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="icon danger" data-a="rm">✕</button>
        </div>
        <div class="row chips" style="margin:0">
          ${DAYS.map((d, di) => `<label><input type="checkbox" data-day="${di}" ${r.days.includes(di) ? 'checked' : ''}>${d}</label>`).join('')}
          <button data-a="wk" style="font-size:12px">Weekdays</button><button data-a="all" style="font-size:12px">Every day</button>
        </div>
        <div class="row" style="margin:0">
          <label>from <input type="time" data-f="start" value="${r.start}"></label>
          <label>to <input type="time" data-f="end" value="${r.end}"></label>
          <span class="hint" style="margin:0">Optional dates:</span>
          <label><input type="date" data-f="start_date" value="${r.start_date || ''}"></label>
          <label>– <input type="date" data-f="end_date" value="${r.end_date || ''}"></label>
        </div>
      </div>`).join('') : '<div class="empty">No rules. The default playlist plays all the time.</div>';
    $('ruleDirty').textContent = rulesDirty ? 'Unsaved changes' : '';
  }

  // ---------------------------------------------------------------- display settings
  const saveSettings = (patch, msg = 'Saved') => act(() => api('PUT', cbase() + '/settings', patch), msg);

  for (const id of ['mode', 'orientation', 'fit', 'transitionStyle']) {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      if (id === 'mode' && b.dataset.v === 'single' && !S.settings.single_media) {
        if (!S.media.length) return toast('Add an image or website first', true);
        $('singleMedia').style.display = ''; setSeg('mode', 'single');
        return toast('Now pick what to show');
      }
      saveSettings({ [id === 'transitionStyle' ? 'transition_style' : id]: b.dataset.v });
    });
  }
  $('singleMedia').addEventListener('change', (e) => e.target.value && saveSettings({ mode: 'single', single_media: e.target.value }, 'Showing one item'));
  $('transition').addEventListener('change', (e) => saveSettings({ transition_ms: Number(e.target.value) }));
  $('defaultDuration').addEventListener('change', (e) => saveSettings({ default_duration: Number(e.target.value) }));
  $('background').addEventListener('change', (e) => saveSettings({ background: e.target.value }));
  $('btnReload').addEventListener('click', () => {
    if (MODE === 'group') {   // refresh every screen in the group
      const ids = (S.group.members || []).filter((m) => devInfo(m).online);
      return act(() => Promise.all(ids.map((m) => api('POST', prefix(m) + '/api/reload-display'))), `Refreshing ${ids.length} screen${ids.length === 1 ? '' : 's'}`);
    }
    act(() => api('POST', base() + '/api/reload-display'), 'Screen will refresh in a few seconds');
  });

  // ---------------------------------------------------------------- uploads
  function upload(files) {
    files = [...files].filter((f) => f.type.startsWith('image/') || /\.(jpe?g|png|gif|webp|bmp)$/i.test(f.name));
    if (!files.length) return toast('No image files selected', true);
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f));
    if ($('addToPl').checked) fd.append('playlist', playlist().id);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', cbase() + '/upload');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) $('progress').style.width = (100 * e.loaded / e.total) + '%'; };
    xhr.onload = async () => {
      $('progress').style.width = '0';
      let d = {}; try { d = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status !== 200) toast(d.error || 'Upload failed', true);
      else if (d.errors && d.errors.length) toast(`Uploaded ${d.added.length}; skipped: ${d.errors.join('; ')}`, true);
      else toast(`Uploaded ${d.added.length} image${d.added.length === 1 ? '' : 's'}`);
      await refreshDevice();
    };
    xhr.onerror = () => { $('progress').style.width = '0'; toast('Upload failed', true); };
    toast(`Uploading ${files.length} file${files.length === 1 ? '' : 's'}…`);
    xhr.send(fd);
  }
  $('btnPick').addEventListener('click', () => $('file').click());
  $('file').addEventListener('change', (e) => { upload(e.target.files); e.target.value = ''; });
  const drop = $('drop');
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => upload(e.dataTransfer.files));

  // ---------------------------------------------------------------- library actions
  $('library').addEventListener('click', (e) => {
    const card = e.target.closest('.card'); if (!card) return;
    const id = card.dataset.id;
    const m = mediaById()[id];
    const a = e.target.closest('button')?.dataset.a;
    if (a === 'add') {
      const pl = playlist();
      act(() => api('PUT', `${cbase()}/playlists/${pl.id}`, { items: [...pl.items, { media: id, duration: S.settings.default_duration }] }), `Added to “${pl.name}”`);
    } else if (a === 'only') {
      saveSettings({ mode: 'single', single_media: id }, `Now showing only “${m.name}”`);
    } else if (a === 'del') {
      if (confirm(`Delete “${m.name}”?\n\nThe library is shared, so this removes it from EVERY sign and group, and from all their playlists.`)) act(() => api('DELETE', `${cbase()}/media/${id}`), 'Deleted');
    } else if (e.target.closest('.meta') && !(isAdmin() || m.uploader === ME.username)) {
      return;
    } else if (a === 'edit' || (e.target.closest('.meta') && m.type === 'web')) {
      webDialog(m);
    } else if (e.target.closest('.meta')) {
      const name = prompt('Rename image', m.name);
      if (name && name.trim()) act(() => api('PATCH', `${cbase()}/media/${id}`, { name: name.trim() }), 'Renamed');
    }
  });

  // ---------------------------------------------------------------- playlists
  const savePl = (patch, msg) => act(() => api('PUT', `${cbase()}/playlists/${playlist().id}`, patch), msg);

  $('plTabs').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    curPl = b.dataset.id; renderPlaylists(); renderLibrary();
  });
  $('plNew').addEventListener('click', () => {
    const name = prompt('Name for the new playlist', 'New playlist'); if (!name) return;
    act(async () => { const p = await api('POST', cbase() + '/playlists', { name }); curPl = p.id; }, 'Playlist created');
  });
  $('plRename').addEventListener('click', () => {
    const name = prompt('Rename playlist', playlist().name); if (name) savePl({ name }, 'Renamed');
  });
  $('plDefault').addEventListener('click', () => savePl({ make_default: true }, 'Default playlist changed'));
  $('plDelete').addEventListener('click', () => {
    const pl = playlist();
    if (confirm(`Delete playlist “${pl.name}”? Schedule rules using it are removed too. Images stay in the library.`))
      act(() => api('DELETE', `${cbase()}/playlists/${pl.id}`), 'Playlist deleted');
  });
  $('plShuffle').addEventListener('change', (e) => savePl({ shuffle: e.target.checked }, e.target.checked ? 'Shuffle on' : 'Shuffle off'));

  // ---------------------------------------------------------------- websites
  async function webDialog(m) {
    const editing = !!m;
    $('webTitle').textContent = editing ? `Edit “${m.name}”` : 'Add website';
    $('web_url').value = editing ? m.url : '';
    $('web_name').value = editing ? m.name : '';
    $('web_zoom').value = editing ? m.zoom : 100;
    $('web_refresh').value = editing ? m.refresh_min : 0;
    $('web_addWrap').hidden = editing;
    $('web_plName').textContent = playlist().name;
    if (!(await ask($('dlgWeb')))) return;
    const body = { url: $('web_url').value.trim(), name: $('web_name').value.trim(),
      zoom: Number($('web_zoom').value) || 100, refresh_min: Number($('web_refresh').value) || 0 };
    if (editing) {
      if (!body.name) delete body.name;
      act(() => api('PATCH', `${cbase()}/media/${m.id}`, body), 'Website updated');
    } else {
      if ($('web_add').checked) body.playlist = playlist().id;
      act(() => api('POST', cbase() + '/web', body), 'Website added');
    }
  }
  $('btnAddWeb').addEventListener('click', () => webDialog(null));

  $('plAllDur').addEventListener('click', () => {
    const d = Number($('allDur').value) || 10;
    savePl({ items: playlist().items.map((it) => ({ ...it, duration: d })) }, `All set to ${d}s`);
  });
  $('plItems').addEventListener('click', (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const i = Number(e.target.closest('.item').dataset.i);
    const items = playlist().items.slice();
    if (a === 'rm') items.splice(i, 1);
    if (a === 'up') [items[i - 1], items[i]] = [items[i], items[i - 1]];
    if (a === 'down') [items[i + 1], items[i]] = [items[i], items[i + 1]];
    savePl({ items });
  });
  $('plItems').addEventListener('change', (e) => {
    if (e.target.dataset.a !== 'dur') return;
    const i = Number(e.target.closest('.item').dataset.i);
    const items = playlist().items.map((it, j) => (j === i ? { ...it, duration: Math.max(1, Number(e.target.value) || 1) } : it));
    savePl({ items }, 'Duration saved');
  });

  // ---------------------------------------------------------------- schedule
  const markDirty = () => { rulesDirty = true; $('ruleDirty').textContent = 'Unsaved changes'; };
  $('ruleAdd').addEventListener('click', () => {
    rules.push({ name: '', playlist: S.playlists[0].id, days: [0, 1, 2, 3, 4], start: '08:00', end: '17:00',
      start_date: null, end_date: null, enabled: true });
    markDirty(); renderRules();
  });
  $('rules').addEventListener('input', (e) => {
    const el = e.target, i = Number(el.closest('.rule')?.dataset.i); if (Number.isNaN(i)) return;
    const r = rules[i];
    if (el.dataset.day !== undefined) {
      const d = Number(el.dataset.day);
      r.days = el.checked ? [...new Set([...r.days, d])].sort() : r.days.filter((x) => x !== d);
    } else if (el.dataset.f === 'enabled') { r.enabled = el.checked; el.closest('.rule').classList.toggle('off', !r.enabled); }
    else if (el.dataset.f) r[el.dataset.f] = el.value || (el.type === 'date' ? null : el.value);
    markDirty();
  });
  $('rules').addEventListener('click', (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const i = Number(e.target.closest('.rule').dataset.i);
    if (a === 'rm') rules.splice(i, 1);
    if (a === 'up') [rules[i - 1], rules[i]] = [rules[i], rules[i - 1]];
    if (a === 'down') [rules[i + 1], rules[i]] = [rules[i], rules[i + 1]];
    if (a === 'wk') rules[i].days = [0, 1, 2, 3, 4];
    if (a === 'all') rules[i].days = [0, 1, 2, 3, 4, 5, 6];
    markDirty(); renderRules();
  });
  $('ruleSave').addEventListener('click', () => act(async () => {
    for (const r of rules) if (!r.days.length) throw new Error('Each rule needs at least one day');
    await api('PUT', cbase() + '/schedules', { schedules: rules });
    rulesDirty = false;
  }, 'Schedule saved'));
  window.addEventListener('beforeunload', (e) => { if (rulesDirty) { e.preventDefault(); e.returnValue = ''; } });

  // ---------------------------------------------------------------- this sign (admin)
  $('btnRenameSign').addEventListener('click', () => {
    const name = prompt('Name for this sign (e.g. Lobby, Cafeteria)', devInfo(DEV).name); if (!name) return;
    act(async () => { await api('PUT', `/api/network/devices/${DEV}`, { name }); await loadNet(); $('devName').textContent = name; }, 'Sign renamed');
  });
  $('btnReboot').addEventListener('click', () => {
    if (confirm(`Reboot “${devInfo(DEV).name}” now? It will be back in about a minute.`))
      api('POST', base() + '/api/reboot').then(() => toast('Rebooting…')).catch((e) => toast(e.message, true));
  });
  $('btnRemoveSign').addEventListener('click', () => removeSign(DEV));

  async function removeSign(id) {
    const d = devInfo(id);
    if (d.self) return toast('Open the admin page on a different sign to remove this one', true);
    if (!confirm(`Remove “${d.name}” from your network?\n\nIt keeps showing its images, but it will no longer appear here and stops sharing users. You can add it back later from the Network page.`)) return;
    try { await api('DELETE', `/api/network/devices/${id}`); toast('Sign removed'); location.hash = '#/network'; route(); }
    catch (e) { toast(e.message, true); }
  }

  // keep the "now showing" line fresh while a sign is open
  setInterval(async () => {
    if ($('v-device').hidden || !DEV || MODE === 'group') return;
    try { const s = await api('GET', base() + '/api/summary'); S.server_time = s.server_time;
      $('nowLine').innerHTML = `Now showing: <b>${esc(s.playlist_name)}</b> · ${esc(s.source)} · ${s.count} image${s.count === 1 ? '' : 's'}`; } catch (_) {}
  }, 20000);

  // ---------------------------------------------------------------- Wi-Fi (admin)
  async function loadWifi() {
    const dev = DEV;
    $('wifiStatus').textContent = 'Loading…';
    $('wifiSaved').innerHTML = '';
    try {
      const w = await api('GET', base() + '/api/wifi');
      if (dev !== DEV) return;
      const st = w.status;
      $('wifiStatus').innerHTML = st.ssid ? `Connected to <b>${esc(st.ssid)}</b>${st.signal != null ? ` · signal ${st.signal}%` : ''}${st.wired ? ' · also on wired Ethernet' : ''}`
        : st.wired ? 'Using wired Ethernet (no Wi-Fi connected).' : st.wifi_device ? 'Not connected to Wi-Fi.' : 'This device has no Wi-Fi adapter.';
      $('wifiSaved').innerHTML = w.saved.length ? w.saved.map((n) => `<div class="item" data-ssid="${esc(n.ssid)}">
        <span class="n"><b>${esc(n.ssid)}</b> ${n.active ? '<span class="pill">connected</span>' : ''}</span>
        ${n.active ? '' : '<button data-a="connect">Switch to this</button>'}
        <button data-a="forget" class="danger">Remove</button></div>`).join('') : '<div class="empty">No saved Wi-Fi networks.</div>';
    } catch (e) { if (dev === DEV) $('wifiStatus').textContent = e.message; }
  }
  $('wifiRefresh').addEventListener('click', loadWifi);
  $('wifiScan').addEventListener('click', async () => {
    toast('Scanning…');
    try {
      const nets = await api('GET', base() + '/api/wifi/scan');
      $('wifiScanList').innerHTML = nets.map((n) => `<option value="${esc(n.ssid)}">${n.signal}%${n.secure ? '' : ' (open)'}</option>`).join('');
      toast(`Found ${nets.length} network${nets.length === 1 ? '' : 's'} – pick one in the name box`);
      $('wifiSsid').focus();
    } catch (e) { toast(e.message, true); }
  });
  $('wifiAdd').addEventListener('click', async () => {
    const body = { ssid: $('wifiSsid').value.trim(), password: $('wifiPw').value, hidden: $('wifiHidden').checked, connect: $('wifiConnect').checked };
    if (!body.ssid) return toast('Enter the network name', true);
    if (body.connect && !confirm(`Switch “${devInfo(DEV).name}” to “${body.ssid}” now?\n\nIf it can't connect, it goes back to its current network. If it works, the sign may get a new address and disappear from this page for up to a minute.`)) return;
    toast(body.connect ? 'Connecting… (up to 30 seconds)' : 'Saving…');
    try {
      await api('POST', base() + '/api/wifi/networks', body);
      toast(body.connect ? `Connected to ${body.ssid}` : `Saved ${body.ssid}`);
      $('wifiPw').value = ''; $('wifiSsid').value = '';
    } catch (e) { toast(e.message, true); }
    loadWifi();
  });
  $('wifiSaved').addEventListener('click', async (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const ssid = e.target.closest('.item').dataset.ssid;
    if (a === 'forget' && !confirm(`Remove the saved network “${ssid}” from this sign?`)) return;
    if (a === 'connect' && !confirm(`Switch this sign to “${ssid}” now? If it fails it goes back to the current network.`)) return;
    toast(a === 'connect' ? 'Connecting…' : 'Removing…');
    try {
      await api('POST', base() + (a === 'connect' ? '/api/wifi/connect' : '/api/wifi/delete'), { ssid });
      toast(a === 'connect' ? `Connected to ${ssid}` : 'Removed');
    } catch (err) { toast(err.message, true); }
    loadWifi();
  });

  // ---------------------------------------------------------------- updates (admin)
  async function updateSigns(list) {
    if (!list.length) return;
    const names = list.map((d) => d.name).join(', ');
    if (!confirm(`Update ${list.length === 1 ? `“${names}”` : `${list.length} signs (${names})`} to v${UPD.latest}?\n\nEach sign downloads the update, installs it and restarts. Its screen goes dark for a few minutes. Content and settings are kept.`)) return;
    // this sign last, so the page keeps working while it sends the others
    list = [...list].sort((a, b) => (a.self === b.self ? 0 : a.self ? 1 : -1));
    const failed = [];
    for (const d of list) {
      try { await api('POST', prefix(d.id) + '/api/update/apply'); } catch (e) { failed.push(`${d.name} (${e.message})`); }
    }
    toast(failed.length ? `Couldn't start on: ${failed.join('; ')}` : `Updating ${list.length} sign${list.length === 1 ? '' : 's'}. They'll be back in a few minutes.`, !!failed.length);
  }
  $('btnUpdateSign').addEventListener('click', () => updateSigns([devInfo(DEV)]));
  $('updCheck').addEventListener('click', async () => { toast('Checking GitHub…'); await loadUpdate(true); renderUpdates(); renderBanner(); showNetwork(); });
  $('updAll').addEventListener('click', () => updateSigns(NET.devices.filter((d) => d.online && outdated(d))));
  function renderUpdates() {
    if (!UPD) { $('updInfo').textContent = 'Update information is unavailable.'; $('updAll').hidden = true; return; }
    const old = NET.devices.filter(outdated);
    const checked = UPD.checked ? `Last checked ${ago(UPD.checked)}.` : 'Not checked yet.';
    if (UPD.error) $('updInfo').textContent = `${UPD.error}. ${checked}`;
    else if (!UPD.latest) $('updInfo').textContent = checked;
    else $('updInfo').innerHTML = `Latest version: <b>v${esc(UPD.latest)}</b>${UPD.url ? ` (<a href="${esc(UPD.url)}" target="_blank" rel="noopener">what's new</a>)` : ''}. ` +
      (old.length ? `${old.length} sign${old.length === 1 ? ' is' : 's are'} out of date.` : 'All signs are up to date.') + ` ${checked}`;
    $('updAll').hidden = !old.filter((d) => d.online).length;
  }

  // ---------------------------------------------------------------- Wi-Fi on all signs (admin)
  $('allWifiSave').addEventListener('click', async () => {
    const body = { ssid: $('allWifiSsid').value.trim(), password: $('allWifiPw').value, hidden: $('allWifiHidden').checked };
    if (!body.ssid) return toast('Enter the network name', true);
    const online = NET.devices.filter((d) => d.online), offline = NET.devices.filter((d) => !d.online);
    if (!confirm(`Save Wi-Fi network “${body.ssid}” on ${online.length} online sign${online.length === 1 ? '' : 's'}?`)) return;
    $('allWifiResult').textContent = 'Saving…';
    const results = await Promise.all(online.map((d) => api('POST', prefix(d.id) + '/api/wifi/networks', body)
      .then(() => null).catch((e) => `${d.name}: ${e.message}`)));
    const failed = results.filter(Boolean);
    $('allWifiResult').textContent = `Saved on ${online.length - failed.length} of ${NET.devices.length} signs.` +
      (failed.length ? ` Failed – ${failed.join('; ')}.` : '') + (offline.length ? ` Offline (not changed): ${offline.map((d) => d.name).join(', ')}.` : '');
    $('allWifiPw').value = '';
  });

  // ================================================================ groups
  const groupOf = (devId, except) => GROUPS.find((gr) => gr.id !== except && gr.members.includes(devId));

  async function showGroups() {
    $('v-groups').hidden = false;
    GROUPS = await api('GET', '/api/groups');
    $('groupAdd').hidden = !isAdmin();
    $('groupList').innerHTML = GROUPS.length ? GROUPS.map((gr) => `
      <div class="item" data-id="${gr.id}">
        <span class="n" style="white-space:normal;min-width:200px"><b>${esc(gr.name)}</b> ${gr.enabled ? '' : '<span class="pill">off</span>'}<br>
          <span class="hint">${gr.members.length ? gr.members.map((m) => `<span class="dot ${devInfo(m).online ? 'on' : 'off'}"></span>${esc(devInfo(m).name)}`).join(' &nbsp; ') : 'No signs yet'}</span></span>
        <button data-a="content" class="primary">Edit content</button>
        <button data-a="restart" title="Start the slideshow from the first item on every sign, right now">Restart together</button>
        ${isAdmin() ? `<button data-a="edit">Name &amp; signs</button>
          <button data-a="toggle">${gr.enabled ? 'Turn off' : 'Turn on'}</button>
          <button data-a="del" class="danger">Delete</button>` : ''}
      </div>`).join('')
      : `<div class="empty">No groups yet.${isAdmin() ? ' Click “+ New group” to make one.' : ''}</div>`;
  }

  async function groupDialog(gr) {
    const editing = !!gr;
    $('grTitle').textContent = editing ? `Edit “${gr.name}”` : 'New group';
    $('gr_name').value = editing ? gr.name : '';
    const sel = new Set(editing ? gr.members : []);
    $('gr_members').innerHTML = NET.devices.map((d) => {
      const other = groupOf(d.id, editing ? gr.id : null);
      return `<label title="${other ? esc(`Currently in “${other.name}”`) : ''}"><input type="checkbox" value="${d.id}" ${sel.has(d.id) ? 'checked' : ''}>${esc(d.name)}${other ? ` <small>(${esc(other.name)})</small>` : ''}</label>`;
    }).join('') || '<span class="hint">No signs yet.</span>';
    $('gr_moveNote').textContent = GROUPS.length ? 'Ticking a sign that’s in another group moves it into this one.' : '';
    $('gr_copyWrap').hidden = editing;
    $('gr_copy').innerHTML = '<option value="">Empty playlists</option>' +
      NET.devices.filter((d) => d.online).map((d) => `<option value="${d.id}">A copy of ${esc(d.name)}’s playlists &amp; schedule</option>`).join('');
    if (!(await ask($('dlgGroup')))) return;
    const body = { name: $('gr_name').value.trim(), members: [...$('gr_members').querySelectorAll('input:checked')].map((i) => i.value) };
    try {
      if (editing) { await api('PUT', `/api/groups/${gr.id}`, body); toast('Group saved'); showGroups(); }
      else {
        if ($('gr_copy').value) body.copy_from = $('gr_copy').value;
        const r = await api('POST', '/api/groups', body);
        toast('Group created – now add its content');
        location.hash = `#/group/${r.id}`;
      }
    } catch (e) { toast(e.message, true); }
  }
  $('groupAdd').addEventListener('click', () => groupDialog(null));
  $('groupList').addEventListener('click', async (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const gr = GROUPS.find((x) => x.id === e.target.closest('.item').dataset.id);
    try {
      if (a === 'content') location.hash = `#/group/${gr.id}`;
      if (a === 'edit') await groupDialog(gr);
      if (a === 'restart') { await api('POST', `/api/groups/${gr.id}/restart`); toast('Restarting on all signs in the group'); }
      if (a === 'toggle') {
        await api('PUT', `/api/groups/${gr.id}`, { enabled: !gr.enabled });
        toast(gr.enabled ? 'Group off – its signs show their own content' : 'Group on'); showGroups();
      }
      if (a === 'del' && confirm(`Delete the group “${gr.name}”?\n\nIts signs go back to their own content. Items in the media library are not deleted.`)) {
        await api('DELETE', `/api/groups/${gr.id}`); toast('Group deleted'); showGroups();
      }
    } catch (err) { toast(err.message, true); }
  });
  $('btnRestartGroup').addEventListener('click', () => act(() => api('POST', `/api/groups/${GID}/restart`), 'Restarting on all signs in the group'));

  // ================================================================ activity log
  let LOG = [], logOffline = [];
  async function showActivity() {
    $('v-activity').hidden = false;
    $('logRows').innerHTML = '<tr><td colspan="5" class="hint">Loading…</td></tr>';
    const online = NET.devices.filter((d) => d.online);
    logOffline = NET.devices.filter((d) => !d.online).map((d) => d.name);
    const results = await Promise.all(online.map((d) => api('GET', prefix(d.id) + '/api/log?limit=1000').catch(() => { logOffline.push(d.name); return []; })));
    LOG = results.flat().sort((a, b) => b.ts - a.ts);
    const devSel = $('logDev'), userSel = $('logUser');
    const keepD = devSel.value, keepU = userSel.value;
    devSel.innerHTML = '<option value="">All signs</option>' + NET.devices.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
    const users = [...new Set(LOG.map((e) => e.user))].sort();
    userSel.innerHTML = '<option value="">All users</option>' + users.map((u) => `<option>${esc(u)}</option>`).join('');
    devSel.value = keepD; userSel.value = users.includes(keepU) ? keepU : '';
    renderLog();
  }
  function renderLog() {
    const dv = $('logDev').value, us = $('logUser').value, q = $('logText').value.trim().toLowerCase();
    const rows = LOG.filter((e) => (!dv || e.device === dv) && (!us || e.user === us) &&
      (!q || `${e.action} ${e.detail} ${e.device_name} ${e.user}`.toLowerCase().includes(q)));
    $('logNote').textContent = (logOffline.length ? `Not included (offline): ${logOffline.join(', ')}. ` : '') +
      `${rows.length} event${rows.length === 1 ? '' : 's'}${rows.length > 1000 ? ', showing newest 1000' : ''}.`;
    $('logRows').innerHTML = rows.slice(0, 1000).map((e) => `<tr>
      <td class="t">${esc(fmtTime(e.ts))}</td>
      <td>${esc(e.device_name)}</td>
      <td>${esc(e.user)}${e.via ? `<br><span class="hint">via ${esc(e.via)}</span>` : ''}</td>
      <td>${esc(e.action)}</td>
      <td>${esc(e.detail)}${e.ip ? `<br><span class="hint">${esc(e.ip)}</span>` : ''}</td></tr>`).join('') ||
      '<tr><td colspan="5" class="hint">No activity yet.</td></tr>';
  }
  ['logDev', 'logUser'].forEach((id) => $(id).addEventListener('change', renderLog));
  $('logText').addEventListener('input', renderLog);
  $('logRefresh').addEventListener('click', () => loadNet().then(showActivity).catch((e) => toast(e.message, true)));

  // ================================================================ users (admin)
  let USERS = [];
  async function showUsers() {
    $('v-users').hidden = false;
    USERS = await api('GET', '/api/users');
    const devName = (id) => (id === '*' ? 'all signs' : devInfo(id).name);
    $('userRows').innerHTML = USERS.map((u) => `<tr data-u="${esc(u.username)}">
      <td><b>${esc(u.username)}</b>${u.username === ME.username ? ' <span class="pill">you</span>' : ''}</td>
      <td>${u.role === 'admin' ? 'Admin' : 'Editor'}</td>
      <td>${u.role === 'admin' ? 'Everything' : (u.devices.length ? u.devices.map((d) => esc(devName(d))).join(', ') : '<span class="hint">no signs yet</span>')}</td>
      <td style="text-align:right;white-space:nowrap"><button data-a="edit">Edit</button> ${u.username === ME.username ? '' : '<button data-a="del" class="danger">Delete</button>'}</td></tr>`).join('');
  }

  async function userDialog(u) {
    const editing = !!u;
    $('userFormTitle').textContent = editing ? `Edit ${u.username}` : 'Add user';
    $('uf_name').value = editing ? u.username : '';
    $('uf_name').disabled = editing;
    $('uf_pw').value = '';
    $('uf_pwLabel').textContent = editing ? 'New password (leave empty to keep)' : 'Password (min 6 characters)';
    $('uf_role').value = editing ? u.role : 'editor';
    const sel = new Set(editing ? u.devices : []);
    $('uf_devs').innerHTML = `<label><input type="checkbox" value="*" ${sel.has('*') ? 'checked' : ''}>All signs (including future ones)</label>` +
      NET.devices.map((d) => `<label><input type="checkbox" value="${d.id}" ${sel.has(d.id) ? 'checked' : ''}>${esc(d.name)}</label>`).join('');
    const syncRole = () => ($('uf_devWrap').hidden = $('uf_role').value === 'admin');
    $('uf_role').onchange = syncRole; syncRole();
    if (!(await ask($('dlgUser')))) return;
    const body = { role: $('uf_role').value, devices: [...$('uf_devs').querySelectorAll('input:checked')].map((i) => i.value) };
    if ($('uf_pw').value) body.password = $('uf_pw').value;
    try {
      if (editing) await api('PUT', `/api/users/${encodeURIComponent(u.username)}`, body);
      else await api('POST', '/api/users', { ...body, username: $('uf_name').value.trim().toLowerCase() });
      toast(editing ? 'User updated' : 'User added');
    } catch (e) { toast(e.message, true); }
    showUsers();
  }
  $('userAdd').addEventListener('click', () => userDialog(null));
  $('userRows').addEventListener('click', async (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const u = USERS.find((x) => x.username === e.target.closest('tr').dataset.u);
    if (a === 'edit') return userDialog(u);
    if (a === 'del' && confirm(`Delete user “${u.username}”? They will be logged out on every sign.`)) {
      try { await api('DELETE', `/api/users/${encodeURIComponent(u.username)}`); toast('User deleted'); } catch (err) { toast(err.message, true); }
      showUsers();
    }
  });

  // ================================================================ network (admin)
  async function showNetwork() {
    $('v-network').hidden = false;
    $('memberRows').innerHTML = NET.devices.map((d) => `<tr data-id="${d.id}">
      <td><b>${esc(d.name)}</b>${d.self ? ' <span class="pill">this Pi</span>' : ''}</td>
      <td>${esc(d.ip || '?')}${d.port && d.port !== 80 ? ':' + d.port : ''}</td>
      <td><span class="dot ${d.online ? 'on' : 'off'}"></span>${d.online ? 'Online' : 'Offline · ' + esc(ago(d.last_seen))}</td>
      <td>${d.version ? 'v' + esc(d.version) : '<span class="hint">?</span>'}${outdated(d) ? ' <span class="pill">update available</span>' : ''}</td>
      <td style="text-align:right;white-space:nowrap">${outdated(d) && d.online ? '<button data-a="update" class="primary">Update</button> ' : ''}<button data-a="open">Manage</button> <button data-a="rename">Rename</button>
        ${d.self ? '' : '<button data-a="remove" class="danger">Remove</button>'}</td></tr>`).join('');
    $('foundList').innerHTML = (NET.found || []).length ? NET.found.map((f) => `
      <div class="item" data-id="${f.id}"><span class="n"><b>${esc(f.name)}</b> <span class="hint">${esc(f.ip)}${f.port !== 80 ? ':' + f.port : ''} · seen ${esc(ago(f.last_seen))}</span></span>
      <button class="primary" data-a="adopt">Add to my network</button></div>`).join('')
      : '<div class="empty">No other JoeDeck devices found right now. New Pis appear here within about 10 seconds of starting up.</div>';
    renderUpdates();
    renderBanner();
  }
  $('memberRows').addEventListener('click', async (e) => {
    const a = e.target.closest('button')?.dataset.a; if (!a) return;
    const id = e.target.closest('tr').dataset.id, d = devInfo(id);
    if (a === 'open') location.hash = `#/sign/${id}`;
    if (a === 'remove') removeSign(id);
    if (a === 'update') updateSigns([d]);
    if (a === 'rename') {
      const name = prompt('New name for this sign', d.name); if (!name) return;
      try { await api('PUT', `/api/network/devices/${id}`, { name }); toast('Renamed'); } catch (err) { toast(err.message, true); }
      await loadNet(); showNetwork();
    }
  });
  $('foundList').addEventListener('click', async (e) => {
    if (e.target.closest('button')?.dataset.a !== 'adopt') return;
    const id = e.target.closest('.item').dataset.id;
    const f = NET.found.find((x) => x.id === id);
    $('adoptName').textContent = f.name; $('ad_pw').value = '';
    if (!(await ask($('dlgAdopt')))) return;
    toast(`Adding ${f.name}…`);
    try {
      await api('POST', '/api/network/adopt', { id, username: $('ad_user').value, password: $('ad_pw').value });
      toast(`“${f.name}” joined your network`);
    } catch (err) { toast(err.message, true); }
    await loadNet(); showNetwork();
  });
  $('probeBtn').addEventListener('click', async () => {
    const ip = $('probeIp').value.trim(); if (!ip) return;
    const [host, port] = ip.split(':');
    try {
      const info = await api('POST', '/api/network/probe', { ip: host, port: Number(port) || 80 });
      toast(info.cluster ? `Found “${info.name}”` : 'Found it');
    } catch (err) { toast(err.message, true); }
    await loadNet(); showNetwork();
  });
  setInterval(() => { if (!$('v-network').hidden) loadNet().then(showNetwork).catch(() => {}); }, 10000);

  // ================================================================ account
  $('btnLogout').addEventListener('click', async () => { await api('POST', '/api/logout').catch(() => {}); location.href = '/login'; });
  $('btnMyPw').addEventListener('click', async () => {
    $('pw_cur').value = ''; $('pw_new').value = '';
    if (!(await ask($('dlgPw')))) return;
    try { await api('POST', '/api/password', { current: $('pw_cur').value, password: $('pw_new').value }); toast('Password changed on all signs'); }
    catch (e) { toast(e.message, true); }
  });

  // ================================================================ start
  (async () => {
    try {
      ME = await api('GET', '/api/me');
      $('appVersion').textContent = 'v' + ME.version;
      $('whoName').innerHTML = `<b>${esc(ME.username)}</b> (${ME.role})`;
      document.querySelectorAll('[data-admin]').forEach((el) => (el.hidden = !isAdmin()));
      if (isAdmin()) await loadUpdate();
      await route();
      if (isAdmin()) renderBanner();
    } catch (e) { toast('Could not load: ' + e.message, true); }
  })();
})();
