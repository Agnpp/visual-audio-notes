/* Local desktop view. User text is always rendered as text, never executable markup. */
(async () => {
  'use strict';
  const i18n = await window.VAN_I18N.init(), t = i18n.t;
  const $ = (id) => document.getElementById(id);
  $('interface-language').value = i18n.language;
  const ui = { state: { projects: [], connection: {}, job: null }, selected: null, project: null,
    view: 'all', tab: 'notes', page: 0, query: '', busy: false, loading: false,
    notesTimer: null, toastTimer: null, pollTimer: null, drafts: new Map(), lastJob: null, initialBoot: true,
    settingsTab: 'text', stopping: false, sourceDrafts: new Map(), importTarget: null };
  const icon = (name) => { const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.classList.add('icon'); const use = document.createElementNS(svg.namespaceURI, 'use'); use.setAttribute('href', '#i-' + name); svg.append(use); return svg; };
  const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
  const media = (path) => '/media/' + String(path).split(/[\\/]/).map(encodeURIComponent).join('/') + '?token=' + encodeURIComponent(window.VAN_TOKEN);
  const date = (value) => { try { return new Date(value).toLocaleDateString(i18n.language, { month: 'numeric', day: 'numeric' }); } catch (_) { return ''; } };
  const time = (value) => { const seconds = Math.max(0, Math.floor(Number(value) || 0)); return (seconds >= 3600 ? Math.floor(seconds / 3600) + ':' : '') + String(Math.floor(seconds / 60) % 60).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0'); };
  const isRunning = () => ui.state.job && ['running', 'stopping', 'cancelling'].includes(ui.state.job.state);
  const sameJobRunning = () => isRunning() && ui.state.job.project_id === ui.selected;
  const isDerivedPreview = (page) => ['derived_readable', 'native_converter'].includes(page.preview_kind);
  const pageLabel = (page) => i18n.language === 'en' ? (page.original_filename ? page.original_filename + (isDerivedPreview(page) ? ' · Preview page ' : ' · Page ') + (page.original_page || page.page) : 'Document page ' + page.page) : page.original_filename ? page.original_filename + (isDerivedPreview(page) ? t(' · 预览第 ') : t(' · 第 ')) + (page.original_page || page.page) + t(' 页') : t('课件第 ') + page.page + t(' 页');
  const bytes = (value) => { const count = Number(value); return value != null && Number.isFinite(count) && count >= 0 ? (count >= 1e9 ? (count / 1e9).toFixed(1) + ' GB' : Math.round(count / 1e6) + ' MB') : ''; };

  async function api(action, body, method = 'POST') {
    const options = { method, headers: { 'X-Van-Token': window.VAN_TOKEN, 'X-Van-Language': i18n.language } };
    if (method !== 'GET') { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body || {}); }
    const response = await fetch('/api/' + action, options);
    let result; try { result = await response.json(); } catch (_) { throw new Error(t('暂时无法连接资料库，请重新打开软件。')); }
    if (!response.ok || result.ok === false) throw new Error(result.error || t('操作没有完成，请稍后重试。'));
    return result.data === undefined ? result : result.data;
  }

  function toast(message, error = false, duration = 5000) {
    clearTimeout(ui.toastTimer); $('toast').classList.toggle('error', error); $('toast').querySelector('span').textContent = message;
    $('toast').hidden = false; ui.toastTimer = setTimeout(() => { $('toast').hidden = true; }, duration);
  }
  function setBusy(value) { ui.busy = value; $('import-button').disabled = value; $('welcome-import').disabled = value; $('add-files-button').disabled = value || !!isRunning(); }

  function filteredProjects() {
    return (ui.state.projects || []).filter((p) => {
      if (ui.view === 'review' && !(p.counts?.pending || p.status === 'failed' || p.has_ai)) return false;
      if (ui.view === 'archived' && !p.archived_at && p.status !== 'archived') return false;
      return !ui.query || [p.title, p.summary, p.manual_notes, ...(p.files || []).map(f => f.name)].join(' ').toLowerCase().includes(ui.query);
    });
  }
  function renderList() {
    const projects = ui.state.projects || [], filtered = filteredProjects();
    $('count-all').textContent = projects.length;
    $('welcome-import-label').textContent = projects.length ? t('导入新资料') : t('导入第一份资料');
    $('count-review').textContent = projects.filter(p => p.counts?.pending || p.status === 'failed' || p.has_ai).length;
    $('count-archived').textContent = projects.filter(p => p.archived_at || p.status === 'archived').length;
    $('list-title').textContent = { all: t('全部资料'), review: t('待核对'), archived: t('已归档') }[ui.view];
    $('list-subtitle').textContent = { all: t('每一份资料，都有条理'), review: t('回到原文，让重点更准确'), archived: t('整理好的，随时可以回看') }[ui.view];
    $('list-count').textContent = i18n.language === 'en' ? filtered.length + (filtered.length === 1 ? ' project' : ' projects') : filtered.length + t(' 份资料');
    $('project-list').replaceChildren();
    if (!filtered.length) {
      const box = el('div', 'list-empty'), circle = el('span', 'list-empty-icon'); circle.append(icon('folder')); box.append(circle);
      box.append(el('strong', '', ui.query ? t('没有找到相关资料') : ui.view === 'archived' ? t('还没有归档资料') : ui.view === 'review' ? t('暂时没有待核对资料') : t('还没有资料')));
      box.append(el('p', '', ui.query ? t('换个关键词试试') : ui.view === 'all' ? t('导入后，资料会收在这里') : t('你的资料会显示在这里'))); $('project-list').append(box);
    }
    for (const project of filtered) {
      const button = el('button', 'project-item' + (ui.selected === project.id ? ' selected' : '')); button.type = 'button'; button.dataset.id = project.id;
      button.setAttribute('aria-label', t('打开资料：') + project.title); button.setAttribute('aria-current', ui.selected === project.id ? 'true' : 'false');
      const top = el('div', 'project-item-top'), symbol = el('span', 'file-symbol'); symbol.append(icon('file')); top.append(symbol, el('h3', '', project.title));
      const bottom = el('div', 'project-item-bottom'), status = el('span', 'mini-status' + (project.has_ai ? ' ai' : ''), project.archived_at ? t('已归档') : project.status_label || t('已导入'));
      bottom.append(status, el('span', '', (project.counts?.pages || 0) + t(' 页 · ') + date(project.created_at)));
      button.append(top, el('div', 'project-item-summary', project.summary || t('原件已保留，随时可以继续整理')), bottom);
      button.addEventListener('click', () => selectProject(project.id)); $('project-list').append(button);
    }
    const conn = ui.state.connection || {};
    $('connection-summary').textContent = conn.configured ? t('AI 连接已配置 · 按需使用') : t('无需 AI 也能整理与归档');
  }

  async function refresh({ reloadProject = false } = {}) {
    if (ui.loading) return;
    ui.loading = true;
    try {
      const state = await api('state', null, 'GET'); ui.state = state;
      renderList(); renderConnection(); renderRecognitionStatus();
      if (ui.initialBoot) { ui.initialBoot = false; if (!ui.selected && state.projects?.length) await selectProject(state.projects[0].id); }
      const job = state.job;
      if (job && !isRunning() && ui.lastJob === job.id) {
        ui.lastJob = null; toast(job.message || (job.state === 'complete' ? t('重点已整理，记得核对原文。') : t('整理暂停，已保留完成的步骤。')), job.state === 'failed');
        reloadProject = true; ui.stopping = false;
      }
      if (isRunning()) ui.lastJob = job.id;
      if (reloadProject && ui.selected) { const result = await api('project', { id: ui.selected }); ui.project = result; renderReader(false); }
      renderJob();
      clearTimeout(ui.pollTimer); if (isRunning()) ui.pollTimer = setTimeout(() => refresh({ reloadProject: true }), 1200);
    } catch (error) { toast(error.message, true); } finally { ui.loading = false; }
  }

  async function selectProject(id) {
    try {
      if (ui.selected && ui.selected !== id) await saveNotes(true);
      const project = await api('project', { id });
      ui.selected = id; ui.project = project; ui.page = 0; ui.tab = 'notes'; $('transcript-search').value = '';
      renderList(); renderReader(true);
    } catch (error) { toast(error.message, true); }
  }
  function renderConnection() {
    const conn = ui.state.connection || {}, thoughts = { off: t('关闭思考'), disabled: t('关闭思考'), low: t('低'), high: t('高'), max: t('最高') };
    $('model-label').textContent = conn.configured ? (conn.model || t('已配置模型')) + ' · ' + (conn.provider === 'deepseek' ? thoughts[conn.thinking] || t('关闭思考') : t('服务控制')) : t('选择 AI 连接');
  }
  function renderReader(reset = false) {
    const project = ui.project;
    $('welcome').hidden = !!project; $('project-reader').hidden = !project;
    if (!project) return;
    $('project-title').textContent = project.title; $('project-status').textContent = project.archived_at ? t('已归档') : project.status_label || t('已导入');
    $('open-export-button').hidden = !project.export_path;
    $('project-meta').textContent = date(project.created_at) + t(' 导入  ·  ') + (project.pages?.length || 0) + t(' 页资料  ·  ') + (project.segments?.length || 0) + t(' 段转写');
    $('file-chips').replaceChildren();
    for (const file of project.files || []) { const chip = el('span', 'file-chip'); chip.title = file.name; chip.append(icon(file.kind === 'audio' ? 'audio' : 'file'), el('span', '', file.name)); $('file-chips').append(chip); }
    $('transcript-count').textContent = project.segments?.length || '0';
    const draft = ui.drafts.get(project.id); $('manual-notes').value = draft === undefined ? project.manual_notes || '' : draft;
    $('save-status').textContent = draft === undefined ? t('已保存') : t('未保存'); $('save-status').classList.toggle('pending', draft !== undefined);
    renderTopics(); renderCapabilities(); renderPages(); renderTranscript(); renderJob(); renderConnection();
    const audioPath = project.audio_path || project.audio; $('audio-dock').hidden = !audioPath;
    const player = $('audio-player');
    if (audioPath && player.dataset.path !== audioPath) { player.src = media(audioPath); player.dataset.path = audioPath; $('audio-name').textContent = (project.files || []).find(f => f.kind === 'audio')?.name || t('独立录音'); }
    if (!audioPath && player.dataset.path) { player.removeAttribute('src'); player.load(); delete player.dataset.path; }
    switchTab(ui.tab, reset);
  }
  function renderCapabilities() {
    const project = ui.project, missing = project.missing_capabilities || [], needs = project.pipeline_needs || {}, notices = [];
    if (needs.asr || missing.includes('speech_to_text')) notices.push(project.needs_model_download ? t('录音还没有转写。首次使用请在“语音识别”中准备本机模型，或选择自己的语音 API；之后可以直接开始整理。') : t('录音将按语音识别设置转成文字。已有转写会直接复用，不重新识别。'));
    if (needs.ocr || missing.includes('ocr')) notices.push(t('无原生文字的图片或扫描页会按“图片识字”设置处理；原图保留，识字结果仍需核对。'));
    if (!project.segments?.length && !needs.asr && !missing.includes('speech_to_text')) notices.push(t('本份资料没有录音转写。仍可阅读原件、手动记录和保存归档。'));
    for (const warning of project.preparation_warnings || []) if (typeof warning === 'string') notices.push(warning);
    $('capability-notice').hidden = !notices.length; $('capability-notice').textContent = notices.join(' ');
    const busy = isRunning(), recognition = ui.state.recognition || {}, localVoiceUnavailable = (needs.asr || missing.includes('speech_to_text')) && recognition.asr?.provider === 'local_whisper' && (recognition.asr.local_engine_available === false || recognition.package_flavor === 'api'); $('ai-button').disabled = !!busy;
    $('ai-button').querySelector('span').textContent = busy ? t('正在整理') : localVoiceUnavailable ? t('选择语音识别方式') : project.needs_model_download ? t('准备后开始整理') : project.has_ai ? t('继续整理') : t('开始整理');
    $('ai-description').textContent = project.has_ai ? t('主题是 AI 草稿。引用可定位，不代表解释已经核实。') : t('按需识别录音和图片，再整理有来源的重点；原生读取、关键词候选和手动记录无需 AI。');
    const usage = project.usage; $('usage-line').hidden = !usage;
    if (usage) { const count = usage.total_api_attempts ?? usage.api_attempts ?? t('未知'), reused = usage.local_result_cache?.complete_reuses || 0; $('usage-line').textContent = t('本轮新增 ') + count + t(' 次 API 调用') + (usage.recognition ? t('（含识别与归纳）') : '') + t(' · 归纳结果复用 ') + reused + t(' 个步骤') + (usage.remote_input_cache?.hit_rate != null ? t(' · 文字归纳的已知输入缓存命中 ') + Math.round(usage.remote_input_cache.hit_rate * 100) + '%' : '') + '。' + (usage.recognition ? t('本机识别同样属于 AI。') : '') + t('未返回的用量记为未知。'); }
  }
  function renderTopics() {
    const container = $('topic-notes'); container.replaceChildren(); let total = 0;
    const transcriptLabel = { generated_asr: t('模型转写'), user_edited: t('修订转写'), user_provided_transcript: t('导入转写') }[ui.project.transcript_origin] || t('录音转写');
    for (const [origin, title, description] of [['audio', t('录音讲到 · 依据') + transcriptLabel, t('转写可能有误，请结合原音核对。')], ['courseware', t('课件补充 · 未确认讲过'), t('来自课件的内容，不代表这段录音讲到了。')]]) {
      const topics = ui.project.topics?.[origin] || []; if (!topics.length) continue; total += topics.length;
      const heading = el('div', 'source-heading'); heading.append(icon(origin === 'audio' ? 'audio' : 'file'), el('h3', '', title), el('small', '', t('AI 草稿')));
      container.append(heading, el('p', 'source-caption', description));
      for (const topic of topics) {
        const card = el('article', 'topic-card ' + origin); card.append(el('h4', '', topic.title || t('重点')), el('p', 'key-idea', topic.key_idea || topic.statement || ''));
        if (topic.explanation) card.append(el('p', 'explanation', topic.explanation));
        if (topic.evidence?.length) {
          const details = el('details', 'topic-evidence'); details.append(el('summary', '', t('查看原文依据 · ') + topic.evidence.length + t(' 条')));
          for (const evidence of topic.evidence) details.append(el('p', '', typeof evidence === 'string' ? evidence : evidence.quote || evidence.text || evidence.source_id || ''));
          card.append(details);
        } else if (topic.claim_ids?.length) card.append(el('div', 'topic-evidence', topic.claim_ids.length + t(' 条原文依据已保留在归档中')));
        container.append(card);
      }
    }
    if (!total) {
      const empty = el('div', 'notes-empty'); empty.append(icon('spark'), el('strong', '', t('重点笔记，从这里开始')), el('p', '', t('点击“开始整理”，按设置识别所需内容。配置文字 AI 后，再归纳重点；现在也能直接阅读、记录和归档。'))); container.append(empty);
    }
  }
  function renderPages() {
    const pages = ui.project.pages || []; ui.page = Math.max(0, Math.min(ui.page, pages.length - 1));
    $('page-total').textContent = pages.length + t(' 页'); $('page-position').textContent = pages.length ? (ui.page + 1) + ' / ' + pages.length : '0 / 0';
    $('page-prev').disabled = !pages.length || ui.page === 0; $('page-next').disabled = !pages.length || ui.page === pages.length - 1;
    $('page-preview').replaceChildren(); $('page-text-details').hidden = !pages.length; $('page-correction').replaceChildren();
    $('page-origin').hidden = !pages.length; $('page-preview-warning').hidden = true;
    if (pages.length) {
      const page = pages[ui.page], derived = isDerivedPreview(page);
      $('page-origin').textContent = pageLabel(page) + (derived ? t(' · 转换或文字预览') : t(' · 原页预览')) + (page.text_origin === 'user_corrected' ? t(' · 用户修订过的文字') : '');
      $('source-preview-title').textContent = derived ? t('课件预览 · 请核对原件') : t('课件预览');
      const warnings = Array.isArray(page.preview_warnings) ? page.preview_warnings : page.preview_warnings ? [page.preview_warnings] : [];
      $('page-preview-warning').hidden = !warnings.length; $('page-preview-warning').textContent = warnings.join(' ');
      if (page.image) { const image = el('img'); image.alt = pageLabel(page) + (derived ? t('的预览') : ''); image.src = media(page.image); $('page-preview').append(image); }
      else $('page-preview').append(el('div', 'preview-empty', page.text || t('该页尚无可用预览，原始文件仍保留。')));
      $('page-text').textContent = page.text || t('尚未取得可读文字，请核对原页或图片识字设置。');
      $('page-correction').append(correctionEditor('page', page.unit_id, page, t('修正识别文字'), t('修正 ') + pageLabel(page) + t(' 的文字')));
    } else $('page-preview').append(el('div', 'preview-empty', t('这份资料还没有课件页面。\n原始文件已保留。')));
  }
  function renderTranscript() {
    const container = $('transcript-list'); container.replaceChildren(); const query = $('transcript-search').value.trim().toLowerCase();
    $('transcript-origin').textContent = ui.project.transcript_origin === 'user_edited' ? t('用户修订稿，仍需核对原音。点时间可回听。') : ui.project.transcript_origin === 'generated_asr' ? t('模型转写仍需核对，点时间可回听原音。') : t('导入转写尚未验证同步，点时间可回听原音。');
    const segments = (ui.project.chunks?.length ? ui.project.chunks : ui.project.segments || []).filter(s => !query || s.text.toLowerCase().includes(query));
    if (!segments.length) { container.append(el('div', 'transcript-empty', query ? t('没有找到相关转写。') : t('还没有转写文字。\n点击“开始整理”使用已选语音能力；已有 SRT / VTT 也可以直接复用。\n未识别的录音不会冒充已完成转写。'))); return; }
    for (const segment of segments) {
      const row = el('div', 'transcript-cue'), button = el('button', 'cue-time'); button.type = 'button'; button.append(icon('play'), document.createTextNode(time(segment.start))); button.title = t('从 ') + time(segment.start) + t(' 回听'); button.disabled = !(ui.project.audio_path || ui.project.audio);
      button.addEventListener('click', () => { const player = $('audio-player'); player.currentTime = Number(segment.start); player.play().catch(() => toast(t('录音暂时无法播放，请检查文件。'), true)); });
      const body = el('div', 'cue-body'); body.append(el('p', 'cue-text', segment.text));
      const review = (ui.project.review_items || []).find(item => item.segment_id === segment.segment_id);
      const suggestions = (review?.candidates || []).slice(0, 3).map(candidate => ({ ...candidate, page: (ui.project.pages || []).find(page => page.unit_id === candidate.unit_id) })).filter(candidate => candidate.page);
      if (suggestions.length && !['confirmed', 'confirmed_unmatched'].includes(review?.status)) {
        const box = el('div', 'candidate-suggestions'), method = ui.project.candidate_method || review?.method || '';
        box.append(el('span', 'candidate-caption', method.includes('ai_derived') || ui.project.candidate_uses_ai_derived_terms ? t('AI 草稿词辅助候选 · 需核对') : method === 'lexical' ? t('关键词候选 · 无需 AI · 需核对') : t('内容候选 · 尚未确认')));
        for (const suggestion of suggestions) {
          const item = el('div', 'candidate-item'), preview = el('button', 'candidate-preview'); preview.type = 'button'; preview.append(icon('file'), el('span', '', t('建议 ') + pageLabel(suggestion.page))); preview.title = t('预览此页，不确认对应关系');
          preview.addEventListener('click', () => { ui.page = ui.project.pages.findIndex(page => page.unit_id === suggestion.unit_id); renderPages(); switchTab('sources'); });
          const confirm = el('button', 'text-button', t('确认')); confirm.type = 'button'; confirm.disabled = !!sameJobRunning(); confirm.setAttribute('aria-label', t('确认 ') + time(segment.start) + t(' 对应 ') + pageLabel(suggestion.page));
          confirm.addEventListener('click', () => confirmCorrespondence(segment.segment_id, [suggestion.unit_id], 'match', confirm));
          item.append(preview, confirm); box.append(item);
        }
        body.append(box);
      }
      if (review || ui.project.pages?.length) {
        const details = el('details', 'cue-review'), summary = el('summary', '', review?.status === 'confirmed' ? t('已确认对应 · 查看或修改') : review?.status === 'confirmed_unmatched' ? t('已确认无对应 · 修改') : t('核对课件对应')); details.append(summary);
        const controls = el('div', 'cue-review-controls'), select = el('select'); select.setAttribute('aria-label', t('为 ') + time(segment.start) + t(' 的讲解选择课件'));
        const empty = el('option', '', t('选择对应课件页')); empty.value = ''; select.append(empty);
        for (const page of ui.project.pages || []) { const option = el('option', '', pageLabel(page) + (page.text ? ' · ' + page.text.slice(0, 28) : '')); option.value = page.unit_id; select.append(option); }
        const confirmedPages = review?.status === 'confirmed' ? (review.confirmed_unit_ids || []) : [];
        if (confirmedPages.length === 1 && (ui.project.pages || []).some(page => page.unit_id === confirmedPages[0])) select.value = confirmedPages[0];
        const confirm = el('button', 'text-button', t('确认对应')); confirm.type = 'button'; confirm.disabled = true;
        select.addEventListener('change', () => { confirm.disabled = !select.value; });
        const noMatch = el('button', 'text-button muted-action', t('确认无对应')); noMatch.type = 'button';
        confirm.addEventListener('click', () => confirmCorrespondence(segment.segment_id, [select.value], 'match', confirm));
        noMatch.addEventListener('click', () => confirmCorrespondence(segment.segment_id, [], 'no_match', noMatch));
        controls.append(select, confirm, noMatch); details.append(controls, el('p', 'cue-review-hint', t('仅在你核对原页和原音后记录；浏览或播放不会自动确认。'))); body.append(details);
      }
      body.append(correctionEditor('transcript', segment.segment_id, segment, t('修正这段转写'), t('修正 ') + time(segment.start) + t(' 的转写')));
      row.append(button, body); container.append(row);
    }
  }
  function correctionEditor(kind, sourceId, source, label, accessibleLabel) {
    const key = ui.selected + '|' + kind + '|' + sourceId, draft = ui.sourceDrafts.get(key);
    const details = el('details', 'source-correction'); details.dataset.sourceId = sourceId; details.open = !!draft;
    details.append(el('summary', '', label));
    const editor = el('div', 'source-edit-body'), textarea = el('textarea', 'source-edit-text'); textarea.rows = 4; textarea.value = draft?.text ?? source.text ?? ''; textarea.setAttribute('aria-label', accessibleLabel); textarea.dataset.lockDuringJob = 'true'; textarea.disabled = !!isRunning();
    const hint = el('p', 'source-edit-hint', t('保留原件与原识别结果；资料内容变化后，原对应关系需重新核对，未改动的模型批次会复用。'));
    const status = el('span', 'source-edit-status', draft ? t('修订尚未保存') : ''), actions = el('div', 'source-edit-actions'), save = el('button', 'secondary-button compact', t('保存修订')), discard = el('button', 'text-button muted-action', t('取消本次修订')); save.type = 'button'; save.dataset.lockDuringJob = 'true'; save.disabled = !!isRunning(); discard.type = 'button'; discard.dataset.lockDuringJob = 'true'; discard.disabled = !!isRunning();
    let start, end;
    const remember = () => { const value = { text: textarea.value }; if (kind === 'transcript') { value.start = start.value; value.end = end.value; } ui.sourceDrafts.set(key, value); status.textContent = t('修订尚未保存'); };
    textarea.addEventListener('input', remember); editor.append(textarea);
    if (kind === 'transcript') {
      const timing = el('details', 'source-edit-timing'); timing.append(el('summary', '', t('调整时间范围（秒）')));
      const fields = el('div', 'source-time-fields');
      for (const [field, title] of [['start', t('开始')], ['end', t('结束')]]) { const wrapper = el('label', '', title), input = el('input'); input.type = 'number'; input.min = '0'; input.step = '0.001'; input.value = draft?.[field] ?? source[field]; input.setAttribute('aria-label', accessibleLabel + t('的') + title + t('秒数')); input.dataset.lockDuringJob = 'true'; input.disabled = !!isRunning(); wrapper.append(input); fields.append(wrapper); if (field === 'start') start = input; else end = input; input.addEventListener('input', remember); }
      timing.append(fields); editor.append(timing);
    }
    save.addEventListener('click', () => saveSourceEdit(kind, sourceId, textarea, start, end, save, key));
    discard.addEventListener('click', () => { ui.sourceDrafts.delete(key); textarea.value = source.text || ''; if (kind === 'transcript') { start.value = source.start; end.value = source.end; } status.textContent = ''; details.open = false; });
    actions.append(status, discard, save); editor.append(hint, actions); details.append(editor); return details;
  }
  async function saveSourceEdit(kind, sourceId, textarea, start, end, button, key) {
    if (!ui.project || isRunning() || button.dataset.saving === 'true') return;
    const id = ui.selected, text = textarea.value, payload = { id, kind, source_id: sourceId, text };
    if (!text.trim()) { toast(t('修订文字不能为空；请保留需要核对的原文。'), true); return; }
    if (kind === 'transcript') { const first = Number(start.value), last = Number(end.value); if (!start.value.trim() || !end.value.trim() || !Number.isFinite(first) || !Number.isFinite(last) || first < 0 || last <= first) { toast(t('请填写有效的开始、结束秒数；不会自动平移时间。'), true); return; } payload.start = first; payload.end = last; }
    const pageId = ui.project.pages?.[ui.page]?.unit_id, scroll = $('reader-scroll').scrollTop;
    const controls = button.closest('.source-edit-body').querySelectorAll('textarea,input,button');
    controls.forEach(control => { control.dataset.saving = 'true'; control.disabled = true; });
    try {
      await saveNotes(true); const project = await api('edit_text', payload); ui.sourceDrafts.delete(key);
      const index = ui.state.projects.findIndex(item => item.id === id); if (index >= 0) ui.state.projects[index] = project;
      if (ui.selected === id) { ui.project = project; const position = (project.pages || []).findIndex(page => page.unit_id === pageId); if (position >= 0) ui.page = position; renderList(); renderReader(false); $('reader-scroll').scrollTop = scroll; }
      toast(t('修订已保存，原件与原识别结果保留；变化后的对应关系请重新核对。'));
    } catch (error) { toast(error.message, true, 8000); }
    finally { controls.forEach(control => { control.dataset.saving = 'false'; control.disabled = !!isRunning(); }); }
  }
  async function confirmCorrespondence(segmentId, unitIds, decision, button) {
    if (!ui.selected || (decision === 'match' && !unitIds[0])) return;
    button.disabled = true;
    try { await saveNotes(true); const project = await api('decide', { id: ui.selected, segment_id: segmentId, unit_ids: unitIds, decision }); ui.project = project; const i = ui.state.projects.findIndex(p => p.id === project.id); if (i >= 0) ui.state.projects[i] = project; renderList(); renderTranscript(); toast(decision === 'match' ? t('已记录你确认的对应关系。') : t('已记录：这段讲解没有对应课件。')); } catch (error) { toast(error.message, true); button.disabled = false; }
  }
  function renderJob() {
    $('interface-language').disabled = !!isRunning();
    renderRecognitionStatus();
    if (!ui.project) return;
    const job = ui.state.job, show = job && (job.project_id === ui.selected || (!job.project_id && isRunning())) && (isRunning() || ['failed', 'cancelled', 'stopped'].includes(job.state));
    $('job-banner').hidden = !show; $('job-banner').classList.toggle('failed', show && job.state === 'failed');
    if (show) { $('job-title').textContent = job.state === 'failed' ? t('整理暂未完成') : !isRunning() ? t('已停止，完成的内容仍保留') : { asr: t('正在识别录音 · 需要 AI'), ocr: t('正在识别图片文字 · 需要 AI'), download_model: t('正在准备本机语音模型'), candidates: t('正在查找内容候选'), evidence: t('正在提取重点 · 需要 AI'), topics: t('正在归纳主题 · 需要 AI'), import: t('正在整理资料') }[job.stage] || t('正在整理'); $('job-message').textContent = job.message || t('已完成的步骤会保留，请稍候。'); $('job-banner').querySelector('.spinner').hidden = !isRunning(); }
    $('cancel-job').hidden = !show || !isRunning(); $('cancel-job').disabled = ui.stopping || !!job?.cancel_requested; $('cancel-job').textContent = ui.stopping || job?.cancel_requested ? t('正在停止…') : t('停止整理');
    if ($('ai-button')) $('ai-button').disabled = !!isRunning();
    $('export-button').disabled = sameJobRunning();
    $('add-files-button').disabled = !!isRunning() || ui.busy;
    document.querySelectorAll('[data-lock-during-job]').forEach(control => { control.disabled = !!isRunning() || control.dataset.saving === 'true'; });
  }
  function switchTab(tab, reset = true) {
    ui.tab = tab;
    document.querySelectorAll('.reader-tab').forEach(button => { const selected = button.dataset.tab === tab; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); });
    for (const name of ['notes', 'sources', 'transcript']) $(name + '-view').hidden = name !== tab;
    if (reset) $('reader-scroll').scrollTop = 0;
  }

  async function importFiles(files, projectId = null) {
    if (projectId && isRunning()) { toast(t('正在整理，请等本次任务结束后再添加资料。'), true); return; }
    if (projectId && Array.from(ui.sourceDrafts.keys()).some(key => key.startsWith(projectId + '|'))) { toast(t('当前资料还有未保存的文字修订，请先保存或取消本次修订，再添加资料。'), true); return; }
    if (ui.busy) return; setBusy(true);
    try {
      await saveNotes(true);
      let result;
      if (files?.length) {
        const form = new FormData(); for (const file of files) form.append('files', file);
        if (projectId) form.append('project_id', projectId);
        toast(t('正在导入并整理原件，请稍候……'), false, 60000);
        const response = await fetch('/api/upload', { method: 'POST', headers: { 'X-Van-Token': window.VAN_TOKEN, 'X-Van-Language': i18n.language }, body: form }); const payload = await response.json();
        if (!response.ok || payload.ok === false) throw new Error(payload.error || t('导入没有完成。')); result = payload.data || payload;
      } else if (ui.state.native_dialogs) result = await api('import', projectId ? { project_id: projectId } : {});
      else { ui.importTarget = projectId; $('file-input').click(); return; }
      if (result?.needs_upload) { ui.importTarget = projectId; $('file-input').click(); return; }
      if (result?.cancelled) return;
      const project = result?.project || result;
      await refresh(); if (project?.id) await selectProject(project.id);
      toast(projectId ? t('资料已加入当前笔记，原件与修订已保留；对应关系和重点请重新核对。') : t('资料已导入，原件已保留。'));
    } catch (error) { toast(error.message, true, 8000); } finally { setBusy(false); $('file-input').value = ''; }
  }
  async function saveNotes(quiet = false) {
    clearTimeout(ui.notesTimer); const id = ui.selected; if (!id || !ui.drafts.has(id)) return;
    const text = ui.drafts.get(id); $('save-status').textContent = t('保存中…');
    try {
      const project = await api('notes', { id, text }); if (ui.drafts.get(id) === text) ui.drafts.delete(id);
      if (ui.selected === id) { ui.project.manual_notes = project.manual_notes ?? text; $('save-status').textContent = ui.drafts.has(id) ? t('未保存') : t('已保存'); $('save-status').classList.toggle('pending', ui.drafts.has(id)); }
      const item = ui.state.projects.find(p => p.id === id); if (item) item.manual_notes = text;
      if (!quiet) toast(t('记录已保存。'));
    } catch (error) { $('save-status').textContent = t('尚未保存'); toast(error.message, true); throw error; }
  }
  async function organize() {
    if (!ui.project) return;
    if (isRunning()) return;
    const needs = ui.project.pipeline_needs || {}, recognition = ui.state.recognition || {};
    const needsAsr = needs.asr || (ui.project.missing_capabilities || []).includes('speech_to_text');
    const needsOcr = needs.ocr || (ui.project.missing_capabilities || []).includes('ocr');
    if (Array.from(ui.sourceDrafts.keys()).some(key => key.startsWith(ui.selected + '|'))) { toast(t('当前资料还有未保存的文字修订，请先点击“保存修订”。'), true); return; }
    if (needsAsr && recognition.asr?.provider === 'local_whisper' && (recognition.asr.local_engine_available === false || recognition.package_flavor === 'api')) { openSettings('asr'); return; }
    if (ui.project.needs_model_download) { openSettings('asr'); return; }
    if (needsAsr && recognition.asr?.provider === 'openai_compatible_asr' && !recognition.asr.key_configured) { openSettings('asr'); return; }
    if (needsOcr && recognition.ocr?.provider === 'deepseek_vision' && !recognition.ocr.key_configured) { openSettings('ocr'); return; }
    if (needsOcr && recognition.ocr?.provider === 'local_rapidocr' && recognition.ocr.local_engine_available === false) { openSettings('ocr'); return; }
    if (!needsAsr && !needsOcr && !ui.state.connection?.configured) { openSettings('text'); return; }
    try { await saveNotes(true); const job = await api('ai', { id: ui.selected, refresh: false }); ui.state.job = job; ui.lastJob = job.id; renderJob(); await refresh(); } catch (error) { toast(error.message, true); }
  }
  async function cancelJob() {
    if (!isRunning() || ui.stopping) return;
    ui.stopping = true; renderJob();
    try { const result = await api('cancel', {}); if (result?.id) ui.state.job = result; toast(t('正在停止后续步骤，已完成内容和用量会保留。')); await refresh({ reloadProject: true }); }
    catch (error) { ui.stopping = false; toast(error.message, true); renderJob(); }
  }
  async function exportProject() {
    if (!ui.selected) return;
    if (Array.from(ui.sourceDrafts.keys()).some(key => key.startsWith(ui.selected + '|'))) { toast(t('当前资料还有未保存的文字修订，请先保存或取消本次修订，再归档。'), true); return; }
    $('export-button').disabled = true;
    try { await saveNotes(true); const result = await api('export', { id: ui.selected }); if (result.cancelled) return; toast(t('已归档：') + (result.path || t('笔记与原件已保存')), false, 10000); await refresh({ reloadProject: true }); } catch (error) { toast(error.message, true, 8000); } finally { $('export-button').disabled = sameJobRunning(); }
  }

  function openSettings(tab = 'text') {
    if (typeof tab !== 'string') tab = 'text';
    const connection = ui.state.connection || {};
    $('settings-provider').value = connection.provider || 'deepseek'; $('settings-model').value = connection.model || 'deepseek-flash';
    $('settings-key').value = ''; $('settings-key').placeholder = connection.key_configured || connection.key_present ? t('已配置密钥，留空则保持不变') : t('输入你的 API 密钥');
    $('key-state').textContent = connection.key_configured || connection.key_present ? t('已配置') : '';
    $('remember-key').checked = !!connection.remember_key; $('settings-endpoint').value = connection.endpoint || 'https://api.deepseek.com';
    $('settings-thinking').value = ['off', 'low', 'high', 'max'].includes(connection.thinking) ? connection.thinking : connection.thinking === 'enabled' ? connection.reasoning_effort || 'high' : 'off';
    $('settings-error').hidden = true; $('models-hint').textContent = t('读取当前连接实际可用的模型。');
    const recognition = ui.state.recognition || {}, asr = recognition.asr || {}, ocr = recognition.ocr || {};
    $('asr-provider').value = asr.provider === 'openai_compatible_asr' ? asr.provider : 'local_whisper';
    $('asr-language').value = ['auto', 'zh', 'en'].includes(asr.language) ? asr.language : 'auto';
    $('asr-endpoint').value = asr.provider === 'openai_compatible_asr' ? asr.endpoint || '' : '';
    $('asr-api-model').value = asr.provider === 'openai_compatible_asr' ? asr.model || '' : '';
    $('asr-remember-key').checked = !!asr.remember_key;
    $('ocr-provider').value = ocr.provider === 'deepseek_vision' ? ocr.provider : 'local_rapidocr';
    $('ocr-model').value = ocr.provider === 'deepseek_vision' ? ocr.model || 'deepseek-flash' : 'deepseek-flash';
    $('ocr-endpoint').value = ocr.provider === 'deepseek_vision' ? ocr.endpoint || 'https://api.deepseek.com' : 'https://api.deepseek.com';
    $('ocr-remember-key').checked = !!ocr.remember_key;
    for (const kind of ['asr', 'ocr']) { const selected = recognition[kind] || {}; $(kind + '-key').value = ''; $(kind + '-key').placeholder = selected.key_configured ? t('已配置密钥，留空则保持不变') : t('输入此识别连接的 API 密钥'); $(kind + '-key-state').textContent = selected.key_configured ? t('已配置') : ''; }
    fillModels(connection.models || []); providerFields(); recognitionProviderFields(); setSettingsTab(tab); renderRecognitionStatus();
    if (!$('settings-dialog').open) $('settings-dialog').showModal();
  }
  function setSettingsTab(tab) {
    ui.settingsTab = ['text', 'asr', 'ocr'].includes(tab) ? tab : 'text';
    document.querySelectorAll('.capability-tab').forEach(button => { const selected = button.dataset.setting === ui.settingsTab; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); });
    for (const kind of ['text', 'asr', 'ocr']) $('settings-' + kind).hidden = kind !== ui.settingsTab;
    $('settings-' + ui.settingsTab).scrollTop = 0;
    $('settings-save').textContent = { text: t('保存连接'), asr: t('保存语音设置'), ocr: t('保存识字设置') }[ui.settingsTab];
    $('settings-error').hidden = true;
  }
  function recognitionProviderFields() {
    const localAsr = $('asr-provider').value === 'local_whisper', localOcr = $('ocr-provider').value === 'local_rapidocr';
    $('asr-local-settings').hidden = !localAsr; $('asr-api-settings').hidden = localAsr;
    $('ocr-local-settings').hidden = !localOcr; $('ocr-api-settings').hidden = localOcr;
  }
  function renderRecognitionStatus() {
    const recognition = ui.state.recognition || {}, model = recognition.asr?.local_model || {}, job = ui.state.job;
    const ready = model.ready === true, downloading = isRunning() && job?.stage === 'download_model', noLocalEngine = recognition.asr?.local_engine_available === false || recognition.package_flavor === 'api';
    $('asr-model-status').textContent = noLocalEngine ? t('此版本不含本机语音识别引擎') : ready ? t('模型已就绪 · 可以离线识别') : t('首次使用需准备本机模型');
    $('download-model').disabled = !!isRunning() || ready || noLocalEngine;
    $('download-model').textContent = noLocalEngine ? t('此版本不准备本机语音模型') : ready ? t('本机语音模型已就绪') : downloading ? t('正在准备本机模型…') : t('准备本机语音模型 · 约 ') + (bytes(model.download_bytes || model.total_bytes) || '486 MB');
    const progress = job?.progress || {}, current = progress.downloaded_bytes ?? progress.current_bytes ?? job?.downloaded_bytes ?? job?.current_bytes, total = progress.total_bytes ?? job?.total_bytes ?? model.download_bytes;
    const fraction = typeof job?.progress === 'number' && Number.isFinite(job.progress) ? Math.max(0, Math.min(1, job.progress)) : null;
    const ratio = fraction !== null ? fraction : Number(current) >= 0 && Number(total) > 0 ? Math.min(1, Number(current) / Number(total)) : null;
    $('model-download-progress').hidden = !downloading; if (ratio !== null) $('model-download-progress').value = ratio; else $('model-download-progress').removeAttribute('value');
    $('model-download-message').textContent = noLocalEngine ? t('API 轻量版不含本机识别引擎，可选择语音 API，或使用完整识别版；无需下载无法使用的模型。') : downloading ? (job.message || t('正在下载，请稍候。')) + (ratio !== null ? ' ' + Math.round(ratio * 100) + '%' + (Number(current) >= 0 && Number(total) > 0 ? ' · ' + bytes(current) + ' / ' + bytes(total) : '') : '') : ready ? t('已完成的模型会复用，不会在每次整理时下载。') : t('只在你点击准备后下载；也可以选择语音 API。');
    $('ocr-engine-status').textContent = recognition.ocr?.local_engine_available === false ? recognition.package_flavor === 'api' ? t('API 轻量版请使用图像 API，或选择完整识别版') : t('本机组件不可用，可改选图像 API') : recognition.ocr?.local_engine_available === true ? t('本机识字组件已就绪') : t('由软件管理，开始时检查组件');
    if ($('cancel-download')) { $('cancel-download').hidden = !downloading; $('cancel-download').disabled = ui.stopping || !!job?.cancel_requested; }
  }
  function recognitionPayload(kind) {
    if (kind === 'asr') {
      const remote = $('asr-provider').value === 'openai_compatible_asr';
      const values = { provider: $('asr-provider').value, model: remote ? $('asr-api-model').value.trim() : 'small', language: $('asr-language').value, remember_key: remote && $('asr-remember-key').checked };
      if (remote) { values.endpoint = $('asr-endpoint').value.trim(); if ($('asr-key').value) values.api_key = $('asr-key').value; }
      return values;
    }
    const remote = $('ocr-provider').value === 'deepseek_vision', values = { provider: $('ocr-provider').value, remember_key: remote && $('ocr-remember-key').checked };
    if (remote) { values.model = $('ocr-model').value.trim(); values.endpoint = $('ocr-endpoint').value.trim(); if ($('ocr-key').value) values.api_key = $('ocr-key').value; }
    return values;
  }
  async function saveRecognition(kind, close = true) {
    if (isRunning()) throw new Error(t('正在处理，请在本次任务结束后修改设置。'));
    const result = await api('recognition_settings', { [kind]: recognitionPayload(kind) });
    ui.state.recognition = result.recognition || result; $(kind + '-key').value = '';
    if (close) { $('settings-dialog').close(); toast(kind === 'asr' ? t('语音识别设置已保存。') : t('图片识字设置已保存。')); }
    await refresh({ reloadProject: true }); renderRecognitionStatus();
  }
  async function downloadModel() {
    if (isRunning()) return;
    if (ui.state.recognition?.asr?.local_engine_available === false || ui.state.recognition?.package_flavor === 'api') { $('settings-error').textContent = t('此版本不含本机语音引擎，请选择语音 API 或使用完整识别版。'); $('settings-error').hidden = false; return; }
    $('download-model').disabled = true; $('settings-error').hidden = true;
    try { await saveRecognition('asr', false); const result = await api('download_model', {}), job = result.job || result; ui.state.job = job; ui.lastJob = job.id; renderRecognitionStatus(); renderJob(); await refresh({ reloadProject: true }); }
    catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; renderRecognitionStatus(); }
  }
  function providerFields(changed = false) {
    const remote = $('settings-provider').value === 'deepseek';
    $('remote-settings').hidden = !remote; $('thinking-settings').hidden = !remote; $('local-thinking-hint').hidden = remote;
    if (changed) { $('settings-endpoint').value = remote ? 'https://api.deepseek.com' : 'http://127.0.0.1:8080'; $('settings-model').value = remote ? 'deepseek-flash' : ''; $('models-hint').textContent = t('读取当前连接实际可用的模型。'); fillModels([]); }
  }
  function fillModels(models) { $('model-options').replaceChildren(); for (const model of models) { const id = typeof model === 'string' ? model : model.id || model.model; if (!id) continue; const option = el('option'); option.value = id; $('model-options').append(option); } }
  function settingsPayload() {
    const remote = $('settings-provider').value === 'deepseek';
    const payload = { provider: $('settings-provider').value, model: $('settings-model').value.trim(), endpoint: $('settings-endpoint').value.trim(), thinking: remote ? $('settings-thinking').value : 'off', remember_key: $('remember-key').checked };
    if (remote && $('settings-key').value) payload.api_key = $('settings-key').value; return payload;
  }
  async function saveSettings(loadModels = false) {
    if (isRunning()) { $('settings-error').textContent = t('正在整理，请在本次任务结束后修改连接。'); $('settings-error').hidden = false; return; }
    const button = loadModels ? $('load-models') : $('settings-save'); button.disabled = true; $('settings-error').hidden = true;
    try {
      if (loadModels) {
        const probe = settingsPayload(); delete probe.remember_key;
        const result = await api('models', probe), models = result.models || result || [];
        fillModels(models);
        if (!$('settings-model').value.trim() && models.length) $('settings-model').value = typeof models[0] === 'string' ? models[0] : models[0].id || models[0].model || '';
        $('models-hint').textContent = models.length ? t('已读取 ') + models.length + t(' 个可用模型；选择后点“保存连接”生效。') : t('连接没有返回可用模型。');
      } else {
        ui.state.connection = await api('settings', settingsPayload()); $('settings-key').value = '';
        $('settings-dialog').close(); toast(t('AI 连接已保存。')); await refresh({ reloadProject: true });
      }
    } catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; } finally { button.disabled = false; }
  }

  $('menu-button').addEventListener('click', () => { const collapsed = document.body.classList.toggle('nav-collapsed'); $('menu-button').setAttribute('aria-label', collapsed ? t('展开导航') : t('收起导航')); });
  document.querySelector('.brand').addEventListener('click', async (event) => { event.preventDefault(); try { await saveNotes(true); } catch (_) { return; } ui.selected = null; ui.project = null; $('audio-player').pause(); renderList(); renderReader(); });
  for (const id of ['import-button', 'welcome-import']) $(id).addEventListener('click', () => importFiles());
  $('add-files-button').addEventListener('click', () => { if (ui.selected) importFiles(undefined, ui.selected); });
  $('file-input').addEventListener('change', () => { const target = ui.importTarget; ui.importTarget = null; if ($('file-input').files.length) importFiles(Array.from($('file-input').files), target); });
  $('file-input').addEventListener('cancel', () => { ui.importTarget = null; });
  $('refresh-button').addEventListener('click', () => refresh({ reloadProject: true }));
  $('search').addEventListener('input', () => { ui.query = $('search').value.trim().toLowerCase(); renderList(); });
  document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => { ui.view = button.dataset.view; document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item === button)); renderList(); }));
  document.querySelectorAll('.reader-tab').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));
  $('page-prev').addEventListener('click', () => { ui.page--; renderPages(); }); $('page-next').addEventListener('click', () => { ui.page++; renderPages(); });
  $('transcript-search').addEventListener('input', renderTranscript);
  $('manual-notes').addEventListener('input', () => { if (!ui.selected) return; ui.drafts.set(ui.selected, $('manual-notes').value); $('save-status').textContent = t('未保存'); $('save-status').classList.add('pending'); clearTimeout(ui.notesTimer); ui.notesTimer = setTimeout(() => saveNotes(true).catch(() => {}), 700); });
  $('save-notes').addEventListener('click', () => saveNotes().catch(() => {})); $('ai-button').addEventListener('click', organize); $('export-button').addEventListener('click', exportProject);
  $('open-export-button').addEventListener('click', async () => { try { const result = await api('open_export', { id: ui.selected }); if (!result.opened) toast(t('归档位置：') + result.path, false, 10000); } catch (error) { toast(error.message, true); } });
  for (const id of ['settings-button', 'model-button']) $(id).addEventListener('click', openSettings);
  $('settings-close').addEventListener('click', () => $('settings-dialog').close());
  $('settings-dialog').addEventListener('close', () => { for (const id of ['settings-key', 'asr-key', 'ocr-key']) $(id).value = ''; });
  $('settings-form').addEventListener('submit', async (event) => { event.preventDefault(); if (ui.settingsTab === 'text') { saveSettings(false); return; } const button = $('settings-save'); button.disabled = true; $('settings-error').hidden = true; try { await saveRecognition(ui.settingsTab); } catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; } finally { button.disabled = false; } });
  $('load-models').addEventListener('click', () => saveSettings(true)); $('settings-provider').addEventListener('change', () => providerFields(true));
  document.querySelectorAll('.capability-tab').forEach(button => button.addEventListener('click', () => setSettingsTab(button.dataset.setting)));
  for (const id of ['asr-provider', 'ocr-provider']) $(id).addEventListener('change', recognitionProviderFields);
  $('download-model').addEventListener('click', downloadModel); $('cancel-job').addEventListener('click', cancelJob);
  if ($('cancel-download')) $('cancel-download').addEventListener('click', cancelJob);
  $('toast').querySelector('button').addEventListener('click', () => { $('toast').hidden = true; });
  document.addEventListener('keydown', (event) => { const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName); if (event.key === '/' && !typing && !$('settings-dialog').open) { event.preventDefault(); $('search').focus(); } if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && ui.project && !$('settings-dialog').open) { event.preventDefault(); saveNotes().catch(() => {}); } });
  let dragDepth = 0;
  document.addEventListener('dragenter', (event) => { if (event.dataTransfer?.types.includes('Files')) { event.preventDefault(); dragDepth++; $('drop-overlay').hidden = false; } });
  document.addEventListener('dragover', (event) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
  document.addEventListener('dragleave', (event) => { if (event.dataTransfer?.types.includes('Files')) { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('drop-overlay').hidden = true; } });
  document.addEventListener('drop', (event) => { if (event.dataTransfer?.files.length) { event.preventDefault(); dragDepth = 0; $('drop-overlay').hidden = true; importFiles(Array.from(event.dataTransfer.files)); } });
  $('interface-language').addEventListener('change', async () => {
    const language = $('interface-language').value;
    if (isRunning() || ui.sourceDrafts.size) {
      $('interface-language').value = i18n.language;
      toast(t('切换语言前请停止任务，并保存或取消文字修订。'), true); return;
    }
    try { await saveNotes(true); await api('preferences', { language }); window.location.reload(); }
    catch (error) { $('interface-language').value = i18n.language; toast(error.message, true); }
  });
  refresh();
})();
