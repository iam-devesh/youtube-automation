/**
 * app.js — The Faceless Studio (dashboard frontend)
 * Handles Story Studio, Quick AI mode, job polling, and all interactions.
 */

const API = 'http://localhost:5055';

// ─── State ────────────────────────────────────────────────────────────────────
let videoType = 'short';
let studioType = 'short';
let studioLang = 'hi';
let currentScript = null;
let pollingIntervals = {};
let openJobId = null;
let sceneCounter = 0;

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadChannelStats();
  loadJobs();
  setInterval(loadJobs, 10000);

  // Start with 3 empty scenes in Story Studio
  addScene('', '', false);
  addScene('', '', false);
  addScene('', '', false);

  // open the tab named in the URL (#series, #jobs …) — a refresh keeps you on the same tab
  const openFromHash = () => {
    const id = location.hash.slice(1);
    const nav = document.querySelector(`.nav-item[data-section="${id}"]`);
    if (nav && document.getElementById(`section-${id}`)) showSection(id, nav);
  };
  openFromHash();
  window.addEventListener('hashchange', openFromHash);
});

// ─── Mode Switching ───────────────────────────────────────────────────────────
function switchMode(mode, btn) {
  document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');

  document.getElementById('mode-studio').style.display = mode === 'studio' ? 'block' : 'none';
  document.getElementById('mode-quick').style.display = mode === 'quick' ? 'block' : 'none';
}

// ─── Section Navigation ───────────────────────────────────────────────────────
function showSection(sectionId, navEl) {
  document.querySelectorAll('.section').forEach(s => s.style.display = 'none');
  document.getElementById(`section-${sectionId}`).style.display = 'flex';

  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  if (navEl) navEl.classList.add('active');
  if (location.hash.slice(1) !== sectionId) history.replaceState(null, '', `#${sectionId}`);

  const titles = {
    create: 'Create Video',
    series: 'Series',
    jobs: 'Job Queue',
    analytics: 'Analytics',
    settings: 'Settings',
  };
  document.getElementById('pageTitle').textContent = titles[sectionId] || sectionId;

  if (sectionId === 'analytics') loadChannelStats();
  if (sectionId === 'jobs') loadJobs();
  if (sectionId === 'series') loadSeries();

  return false;
}

