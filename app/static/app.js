'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const ui = {
    actor: $('actor-select'), role: $('actor-role'), input: $('message-input'), form: $('message-form'),
    send: $('send-button'), messages: $('messages'), quick: $('quick-actions'), summary: $('summary'),
    content: $('state-content'), heading: $('state-section-heading'), count: $('state-count'),
    refresh: $('refresh-button'), start: $('start-button'), error: $('request-error'), retry: $('retry-button'),
    announcement: $('status-announcement'), botStatus: $('bot-status'), help: $('scenario-help'), steps: $('scenario-steps'),
  };
  const model = { actors: [], actorId: null, state: null, view: 'hackathon', busy: false,
    sequence: 0, controller: null, retry: null, pending: new Map(), drafts: new Map(),
    maxStatus: null, maxLoading: false, inviteBusy: false, inviteEpoch: 0, inviteTimer: null };
  const themeButtons = [...document.querySelectorAll('[data-theme-choice]')];
  let systemTheme;
  try { systemTheme = window.matchMedia('(prefers-color-scheme: dark)'); } catch { systemTheme = null; }
  let savedTheme = null;
  try { savedTheme = localStorage.getItem('ideyafix-theme'); } catch { /* An unavailable preference store does not block the demo. */ }
  if (!['light', 'dark'].includes(savedTheme)) savedTheme = null;
  function setTheme(theme, persist = false) {
    const value = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = value;
    themeButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === value)));
    if (persist) {
      savedTheme = value;
      try { localStorage.setItem('ideyafix-theme', value); } catch { /* The chosen theme remains active for this page. */ }
    }
  }
  setTheme(['light', 'dark'].includes(savedTheme) ? savedTheme : systemTheme?.matches ? 'dark' : 'light');
  themeButtons.forEach(button => button.addEventListener('click', () => setTheme(button.dataset.themeChoice, true)));
  systemTheme?.addEventListener?.('change', event => { if (!savedTheme) setTheme(event.matches ? 'dark' : 'light'); });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }
  function announce(text) { ui.announcement.textContent = text; }
  function updateMaxControls() {
    const status = model.maxStatus;
    $('max-connect').disabled = model.busy || model.inviteBusy || model.maxLoading || !model.state
      || !status?.configured || !status.enabled || !status.workerAlive || status.mode !== 'long_polling';
    $('max-refresh').disabled = model.maxLoading || model.inviteBusy;
  }
  function clearMaxInvite() {
    model.inviteEpoch += 1; model.inviteBusy = false;
    clearTimeout(model.inviteTimer); model.inviteTimer = null;
    $('max-invite-link').removeAttribute('href'); $('max-invite').hidden = true;
    $('max-invite-expiry').textContent = ''; $('max-error').hidden = true;
    updateMaxControls();
  }
  async function loadMaxStatus() {
    if (model.maxLoading) return;
    clearMaxInvite(); model.maxLoading = true; updateMaxControls();
    $('max-status').textContent = 'Проверяем локальный процесс…';
    try {
      const status = await request('/api/max/status');
      if (typeof status.configured !== 'boolean' || typeof status.enabled !== 'boolean'
        || typeof status.workerAlive !== 'boolean' || typeof status.sendingEnabled !== 'boolean'
        || !['long_polling', 'disabled'].includes(status.mode) || status.botUsername !== 'ideya_fiks_150_bot') throw new Error('invalid_max_status');
      model.maxStatus = status;
      document.body.classList.toggle('platform-console', status.platformEnabled === true);
      if (status.platformEnabled === true) {
        $('identity-heading').textContent = 'Профиль для приглашения в MAX';
        document.querySelector('.identity-note p').textContent = 'Создайте одноразовую ссылку для выбранного личного профиля. Получатель будет работать внутри MAX с закреплёнными правами.';
        document.querySelector('.page-intro h1').textContent = 'Идеяфикс в MAX';
        document.querySelector('.max-note').textContent = 'Все предметные данные вымышлены. Обычный пользователь получает закреплённый профиль. Тестовый оператор выбирает роли через /role. Компьютер и приёмник должны работать.';
        document.querySelector('footer span:last-child').textContent = 'Тестовые данные. Личные действия выполняются в MAX.';
      }
      $('demo-label-text').textContent = status.platformEnabled ? 'Тестовые данные · личные кабинеты MAX' : status.configured ? 'Тестовые данные · MAX и локальное демо' : 'Тестовый стенд • имитация MAX';
      $('max-status').textContent = !status.configured ? 'Подключение MAX ещё не настроено.'
        : !status.enabled || status.mode !== 'long_polling' ? 'Локальный приём MAX выключен.'
          : !status.workerAlive ? 'Процесс MAX не работает. Запустите его и обновите статус.'
            : status.sendingEnabled ? 'Long Polling работает · ответы включены.' : 'Long Polling работает · ответы MAX выключены.';
      const counts = status.counts;
      const validCounts = counts && ['queued', 'accepted', 'rejected', 'unknown'].every(key => Number.isInteger(counts[key]) && counts[key] >= 0);
      $('max-counts').hidden = !validCounts;
      if (validCounts) $('max-counts').textContent = `В очереди: ${counts.queued} · Принято MAX: ${counts.accepted} · Отклонено: ${counts.rejected} · Без подтверждения: ${counts.unknown}`;
    } catch {
      model.maxStatus = null; $('max-counts').hidden = true;
      $('max-status').textContent = document.body.classList.contains('platform-console')
        ? 'Статус MAX недоступен. Проверьте локальный приёмник и обновите статус.'
        : 'Статус MAX недоступен. Локальный чат продолжает работать. Обновите статус позднее.';
    } finally { model.maxLoading = false; updateMaxControls(); }
  }
  async function connectMax() {
    if ($('max-connect').disabled) return;
    clearMaxInvite(); const epoch = model.inviteEpoch; const actorId = model.actorId;
    model.inviteBusy = true; updateMaxControls();
    try {
      const result = await request('/api/max/invite', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ actor_id: actorId }) });
      if (epoch !== model.inviteEpoch || actorId !== model.actorId) return;
      const url = new URL(result.url);
      const token = url.searchParams.get('start'); const expiry = new Date(result.expiresAt).getTime();
      if (url.origin !== 'https://max.ru' || url.pathname !== '/ideya_fiks_150_bot' || url.username || url.password
        || url.hash || [...url.searchParams].length !== 1 || !/^[A-Za-z0-9_-]{16,256}$/.test(token || '')
        || !Number.isFinite(expiry) || expiry <= Date.now()) throw new Error('invalid_max_invite');
      $('max-invite-link').href = url.href;
      $('max-invite-expiry').textContent = 'Одноразовая ссылка для ' + model.state.actor.name + '. Действует 15 минут, до '
        + new Date(expiry).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) + '.';
      $('max-invite').hidden = false;
      model.inviteTimer = setTimeout(clearMaxInvite, Math.min(expiry - Date.now(), 900000));
      announce('Личная ссылка MAX готова. Откройте бота в отдельной вкладке.');
    } catch {
      if (epoch === model.inviteEpoch) { $('max-error').textContent = 'Не удалось создать ссылку. Обновите статус MAX и попробуйте ещё раз.'; $('max-error').hidden = false; }
    } finally { if (epoch === model.inviteEpoch) { model.inviteBusy = false; updateMaxControls(); } }
  }
  function setBusy(busy, text) {
    model.busy = busy;
    const ready = model.actors.length > 0 && model.state !== null;
    ui.actor.disabled = busy || model.actors.length === 0;
    ui.input.disabled = busy || !ready;
    ui.send.disabled = busy || !ready || !ui.input.value.trim();
    ui.refresh.disabled = busy || !ready;
    ui.start.disabled = busy || !ready;
    document.querySelectorAll('[data-scenario], .quick-action, [data-command]').forEach(button => { button.disabled = busy || !ready; });
    document.querySelectorAll('.chat-panel, .state-panel').forEach(panel => panel.setAttribute('aria-busy', String(busy)));
    ui.botStatus.replaceChildren(node('span', 'status-dot'), document.createTextNode(text || (ready ? 'Готов к диалогу · локальный режим' : 'Выберите пользователя')));
    if (text) announce(text);
    updateMaxControls();
  }
  function hideError() { ui.error.hidden = true; model.retry = null; }
  function showError(error, retry, sending = false) {
    $('error-title').textContent = sending ? 'Ответ пока не получен' : 'Не удалось обновить стенд';
    $('error-message').textContent = error?.status === 409
      ? 'Этот ID события уже использован с другим содержимым. Обновите диалог перед новой отправкой.'
      : sending
        ? 'Проверьте локальный сервер и повторите запрос. Повтор использует тот же ID события и не создаёт второе действие.'
        : 'Проверьте, что локальный сервер запущен. Данные на экране могут быть неактуальны.';
    ui.retry.textContent = sending ? 'Повторить отправку' : 'Повторить загрузку';
    model.retry = retry;
    ui.error.hidden = false;
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const externalSignal = options.signal;
    const cancel = () => controller.abort();
    if (externalSignal?.aborted) cancel();
    externalSignal?.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options,
        signal: controller.signal,
        headers: { Accept: 'application/json', ...options.headers } });
      if (!response.ok) {
        const failure = new Error('demo_request_failed'); failure.status = response.status; throw failure;
      }
      return await response.json();
    } finally { clearTimeout(timer); externalSignal?.removeEventListener('abort', cancel); }
  }
  function verifyState(state, actorId) {
    if (!state || state.actor?.id !== actorId || !Array.isArray(state.messages) || !Array.isArray(state.teams)
      || !Array.isArray(state.assignments) || !Array.isArray(state.quickActions)
      || state.mode !== 'synthetic' || state.realProviderCalls !== 0) throw new Error('invalid_synthetic_state');
    return state;
  }
  function timeLabel(value) {
    if (value === undefined || value === null) return '';
    const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  function emptyState(title, text) {
    const box = node('div', 'empty-state');
    box.append(node('span', 'empty-symbol', '✦'), node('strong', '', title), node('p', 'empty-copy', text));
    box.firstElementChild.setAttribute('aria-hidden', 'true');
    return box;
  }
  function renderMessages(state, animateFrom = Infinity) {
    const fragment = document.createDocumentFragment();
    if (!state.messages.length) {
      const empty = node('div', 'chat-empty');
      empty.append(node('span', 'empty-symbol', '✦'), node('h3', '', 'Начнём с простого'),
        node('p', '', 'Нажмите «Показать меню бота» или выберите быстрое действие. Ответ появится здесь.'));
      empty.firstElementChild.setAttribute('aria-hidden', 'true');
      fragment.append(empty);
    }
    state.messages.forEach((message, index) => {
      // Directions use native MAX semantics: incoming is the actor's message to the bot.
      const fromActor = message.direction === 'incoming';
      const item = node('div', 'message' + (fromActor ? ' outgoing' : '') + (index >= animateFrom ? ' is-new' : ''));
      const bubble = node('div', 'message-bubble', message.text);
      const time = timeLabel(message.createdAt);
      if (time) bubble.append(node('time', '', time));
      item.append(node('div', 'message-author', fromActor ? state.actor.name : 'Идеяфикс'), bubble);
      fragment.append(item);
    });
    ui.messages.replaceChildren(fragment);
    ui.messages.scrollTop = ui.messages.scrollHeight;
  }
  function commandButton(label, command, prepare = false) {
    const button = node('button', 'button', label);
    button.type = 'button'; button.dataset.command = command;
    button.addEventListener('click', () => prepare ? prepareCommand(command) : send(command));
    return button;
  }
  function prepareCommand(text) {
    if (model.busy) return;
    ui.input.value = text; updateDraft(); ui.input.focus();
    ui.input.setSelectionRange(text.length, text.length);
    announce('Команда подготовлена. Можно изменить текст и отправить.');
  }
  function renderQuickActions(state) {
    const allowed = model.view === 'education'
      ? new Set(['/start', '/education', '/lessons', '/task', '/submit', '/review', '/progress'])
      : new Set(['/start', '/hackathon', '/team', '/join', '/idea', '/apply', '/applications', '/approve', '/return']);
    const actions = state.quickActions.filter(action => typeof action.text === 'string' && allowed.has(action.text.trim().split(/\s/)[0]));
    ui.quick.replaceChildren(...actions.map(action => {
      const button = node('button', 'quick-action', action.label); button.type = 'button'; button.title = action.text;
      button.addEventListener('click', () => send(action.text)); return button;
    }));
    $('quick-hint').textContent = 'Для роли: ' + state.actor.label;
  }
  const applicationLabels = { submitted: 'На рассмотрении', accepted: 'Принята', revision: 'На доработке' };
  const assignmentLabels = { assigned: 'Новое задание', submitted: 'На проверке', accepted: 'Принято', revision: 'На доработке' };
  function badge(label, good = false) { return node('span', 'state-badge' + (good ? ' good' : ''), label); }
  function labelValue(parent, label, value) {
    parent.append(node('p', 'record-label', label), node('p', '', value));
  }
  function teamCard(team, actor) {
    const card = node('article', 'record');
    const heading = node('div', 'record-heading');
    heading.append(node('h4', '', team.name));
    if (team.application) heading.append(badge(applicationLabels[team.application.status] || 'Заявка', team.application.status === 'accepted'));
    card.append(heading);
    const members = Array.isArray(team.members) ? team.members.join(', ') : '';
    card.append(node('p', 'members', members || 'Участники ещё не добавлены'));
    const code = node('p', ''); code.append(document.createTextNode('Код команды: '), node('span', 'team-code', team.code)); card.append(code);
    const flow = node('div', 'workflow'); flow.setAttribute('aria-label', 'Этапы команды');
    [['Команда', true], ['Идея', Boolean(team.idea)], ['Заявка', Boolean(team.application)]].forEach(([label, done], index) => {
      if (index) { const arrow = node('span', 'workflow-arrow', '→'); arrow.setAttribute('aria-hidden', 'true'); flow.append(arrow); }
      flow.append(node('span', 'workflow-step' + (done ? ' done' : ''), label));
    }); card.append(flow);
    if (team.idea) { labelValue(card, 'Идея', team.idea.title); card.append(node('p', '', team.idea.description)); }
    else card.append(node('p', '', 'Идея пока не добавлена.'));
    if (team.application) {
      labelValue(card, 'Заявка ' + team.application.id, applicationLabels[team.application.status] || team.application.status);
      if (team.application.comment) card.append(node('p', '', 'Комментарий: ' + team.application.comment));
      if (actor.role === 'organizer' && team.application.status === 'submitted') {
        const actions = node('div', 'record-actions');
        actions.append(commandButton('Принять заявку', '/approve ' + team.application.id),
          commandButton('Вернуть с комментарием', '/return ' + team.application.id + ' | Уточните способ проверки гипотезы.', true));
        card.append(actions);
      }
    }
    return card;
  }
  function assignmentCard(task) {
    const card = node('article', 'record');
    const heading = node('div', 'record-heading');
    heading.append(node('h4', '', task.title), badge(assignmentLabels[task.state] || 'Задание', task.state === 'accepted'));
    card.append(heading, node('p', '', task.instructions));
    card.append(node('p', 'members', 'Ученик: ' + task.learner.name + ' · Преподаватель: ' + task.mentor.name));
    if (task.submission) labelValue(card, 'Ответ · попытка ' + task.submission.attempt, task.submission.text);
    if (task.review) labelValue(card, task.review.decision === 'accepted' ? 'Принято преподавателем' : 'Обратная связь', task.review.comment);
    return card;
  }
  function renderState(state) {
    const educational = model.view === 'education';
    const metrics = educational
      ? [['Заданий', state.summary.assignments], ['На проверке', state.assignments.filter(task => task.state === 'submitted').length], ['Принято', state.summary.acceptedAssignments]]
      : [['Команд', state.summary.teams], ['Идей', state.summary.ideas], ['Заявок', state.summary.applications]];
    ui.summary.replaceChildren(...metrics.map(([label, value]) => {
      const metric = node('div', 'metric'); metric.append(node('strong', '', Number.isInteger(value) ? value : '—'), node('span', '', label)); return metric;
    }));
    ui.heading.textContent = educational ? 'Учебные задания' : 'Команды и идеи';
    const records = educational ? state.assignments : state.teams;
    ui.count.textContent = String(records.length);
    ui.content.replaceChildren(...records.map(record => educational ? assignmentCard(record) : teamCard(record, state.actor)));
    if (!records.length) {
      ui.content.append(educational
        ? emptyState('Здесь появится учебная работа', ['mentor', 'learner'].includes(state.actor.role)
          ? 'Преподаватель выдаёт задание, ученик отправляет ответ. Результат проверки будет виден в этой карточке.'
          : 'Для учебного сценария выберите Сашу Демо или Марину Демо в списке тестовых пользователей.')
        : emptyState('У каждой идеи будет своя команда', 'Создайте первую команду через быстрое действие. Затем можно пригласить второго участника по коду.'));
    }
    ui.help.textContent = educational ? 'Пройдите короткий цикл: задание → ответ → обратная связь.' : 'Соберите команду, сохраните идею и передайте заявку организатору.';
    const steps = educational
      ? ['Марина Демо выдаёт задание Саше Демо.', 'Саша отправляет ответ. Марина принимает его или просит доработать.', 'После доработки Саша сдаёт повторно; номер попытки сохраняется.']
      : ['Лера создаёт команду; Тимур может войти по её коду.', 'Участник описывает идею и подаёт заявку.', 'Роман Демо принимает заявку или возвращает её с комментарием.'];
    ui.steps.replaceChildren(...steps.map(text => node('li', '', text)));
    document.querySelectorAll('[data-scenario]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.scenario === model.view)));
  }
  function applyState(state, animate = false) {
    const previousCount = model.state?.actor.id === state.actor.id ? model.state.messages.length : Infinity;
    model.state = state; ui.role.textContent = state.actor.label;
    renderMessages(state, animate ? previousCount : Infinity); renderQuickActions(state); renderState(state);
  }
  function updateDraft() {
    $('message-count').textContent = ui.input.value.length + ' / 2000';
    if (model.actorId !== null) model.drafts.set(model.actorId, ui.input.value);
    ui.send.disabled = model.busy || !model.state || !ui.input.value.trim();
  }
  async function loadActor(actorId, initial = false) {
    if (model.busy && !initial) return;
    clearMaxInvite();
    model.controller?.abort(); const controller = new AbortController(); model.controller = controller;
    const sequence = ++model.sequence; model.actorId = actorId;
    const actor = model.actors.find(item => item.id === actorId);
    if (!actor) return;
    model.state = null; ui.role.textContent = actor.label;
    ui.messages.replaceChildren(node('p', 'empty-copy', 'Загружаем диалог ' + actor.name + '…'));
    ui.quick.replaceChildren(); ui.summary.replaceChildren();
    ui.content.replaceChildren(node('p', 'empty-copy', 'Загружаем состояние выбранного пользователя…'));
    ui.count.textContent = '—';
    model.view = ['mentor', 'learner'].includes(actor.role) ? 'education' : 'hackathon';
    ui.actor.value = String(actorId); ui.input.value = model.drafts.get(actorId) || ''; updateDraft();
    hideError(); setBusy(true, 'Загружаем диалог…');
    try {
      const state = verifyState(await request('/api/demo/state?actor_id=' + encodeURIComponent(actorId), { signal: controller.signal }), actorId);
      if (sequence !== model.sequence) return;
      applyState(state); announce('Открыт профиль: ' + actor.name + '. ' + actor.label + '.');
    } catch (error) {
      if (sequence === model.sequence && !controller.signal.aborted) showError(error, () => loadActor(actorId));
    } finally { if (sequence === model.sequence) setBusy(false); }
  }
  async function refreshState() {
    if (model.busy || model.actorId === null) return;
    clearMaxInvite();
    hideError(); setBusy(true, 'Обновляем состояние…');
    try { applyState(verifyState(await request('/api/demo/state?actor_id=' + encodeURIComponent(model.actorId)), model.actorId)); announce('Диалог и состояние обновлены.'); }
    catch (error) { showError(error, refreshState); }
    finally { setBusy(false); }
  }
  function eventId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
  }
  async function send(text, exactPending = null) {
    if (model.busy || !model.state || typeof text !== 'string' || !text.trim()) return;
    const actorId = model.actorId;
    const existing = model.pending.get(actorId);
    const payload = exactPending || (existing?.text === text.trim() ? existing : { actor_id: actorId, text: text.trim(), event_id: eventId() });
    model.pending.set(actorId, payload); hideError(); setBusy(true, 'Идеяфикс готовит ответ…');
    try {
      const result = await request('/api/demo/events', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const state = verifyState(result.state, actorId);
      if (payload.text === '/education') model.view = 'education';
      if (payload.text === '/hackathon') model.view = 'hackathon';
      model.pending.delete(actorId); ui.input.value = ''; updateDraft(); applyState(state, true);
      announce(result.duplicate ? 'Сохранённый ответ получен. Действие не повторено.' : 'Ответ Идеяфикса получен. Состояние обновлено.');
    } catch (error) {
      ui.input.value = payload.text; updateDraft();
      showError(error, () => model.actorId === actorId ? send(payload.text, payload) : loadActor(actorId), true);
    } finally { setBusy(false); }
  }
  async function bootstrap() {
    hideError(); setBusy(true, 'Загружаем стенд…');
    try {
      const result = await request('/api/demo/bootstrap');
      if (result.mode !== 'synthetic' || result.realProviderCalls !== 0 || !Array.isArray(result.actors) || !result.actors.length
        || !result.actors.every(actor => Number.isInteger(actor.id) && typeof actor.name === 'string' && typeof actor.role === 'string' && typeof actor.label === 'string')) throw new Error('invalid_synthetic_bootstrap');
      model.actors = result.actors;
      ui.actor.replaceChildren(...result.actors.map(actor => { const option = node('option', '', actor.name + ' · ' + actor.label); option.value = String(actor.id); return option; }));
      const selected = result.actors.some(actor => actor.id === result.selectedActorId) ? result.selectedActorId : result.actors[0].id;
      await loadActor(selected, true);
    } catch (error) { showError(error, bootstrap); setBusy(false); }
  }
  ui.form.addEventListener('submit', event => { event.preventDefault(); send(ui.input.value); });
  ui.input.addEventListener('input', updateDraft);
  ui.input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); ui.form.requestSubmit(); } });
  ui.actor.addEventListener('change', () => loadActor(Number(ui.actor.value)));
  ui.refresh.addEventListener('click', refreshState);
  ui.start.addEventListener('click', () => send('/start'));
  ui.retry.addEventListener('click', () => { const retry = model.retry; if (!model.busy) retry?.(); });
  $('max-refresh').addEventListener('click', loadMaxStatus);
  $('max-connect').addEventListener('click', connectMax);
  document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => send('/' + button.dataset.scenario)));
  bootstrap();
  loadMaxStatus();
})();