// ─── Studio Type & Language Toggles ───────────────────────────────────────────
function setStudioType(type, btn) {
  studioType = type;
  document.querySelectorAll('#studioTypeToggle .toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function setLang(lang, btn) {
  studioLang = lang;
  document.querySelectorAll('#langToggle .toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ─── Scene Editor ─────────────────────────────────────────────────────────────
function addScene(narration = '', visual = '', focus = true) {
  sceneCounter++;
  const editor = document.getElementById('scenesEditor');
  const sceneEl = document.createElement('div');
  sceneEl.className = 'scene-editor-card fade-in';
  sceneEl.id = `scene-${sceneCounter}`;
  sceneEl.dataset.sceneId = sceneCounter;

  const sceneNum = editor.children.length + 1;

  sceneEl.innerHTML = `
    <div class="scene-editor-header">
      <div class="scene-num-badge">${sceneNum}</div>
      <div class="scene-editor-title">Scene ${sceneNum}</div>
      <button class="scene-delete-btn" onclick="removeScene(${sceneCounter})" title="Remove scene">✕</button>
    </div>
    <div class="scene-editor-body">
      <div class="scene-field">
        <label class="scene-field-label">
          <span class="scene-field-icon">🎙️</span> Narration
        </label>
        <textarea
          class="scene-textarea narration-input"
          rows="2"
          placeholder="Write what the narrator will say in this scene..."
          data-scene="${sceneCounter}"
        >${escHtml(narration)}</textarea>
      </div>
      <div class="scene-field">
        <label class="scene-field-label">
          <span class="scene-field-icon">🎨</span> Visual Description
          <span class="scene-field-hint">(What the AI animation should show)</span>
        </label>
        <textarea
          class="scene-textarea visual-input"
          rows="2"
          placeholder="e.g. A massive ship sailing through a violent storm at night, with lightning illuminating dark clouds..."
          data-scene="${sceneCounter}"
        >${escHtml(visual)}</textarea>
      </div>
    </div>
  `;

  editor.appendChild(sceneEl);
  if (focus) sceneEl.querySelector('.narration-input').focus();
  renumberScenes();
}

function removeScene(sceneId) {
  const el = document.getElementById(`scene-${sceneId}`);
  if (el) {
    el.style.animation = 'fadeOut 0.2s ease forwards';
    setTimeout(() => {
      el.remove();
      renumberScenes();
    }, 200);
  }
}

function renumberScenes() {
  const cards = document.querySelectorAll('.scene-editor-card');
  cards.forEach((card, i) => {
    const num = i + 1;
    card.querySelector('.scene-num-badge').textContent = num;
    card.querySelector('.scene-editor-title').textContent = `Scene ${num}`;
  });
}

function collectScenes() {
  const cards = document.querySelectorAll('.scene-editor-card');
  const scenes = [];
  cards.forEach((card, i) => {
    const narration = card.querySelector('.narration-input').value.trim();
    const visual = card.querySelector('.visual-input').value.trim();
    if (narration || visual) {
      scenes.push({
        scene_number: i + 1,
        narration: narration || '...',
        visual_description: visual || narration,
        duration_hint: 5,
      });
    }
  });
  return scenes;
}

// ─── Run Animated Pipeline ────────────────────────────────────────────────────
async function runAnimatedPipeline() {
  const title = document.getElementById('filmTitle').value.trim();
  if (!title) { showToast('Please enter a film title', 'error'); return; }

  const scenes = collectScenes();
  if (!scenes.length) { showToast('Please add at least one scene with narration or visual description', 'error'); return; }

  const description = document.getElementById('filmDescription').value.trim();
  const tagsStr = document.getElementById('studioTags').value.trim();
  const tags = tagsStr ? tagsStr.split(',').map(t => t.trim()).filter(Boolean) : [];
  const privacy = document.getElementById('studioPrivacy').value;
  const btn = document.getElementById('runAnimatedBtn');

  setButtonLoading(btn, true, '🎬 Starting...');

  try {
    const res = await fetch(`${API}/api/run-animated-pipeline`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        description,
        tags,
        lang: studioLang,
        video_type: studioType,
        template: document.getElementById('studioTemplate')?.value || 'mystery',
        style: document.getElementById('studioStyle')?.value || 'animated_3d',
        privacy,
        auto_upload: false,
        scenes,
      }),
    });
    const data = await res.json();

    if (data.error) throw new Error(data.error);

    showToast(`🎬 Animated film pipeline started! Job: ${data.job_id}`, 'success');
    showSection('jobs', document.querySelector('[data-section=jobs]'));

    await loadJobs();
    openJobModal(data.job_id);

  } catch (err) {
    showToast(`Failed: ${err.message}`, 'error');
    console.error(err);
  } finally {
    setButtonLoading(btn, false, '🎬 Generate Animated Film');
  }
}

// ─── Topic Suggestions (Quick Mode) ──────────────────────────────────────────
function setSuggestion(text) {
  document.getElementById('topicInput').value = text;
  document.getElementById('topicInput').focus();
}

// ─── Video Type Toggle (Quick Mode) ──────────────────────────────────────────
function setVideoType(type, btn) {
  videoType = type;
  document.querySelectorAll('#videoTypeToggle .toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ─── Preview Script (Quick Mode) ─────────────────────────────────────────────
async function previewScript() {
  const topic = document.getElementById('topicInput').value.trim();
  if (!topic) { showToast('Please enter a topic first', 'error'); return; }

  const btn = document.getElementById('previewScriptBtn');
  setButtonLoading(btn, true, '📝 Generating...');

  try {
    const res = await fetch(`${API}/api/generate-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, video_type: videoType }),
    });
    const data = await res.json();

    if (data.error) throw new Error(data.error);

    currentScript = data.script;
    renderScriptPreview(data.script);
    showToast('Script generated successfully!', 'success');
  } catch (err) {
    showToast(`Failed: ${err.message}`, 'error');
  } finally {
    setButtonLoading(btn, false, '📝 Preview Script Only');
  }
}

function renderScriptPreview(script) {
  const card = document.getElementById('scriptPreviewCard');
  document.getElementById('scriptTitle').textContent = script.title;
  document.getElementById('scriptMeta').textContent =
    `${script.scenes?.length || 0} scenes  ·  ${script.video_type === 'short' ? 'YouTube Short' : 'Long-form'}  ·  Topic: ${script.topic}`;

  const list = document.getElementById('scenesList');
  list.innerHTML = '';

  (script.scenes || []).forEach(scene => {
    const div = document.createElement('div');
    div.className = 'scene-card fade-in';
    div.innerHTML = `
      <div class="scene-num">${scene.scene_number}</div>
      <div class="scene-content">
        <div class="scene-narration">${escHtml(scene.narration)}</div>
        <div class="scene-visual">${escHtml(scene.visual_description)}</div>
      </div>
    `;
    list.appendChild(div);
  });

  card.style.display = 'block';
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function runPipelineFromPreview() { runPipeline(); }

// ─── Run Full Pipeline (Quick Mode) ──────────────────────────────────────────
async function runPipeline() {
  const topic = document.getElementById('topicInput').value.trim();
  if (!topic) { showToast('Please enter a topic first', 'error'); return; }

  const privacy = document.getElementById('privacySelect').value;
  const autoUpload = document.getElementById('autoUploadCheck').checked;
  const btn = document.getElementById('runPipelineBtn');

  setButtonLoading(btn, true, 'Starting Pipeline...');

  try {
    const res = await fetch(`${API}/api/run-pipeline`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topic, video_type: videoType, privacy, auto_upload: autoUpload,
        template: document.getElementById('templateSelect')?.value || 'mystery',
        style: document.getElementById('styleSelect')?.value || 'animated_3d',
      }),
    });
    const data = await res.json();

    if (data.error) throw new Error(data.error);

    showToast(`🚀 Pipeline started! Job: ${data.job_id}`, 'success');
    showSection('jobs', document.querySelector('[data-section=jobs]'));

    await loadJobs();
    openJobModal(data.job_id);

  } catch (err) {
    showToast(`Failed: ${err.message}`, 'error');
  } finally {
    setButtonLoading(btn, false, 'Run Full Pipeline');
  }
}

// ─── Job Queue ────────────────────────────────────────────────────────────────
async function pauseJob(jobId, action, btn) {
  if (btn) setButtonLoading(btn, true, action === 'pause' ? 'Pausing…' : 'Resuming…');
  try {
    const res = await fetch(`${API}/api/jobs/${jobId}/${action}`, { method: 'POST' });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showToast(action === 'pause'
      ? (jobId.startsWith('series__') ? '⏸️ Render paused' : '⏸️ Pausing at the next checkpoint…')
      : '▶️ Resumed', 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  await loadJobs();
  if (openJobId === jobId) refreshJobModal(jobId);
  if (document.getElementById('section-series').style.display !== 'none') loadSeries();
}

function uploadStats(u) {
  const eta = u.eta_s > 90 ? `~${Math.round(u.eta_s / 60)} min left` : `~${u.eta_s}s left`;
  return `⬆️ ${u.sent_mb} / ${u.total_mb} MB · ${u.speed_mbps} MB/s · ${eta}`;
}

let fastJobPoll = null;
async function loadJobs() {
  try {
    const res = await fetch(`${API}/api/jobs`);
    const data = await res.json();
    renderJobsList(data.jobs || []);
    updateJobsBadge(data.jobs || []);
  } catch (err) {
    console.error('Failed to load jobs:', err);
  }
}

function renderJobsList(jobs) {
  const list = document.getElementById('jobsList');
  clearTimeout(fastJobPoll);
  if (jobs.some(j => j.kind === 'upload' && j.status === 'running')) fastJobPoll = setTimeout(loadJobs, 2000);

  if (!jobs.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <p>No jobs yet. Create your first animated film!</p>
        <button class="btn btn-primary" onclick="showSection('create', document.querySelector('[data-section=create]'))">
          🎬 Create Film
        </button>
      </div>`;
    return;
  }

  list.innerHTML = '';
  jobs.forEach(job => {
    const card = document.createElement('div');
    card.className = `job-card status-${job.status}`;
    card.id = `job-card-${job.job_id}`;
    card.onclick = () => openJobModal(job.job_id);

    const topic = job.script?.title || job.steps?.[0]?.message?.replace(/🚀 Starting.*?: '/, '').replace(/'$/, '') || `Job ${job.job_id}`;
    const timeAgo = formatTimeAgo(job.created_at);
    const statusLabel = { queued: 'Queued', running: `Running ${job.progress || 0}%`, paused: `Paused ${job.progress || 0}%`,
                          done: 'Done ✅', error: 'Error ❌' }[job.status] || job.status;
    const ctl = job.status === 'running' ? `<button class="btn btn-ghost btn-sm job-ctl" title="Pause"
                   onclick="event.stopPropagation(); pauseJob('${job.job_id}', 'pause', this)">⏸ Pause</button>`
              : job.status === 'paused' ? `<button class="btn btn-primary btn-sm job-ctl" title="Resume"
                   onclick="event.stopPropagation(); pauseJob('${job.job_id}', 'resume', this)">▶ Resume</button>` : '';
    let nowStep = job.status === 'running' ? (job.steps || []).slice(-1)[0]?.message?.replace(/^→ /, '') : '';
    if (job.kind === 'upload' && job.status === 'running' && job.upload) nowStep = uploadStats(job.upload);

    card.innerHTML = `
      <div class="job-status-dot"></div>
      <div class="job-info">
        <div class="job-topic">${escHtml(topic)}</div>
        <div class="job-meta">${nowStep ? escHtml(nowStep) + ' · ' : ''}${timeAgo} · ${job.video_type || 'short'}</div>
      </div>
      <div class="job-progress-bar">
        <div class="job-progress-fill" style="width:${job.progress || 0}%"></div>
      </div>
      ${ctl}
      <span class="status-badge ${job.status}">${statusLabel}</span>
    `;
    list.appendChild(card);
  });
}

function updateJobsBadge(jobs) {
  const running = jobs.filter(j => j.status === 'running' || j.status === 'queued').length;
  const badge = document.getElementById('jobsBadge');
  if (running > 0) {
    badge.style.display = 'inline';
    badge.textContent = running;
  } else {
    badge.style.display = 'none';
  }
}

// ─── Job Modal ────────────────────────────────────────────────────────────────
async function openJobModal(jobId) {
  openJobId = jobId;
  document.getElementById('jobModal').classList.add('open');
  await refreshJobModal(jobId);

  if (!pollingIntervals[jobId]) {
    pollingIntervals[jobId] = setInterval(async () => {
      const job = await fetchJob(jobId);
      updateJobModal(job);
      loadJobs();
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(pollingIntervals[jobId]);
        delete pollingIntervals[jobId];
      }
    }, 2500);
  }
}

async function refreshJobModal(jobId) {
  const job = await fetchJob(jobId);
  updateJobModal(job);
}

function updateJobModal(job) {
  if (!job || openJobId !== job.job_id) return;

  const topic = job.script?.title || job.steps?.[0]?.message || `Job ${job.job_id}`;
  document.getElementById('modalTitle').textContent = topic;
  document.getElementById('modalStatus').textContent = job.status;
  document.getElementById('modalStatus').className = `status-badge ${job.status}`;
  document.getElementById('modalProgress').style.width = `${job.progress || 0}%`;
  document.getElementById('modalProgressPct').textContent = `${job.progress || 0}%`;

  const log = document.getElementById('modalStepLog');
  log.innerHTML = (job.steps || []).map(s =>
    `<div class="log-line">${escHtml(s.message)}</div>`
  ).join('');
  log.scrollTop = log.scrollHeight;

  const actions = document.getElementById('modalActions');
  actions.innerHTML = '';

  if (job.status === 'running' || job.status === 'paused') {
    const b = document.createElement('button');
    b.className = job.status === 'paused' ? 'btn btn-primary' : 'btn btn-ghost';
    b.textContent = job.status === 'paused' ? '▶ Resume' : '⏸ Pause';
    b.onclick = () => pauseJob(job.job_id, job.status === 'paused' ? 'resume' : 'pause', b);
    actions.appendChild(b);
  }

  if (job.kind === 'upload') {
    if (job.status === 'running' && job.upload) {
      const st = document.createElement('div');
      st.className = 'log-line'; st.style.fontWeight = '600';
      st.textContent = uploadStats(job.upload);
      log.appendChild(st);
    }
    const r = job.result || {};
    if (r.youtube_url) {
      const yt = document.createElement('a');
      yt.href = r.youtube_url; yt.target = '_blank'; yt.className = 'btn btn-primary';
      yt.textContent = '▶ Watch on YouTube';
      actions.appendChild(yt);
    }
    if (r.playlist_url) {
      const pl = document.createElement('a');
      pl.href = r.playlist_url; pl.target = '_blank'; pl.className = 'btn btn-secondary';
      pl.textContent = '📃 Playlist';
      actions.appendChild(pl);
    }
    if (job.status === 'running') setTimeout(() => refreshJobModal(job.job_id), 2000);
    return;
  }

  if (job.series) {
    const sr = job.series;
    if (sr.video_url) {
      const w = document.createElement('button');
      w.className = 'btn btn-secondary';
      w.textContent = '▶ Watch';
      w.onclick = () => openPlayer(sr.video_url, sr.title);
      actions.appendChild(w);
      if (sr.youtube_url) {
        const yt = document.createElement('a');
        yt.href = sr.youtube_url; yt.target = '_blank'; yt.className = 'btn btn-primary';
        yt.textContent = '▶ Watch on YouTube';
        actions.appendChild(yt);
      } else {
        const up = document.createElement('button');
        up.className = 'btn btn-primary';
        up.textContent = '📤 Upload to YouTube';
        up.onclick = () => uploadEpisode(sr.series_id, sr.episode_id, up);
        actions.appendChild(up);
      }
    }
    return;
  }

  if (job.status === 'done' && job.result) {
    const r = job.result;

    if (r.video_url_local) {
      const dlBtn = document.createElement('a');
      dlBtn.href = `${API}/api/download/${job.job_id}`;
      dlBtn.className = 'btn btn-secondary';
      dlBtn.textContent = '⬇️ Download Video';
      actions.appendChild(dlBtn);
    }

    if (r.youtube_url) {
      const ytBtn = document.createElement('a');
      ytBtn.href = r.youtube_url;
      ytBtn.target = '_blank';
      ytBtn.className = 'btn btn-primary';
      ytBtn.textContent = '▶ Watch on YouTube';
      actions.appendChild(ytBtn);
    } else {
      const uploadBtn = document.createElement('button');
      uploadBtn.className = 'btn btn-primary';
      uploadBtn.textContent = '📤 Upload to YouTube';
      uploadBtn.onclick = () => manualUpload(job.job_id, uploadBtn);
      actions.appendChild(uploadBtn);
    }
  }

  if (job.status === 'error') {
    const errDiv = document.createElement('div');
    errDiv.style.cssText = 'color: #FF6584; font-size: 13px; margin-top: 8px; padding: 10px; background: rgba(255,101,132,0.1); border-radius: 8px; border: 1px solid rgba(255,101,132,0.3);';
    errDiv.textContent = `❌ Error: ${job.error}`;
    document.getElementById('modalStepLog').after(errDiv);
  }
}

async function fetchJob(jobId) {
  try {
    const res = await fetch(`${API}/api/jobs/${jobId}`);
    return await res.json();
  } catch {
    return null;
  }
}

function closeModal() {
  document.getElementById('jobModal').classList.remove('open');
  openJobId = null;
}

function closeJobModal(e) {
  if (e.target === document.getElementById('jobModal')) closeModal();
}

// ─── Manual Upload ────────────────────────────────────────────────────────────
async function manualUpload(jobId, btn) {
  const privacy = document.getElementById('studioPrivacy')?.value ||
                  document.getElementById('privacySelect')?.value || 'private';
  setButtonLoading(btn, true, 'Uploading...');

  try {
    const res = await fetch(`${API}/api/jobs/${jobId}/upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ privacy }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    showToast('⬆️ Upload started — live progress in Job Queue', 'success');
    closeModal();
    showSection('jobs', document.querySelector('[data-section=jobs]'));
  } catch (err) {
    showToast(`Upload failed: ${err.message}`, 'error');
  } finally {
    setButtonLoading(btn, false, '📤 Upload to YouTube');
  }
}

// ─── Channel Stats ────────────────────────────────────────────────────────────
async function loadChannelStats() {
  try {
    const res = await fetch(`${API}/api/channel-stats`);
    const data = await res.json();

    if (data.error) {
      document.getElementById('miniSubs').textContent = '—';
      document.getElementById('miniViews').textContent = '—';
      document.getElementById('miniVideos').textContent = '—';
      return;
    }

    const fmt = n => n >= 1000000 ? `${(n/1000000).toFixed(1)}M` :
                     n >= 1000 ? `${(n/1000).toFixed(1)}K` : `${n}`;

    document.getElementById('miniSubs').textContent   = fmt(data.subscriber_count || 0);
    document.getElementById('miniViews').textContent  = fmt(data.view_count || 0);
    document.getElementById('miniVideos').textContent = data.video_count || 0;

    document.getElementById('statSubs').textContent   = fmt(data.subscriber_count || 0);
    document.getElementById('statViews').textContent  = fmt(data.view_count || 0);
    document.getElementById('statVideos').textContent = data.video_count || 0;

    document.getElementById('channelName').textContent = data.channel_name || 'Your Channel';

    renderRecentVideos(data.recent_videos || []);
  } catch (err) {
    console.error('Stats error:', err);
  }
}

function renderRecentVideos(videos) {
  const list = document.getElementById('recentVideosList');
  if (!videos.length) {
    list.innerHTML = '<div class="loading-pulse">No recent videos found.</div>';
    return;
  }

  list.innerHTML = videos.map(v => `
    <a class="recent-video-item" href="https://www.youtube.com/watch?v=${v.video_id}" target="_blank" style="text-decoration:none; color:inherit">
      <img class="recent-video-thumb" src="${v.thumbnail}" alt="${escHtml(v.title)}" onerror="this.style.display='none'" />
      <div>
        <div class="recent-video-title">${escHtml(v.title)}</div>
        <div class="recent-video-date">${formatDate(v.published_at)}</div>
      </div>
    </a>
  `).join('');
}

// ─── Utilities ─────────────────────────────────────────────────────────────────
function setButtonLoading(btn, loading, label) {
  if (loading) {
    btn.disabled = true;
    btn.classList.add('btn-loading');
    btn.dataset.originalText = btn.textContent;
    btn.innerHTML = `<span class="btn-icon"></span> ${label}`;
  } else {
    btn.disabled = false;
    btn.classList.remove('btn-loading');
    btn.innerHTML = label;
  }
}

function escHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatTimeAgo(isoStr) {
  if (!isoStr) return '';
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function formatDate(isoStr) {
  if (!isoStr) return '';
  return new Date(isoStr).toLocaleDateString('en-IN', {
    day: 'numeric', month: 'short', year: 'numeric',
  });
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'toastOut 0.3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ─── Keyboard Shortcuts ───────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});


// ─── Series ───────────────────────────────────────────────────────────────────
let seriesPoll = null;

async function loadSeries() {
  try {
    const res = await fetch(`${API}/api/series`);
    const data = await res.json();
    renderSeries(data);
    clearInterval(seriesPoll);
    const uploading = data.series.some(se => se.episodes.some(e => e.status === 'uploading'));
    if (data.busy || uploading) seriesPoll = setInterval(() => {
      if (document.getElementById('section-series').style.display !== 'none') loadSeries();
    }, uploading ? 2500 : 15000);
  } catch (e) {
    document.getElementById('seriesList').innerHTML =
      `<div class="empty-state"><div class="empty-icon">⚠️</div><p>Could not load series: ${escHtml(e.message)}</p></div>`;
  }
}

function renderSeries(data) {
  window.__seriesPaused = !!data.paused;
  const busy = document.getElementById('seriesBusy');
  busy.style.display = data.busy ? 'block' : 'none';
  const sid = data.series[0]?.id;
  const epId = (data.busy_with || '').replace(/\.json$/, '');
  busy.innerHTML = !data.busy ? '' : data.paused
    ? `⏸️ Paused: <b>${escHtml(data.busy_with)}</b> — it will continue exactly where it stopped.
       <button class="btn btn-primary btn-sm" onclick="pauseJob('series__${sid}__${epId}', 'resume', this)">▶ Resume</button>`
    : `⏳ Rendering <b>${escHtml(data.busy_with || 'an episode')}</b> — one at a time, ~15–20 min each.
       <button class="btn btn-ghost btn-sm" onclick="pauseJob('series__${sid}__${epId}', 'pause', this)">⏸ Pause</button>`;
  const list = document.getElementById('seriesList');
  if (!data.series.length) {
    list.innerHTML = '<div class="empty-state"><div class="empty-icon">📺</div><p>No series yet. Add one under scripts/&lt;name&gt;/series.json</p></div>';
    return;
  }
  list.innerHTML = data.series.map(se => {
    const done = se.episodes.filter(e => ['rendered', 'uploaded'].includes(e.status)).length;
    const eps = se.episodes.map(ep => episodeCard(se, ep, data.busy)).join('');
    return `
      <div class="series-card">
        <div class="series-head">
          <div>
            <div class="series-name">${escHtml(se.name_hindi || '')} <span class="series-name-en">${escHtml(se.name || se.id)}</span></div>
            <div class="series-sub">${se.episodes.length} episodes · ${done} rendered · ${escHtml(se.hashtag || '')}
              ${se.playlist_url ? ` · <a href="${escHtml(se.playlist_url)}" target="_blank">📃 Playlist ↗</a>` : ' · 📃 playlist is created on first upload'}</div>
          </div>
          <div class="series-actions">
            <button class="btn btn-secondary" ${se.episodes.every(e => e.status !== 'ready' || e.prepared) ? 'disabled' : ''}
                    title="Record + check the voice and make + check the images for every episode (no video yet)"
                    onclick="prepareSeries('${se.id}', this)">🎙️ Prepare voice &amp; art</button>
            <button class="btn btn-primary" ${done === se.episodes.length ? 'disabled' : ''}
                    onclick="renderSeriesEp('${se.id}', '', this)">🎬 Render remaining</button>
          </div>
        </div>
        <div class="episode-grid">${eps}</div>
      </div>`;
  }).join('');
}

function episodeCard(se, ep, busy) {
  if (ep.status === 'rendering' && window.__seriesPaused) ep.status = 'paused';
  const badge = { rendered: 'Rendered ✅', uploaded: 'On YouTube ✅', rendering: 'Rendering…', paused: 'Paused ⏸️', queued: 'Queued',
                  ready: ep.prepared ? 'Voice + art ready ✅' : 'Script ready',
                  uploading: `Uploading ${ep.upload_pct ?? 0}%` }[ep.status] || ep.status;
  if (ep.status === 'uploading') {
    return `
    <div class="episode-card ep-uploading">
      <div class="ep-top"><span class="ep-num">EP ${ep.episode ?? ''}</span><span class="status-badge ep-badge">${badge}</span></div>
      <div class="ep-hook">${escHtml(ep.hook || '')}</div>
      <div class="ep-title">${escHtml(ep.title || ep.id)}</div>
      <div class="job-progress-bar"><div class="job-progress-fill" style="width:${ep.upload_pct ?? 0}%"></div></div>
      <div class="ep-meta">⬆️ Uploading to YouTube…</div>
    </div>`;
  }
  const actions = ep.status === 'uploaded'
    ? `<a class="btn btn-outline btn-sm" href="${escHtml(ep.youtube_url || '#')}" target="_blank">Watch on YouTube ↗</a>`
    : ep.status === 'rendered'
    ? `<button class="btn btn-ghost btn-sm" onclick='openPlayer(${JSON.stringify(ep.video_url)}, ${JSON.stringify(ep.title)})'>▶ Watch</button>
       ${ep.youtube_url
         ? `<a class="btn btn-outline btn-sm" href="${escHtml(ep.youtube_url)}" target="_blank">On YouTube ↗</a>`
         : `<button class="btn btn-outline btn-sm" onclick="uploadEpisode('${se.id}', '${ep.id}', this)">⬆ Upload</button>`}`
    : ep.status === 'ready'
      ? `<button class="btn btn-ghost btn-sm" onclick="renderSeriesEp('${se.id}', '${ep.id}', this)">🎬 ${busy ? 'Add to queue' : 'Render'}</button>`
      : '';
  return `
    <div class="episode-card ep-${ep.status}">
      <div class="ep-top"><span class="ep-num">EP ${ep.episode ?? ''}</span><span class="status-badge ep-badge">${badge}</span></div>
      <div class="ep-hook">${escHtml(ep.hook || '')}</div>
      <div class="ep-title">${escHtml(ep.title || ep.id)}</div>
      <div class="ep-meta">${ep.scenes} scenes · ${(ep.tags || []).length} tags</div>
      <div class="ep-actions">${actions}</div>
    </div>`;
}

async function prepareSeries(seriesId, btn) {
  setButtonLoading(btn, true, 'Queuing…');
  try {
    const res = await fetch(`${API}/api/series/${seriesId}/prepare`, { method: 'POST' });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showToast(`🎙️ Preparing voice + art: ${data.queued.join(', ')}`, 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadSeries();
}

async function renderSeriesEp(seriesId, episodeId, btn) {
  setButtonLoading(btn, true, 'Starting…');
  try {
    const res = await fetch(`${API}/api/series/${seriesId}/render`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ episode: episodeId }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showToast(data.waiting ? `⏳ Added to the render queue: ${data.queued.join(', ')}` : `🎬 Rendering: ${data.queued.join(', ')}`, 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadSeries();
}

async function uploadEpisode(seriesId, episodeId, btn) {
  const privacy = prompt('Upload to YouTube as: private, unlisted or public?', 'private');
  if (!privacy) return;
  if (!['private', 'unlisted', 'public'].includes(privacy.trim())) { showToast('Use private, unlisted or public', 'error'); return; }
  if (!confirm(`Upload this episode to The Faceless as ${privacy.trim().toUpperCase()}?`)) return;
  setButtonLoading(btn, true, 'Uploading…');
  try {
    const res = await fetch(`${API}/api/series/${seriesId}/${episodeId}/upload`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ privacy: privacy.trim() }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showToast('⬆️ Upload started — live progress in Job Queue', 'success');
  } catch (e) {
    showToast(`Upload failed: ${e.message}`, 'error');
  }
  loadSeries();
  loadJobs();
}

function openPlayer(url, title) {
  const v = document.getElementById('playerVideo');
  v.src = url;
  document.getElementById('playerMeta').textContent = title || '';
  document.getElementById('playerModal').style.display = 'flex';
  v.play().catch(() => {});
}

function closePlayer() {
  const v = document.getElementById('playerVideo');
  v.pause(); v.removeAttribute('src'); v.load();
  document.getElementById('playerModal').style.display = 'none';
}
