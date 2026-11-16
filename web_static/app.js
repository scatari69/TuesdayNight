'use strict';

const $ = (id) => document.getElementById(id);
const {t, translateError, dateLocale, applyStatic, getLanguage, setLanguage} = window.I18n;
const sections = {
  overview: {name: 'Обзор', icon: '◈', title: 'Всё под контролем', description: 'Ваш сервер, настройки и ближайшие игровые сборы.'},
  deadlock: {name: 'Deadlock', icon: '↗', title: 'Время собираться', description: 'Пригласите игроков, настройте расписание и посмотрите статистику.'},
  reactionrole: {name: 'Роли по реакциям', icon: '✳', title: 'Каждому — своя игра', description: 'Создавайте панели и связывайте реакции с ролями сервера.'},
  dashboard: {name: 'WoW-дашборд', icon: '▤', title: 'Прогресс вашей гильдии', description: 'Настройте гильдию, обновляйте данные и управляйте дашбордом в Discord.'},
  mplus: {name: 'Mythic+', icon: '+', title: 'Пять игроков. Один ключ.', description: 'Создавайте группы, выбирайте специализацию и управляйте составом.'},
  tools: {name: 'Очистка', icon: '⌁', title: 'Порядок в каналах', description: 'Удаляйте сообщения бота за последние сутки в выбранном канале.'}
};
const commandsText = {
  setup: ['Настроить сборы', 'Выберите роль игроков и канал для приглашений.'],
  tag: ['Позвать игроков', 'Приглашение получат участники роли, которые ещё не подтвердили участие.'],
  team: ['Состав игроков', 'Текущие участники настроенной роли без ботов.'],
  settings: ['Расписание и задержка', 'Оставьте поле пустым, чтобы сохранить настройку. Время — в часовом поясе бота.'],
  clear: ['Закрыть сбор', 'Сбросить голоса и закрыть действующие карточки сбора.'],
  stats: ['Статистика сервера', 'Количество сборов, активные организаторы и отказы.'],
  me: ['Моя статистика', 'Ваше участие в сборах и статус в составе игроков.'],
  cleanup: ['Удалить сообщения бота', 'Сообщения за последние 24 часа в канале выполнения. Панели ролей тоже будут удалены.'],
  'reactionrole panel': ['Создать панель ролей', 'Опубликуйте сообщение и используйте его ID для привязки реакций.'],
  'reactionrole add': ['Добавить реакцию', 'Свяжите эмодзи с ролью без серверных разрешений, ниже роли бота.'],
  'reactionrole remove': ['Удалить привязку', 'Участники сохранят ранее выданные роли.'],
  'reactionrole list': ['Показать привязки', 'Все панели, реакции и связанные с ними роли.'],
  'dashboard setup': ['Настроить дашборд', 'Выберите регион, сервер WoW, гильдию и канал публикации.'],
  'dashboard refresh': ['Обновить дашборд', 'Запросить обновление данных Raider.IO с учётом кэша.'],
  'dashboard status': ['Проверить состояние', 'Настройки дашборда и последняя ошибка обновления.'],
  'dashboard remove': ['Удалить дашборд', 'Остановить обновления и удалить сообщение дашборда.'],
  'mplus setup': ['Настроить канал M+', 'В этом канале будут публиковаться новые группы.'],
  'mplus create': ['Создать группу', 'Вы сразу займёте место в группе по выбранной специализации.'],
  'mplus join': ['Записаться в группу', 'Выберите группу, класс и специализацию.'],
  'mplus show': ['Показать группу', 'Состав, роли, время начала и состояние сбора.'],
  'mplus leave': ['Выйти из группы', 'Если выходит лидер, сбор будет отменён.'],
  'mplus kick': ['Исключить участника', 'Управление доступно лидеру или участнику с правом Manage Server.'],
  'mplus transfer': ['Передать лидерство', 'Новым лидером может стать только участник группы.'],
  'mplus start': ['Начать забег', 'Нужен полный состав: 1 танк, 1 лекарь и 3 DPS. Запись будет закрыта.'],
  'mplus cancel': ['Отменить группу', 'Закрыть сбор и остановить запись участников.']
};
const fieldNames = {role:'Роль игроков',channel:'Канал',cooldown:'Задержка между приглашениями, сек.',reset_time:'Ежедневный сброс',autotag:'Автосбор',message_id:'ID сообщения панели',emoji:'Эмодзи',text:'Текст панели',region:'Регион',realm:'Сервер WoW',guild_name:'Название гильдии',dungeon:'Подземелье',level:'Уровень ключа',start:'Начало сбора',mode:'Цель группы',group:'Группа',member:'Участник'};
const destructive = {clear:'Голоса будут сброшены, текущие сборы закрыты.',cleanup:'Будут удалены сообщения бота за последние 24 часа в выбранном канале, включая панели ролей.', 'dashboard remove':'Дашборд будет отключён, а его сообщение удалено.', 'mplus cancel':'Сбор будет отменён. Запись в группу закроется.', 'mplus kick':'Участник будет исключён из группы.', 'reactionrole remove':'Привязка реакции будет удалена. Уже выданные роли останутся у участников.', 'mplus start':'Запись будет закрыта, а группа отмечена как начавшая забег.', 'mplus transfer':'Лидерство перейдёт выбранному участнику.', 'mplus leave':'Вы выйдете из группы. Если вы лидер, сбор будет отменён.'};
let identity, guildData, section = 'overview', loading = false, executing = false, playerCounter = 0;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}
function ui(tag, className, text) { return node(tag, className, t(text)); }
let noticeSource = '';
function feedback(el, text, error = false) {
  el.dataset.source = text;
  el.className = error ? 'form-feedback error' : 'form-feedback';
  el.textContent = translateError(text);
}
function notice(text, error = false) {
  noticeSource = text;
  $('notice').textContent = translateError(text);
  $('notice').className = error ? 'notice error' : 'notice';
  $('notice').hidden = !text;
}
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options, headers: {'Accept': 'application/json', ...(options.body ? {'Content-Type':'application/json', 'X-CSRF-Token':identity?.csrf || ''} : {}), ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ. Попробуйте позже.'); }
  if (!response.ok) {
    if (response.status === 401) {
      $('welcome').hidden = false;
      $('dashboard').hidden = true;
      $('navigation').hidden = true;
      $('server-picker').hidden = true;
    }
    throw new Error(response.status === 401 ? 'Войдите через Discord, чтобы продолжить.' : (data.error || 'Не удалось выполнить запрос.'));
  }
  return data;
}
function groupMode(value) { return t({push:'Пуш',relaxed:'Без спешки',carry:'Помощь'}[value] || value); }
function groupStatus(value) { return t({open:'Открыта',started:'Начата',cancelled:'Отменена',expired:'Истекла'}[value] || value); }
function commandSection(name) {
  if (name === 'cleanup') return 'tools';
  const prefix = name.split(' ')[0];
  return sections[prefix] ? prefix : 'deadlock';
}
function option(value, text) {
  const result = node('option', '', text);
  result.value = String(value);
  return result;
}
function channelOptions(select, selected) {
  select.replaceChildren(option('', t('Выберите канал')));
  for (const channel of guildData.channels || []) select.append(option(channel.id, '#' + channel.name));
  if (selected) select.value = String(selected);
}
function renderNavigation() {
  const nav = $('navigation');
  nav.replaceChildren();
  for (const [key, details] of Object.entries(sections)) {
    const button = node('button', 'nav-button' + (key === section ? ' active' : ''));
    button.type = 'button';
    button.disabled = executing;
    button.setAttribute('aria-current', key === section ? 'page' : 'false');
    button.append(node('span', 'nav-icon', details.icon), ui('span', '', details.name));
    if (key !== 'overview') button.append(node('span', 'nav-count', (guildData.commands || []).filter((c) => commandSection(c.name) === key).length));
    button.addEventListener('click', () => {section = key; renderPage();});
    nav.append(button);
  }
  nav.hidden = false;
}
function renderSummary() {
  const state = guildData.state || {};
  const groups = Object.values(state.mplus_groups || {}).filter((g) => g.status === 'open');
  const labels = [
    ['Сервер', guildData.guild.name, 'Discord'],
    ['Сборы Deadlock', state.tags ?? '—', t(state.role_id ? 'Роль игроков настроена' : 'Роль ещё не выбрана')],
    ['Группы Mythic+', groups.length, t('Открыто для записи')],
    ['WoW-дашборд', t(state.dashboard ? 'Подключён' : 'Не настроен'), state.dashboard?.name || t('Данные Raider.IO')]
  ];
  $('summary').replaceChildren(...labels.map(([label,value,detail]) => {
    const card = node('div', 'summary-card');
    card.append(ui('span','summary-label',label),node('strong','summary-value',value),node('span','summary-note',detail));
    return card;
  }));
}
function displayChoice(items, id, prefix = '') {
  const item = (items || []).find((entry) => String(entry.id) === String(id));
  return item ? prefix + item.name : id ? String(id) : t('Не настроено');
}
function overviewCard(title, entries) {
  const card = node('section','overview-card');
  card.append(ui('h2','',title));
  const list = node('dl');
  for (const [key,value] of entries) {
    const row = node('div','key-value');
    row.append(ui('dt','',key),node('dd','',value));
    list.append(row);
  }
  card.append(list);
  return card;
}
function renderOverview() {
  const state = guildData.state || {};
  const wrapper = node('div','overview-grid');
  wrapper.append(overviewCard('Сборы Deadlock', [
    ['Роль игроков', displayChoice(guildData.roles,state.role_id,'@')],
    ['Канал', displayChoice(guildData.channels,state.channel_id,'#')],
    ['Автосбор', state.autotag || t('Выключен')], ['Сброс голосов', state.reset_time || '—'],
    ['Задержка', `${state.cooldown ?? '—'} ${t('сек.')}`]
  ]));
  const groupsCard = node('section','overview-card');
  groupsCard.append(ui('h2','','Открытые группы Mythic+'));
  const groups = Object.entries(state.mplus_groups || {}).filter(([,g]) => g.status === 'open');
  if (!groups.length) groupsCard.append(ui('p','empty-state','Пока нет открытых групп. Создайте первую в разделе Mythic+.'));
  for (const [id,group] of groups) {
    const row = node('div','group-row'), detail = node('div');
    const date = new Date(group.start * 1000);
    detail.append(node('strong','',`#${id} · ${group.dungeon} +${group.level}`), node('small','',`${Number.isNaN(date.getTime()) ? t('Время не указано') : date.toLocaleString(dateLocale())} · ${groupMode(group.mode)}`));
    row.append(detail,node('span','badge',`${Object.keys(group.players || {}).length}/5`));
    groupsCard.append(row);
  }
  wrapper.append(groupsCard);
  const dashboard = state.dashboard;
  const wowCard = overviewCard('WoW-гильдия', [['Гильдия',dashboard?.name || t('Не настроена')],['Сервер',dashboard ? `${String(dashboard.region).toUpperCase()} / ${dashboard.realm}` : '—'],['Последняя ошибка',dashboard?.error || t('Нет')]]);
  wrapper.append(wowCard);
  const roleCount = Object.values(state.reaction_roles || {}).reduce((count, panel) => count + Object.keys(panel.roles || {}).length, 0);
  wrapper.append(overviewCard('Роли по реакциям', [['Панелей',Object.keys(state.reaction_roles || {}).length],['Привязок',roleCount],['Как работает',t('Реакция выдаёт роль, снятие забирает')]]));
  $('overview').replaceChildren(wrapper);
}
function buildField(parameter, command, index) {
  const label = node('label','field');
  const caption = ui('span','field-label',fieldNames[parameter.name] || parameter.name);
  if (!parameter.required) caption.append(ui('span','optional',' · необязательно'));
  label.append(caption);
  let input;
  const listTypes = {channel: guildData.channels, role:guildData.roles, user:guildData.members};
  if (parameter.choices?.length || Object.hasOwn(listTypes, parameter.type) || parameter.type === 'boolean' || parameter.name === 'group') {
    input = node('select');
    input.append(option('',t(parameter.required ? 'Выберите…' : 'Не менять / не указывать')));
    if (parameter.choices?.length) for (const choice of parameter.choices) input.append(option(choice.value, parameter.name === 'mode' ? groupMode(choice.value) : choice.name));
    else if (parameter.type === 'boolean') input.append(option('true',t('Да')),option('false',t('Нет')));
    else if (parameter.name === 'group') for (const [id,group] of Object.entries(guildData.state?.mplus_groups || {})) {
      if (command.name !== 'mplus show' && group.status !== 'open') continue;
      input.append(option(id,`#${id} · +${group.level} ${group.dungeon} · ${groupStatus(group.status)}`));
    }
    else for (const item of listTypes[parameter.type] || []) input.append(option(item.id,(parameter.type === 'channel' ? '#' : parameter.type === 'role' ? '@' : '') + item.name));
    if (!input.options.length || input.options.length === 1) input.setAttribute('data-empty-options','true');
  } else if (parameter.name === 'text') {
    input = node('textarea');
    input.rows = 5;
    input.placeholder = t('🎮 Выберите свои игры\n🔫 — Deadlock\n🐉 — WoW Retail');
  } else {
    input = node('input');
    input.type = ['integer','number'].includes(parameter.type) ? 'number' : 'text';
    if (parameter.type === 'integer') input.step = '1';
    if (parameter.type === 'number') input.step = 'any';
    if (['message_id','member'].includes(parameter.name)) input.inputMode = 'numeric';
    input.autocomplete = 'off';
  }
  input.id = 'field-' + command.name.replaceAll(' ','-') + '-' + index;
  input.name = parameter.name;
  input.required = Boolean(parameter.required);
  if (parameter.min_value != null) {
    if (input.type === 'number') input.min = parameter.min_value;
    else if ('minLength' in input) input.minLength = parameter.min_value;
  }
  if (parameter.max_value != null) {
    if (input.type === 'number') input.max = parameter.max_value;
    else if ('maxLength' in input) input.maxLength = parameter.max_value;
  }
  const help = {start:'Дата, время и часовой пояс: 2026-11-20T20:00+02:00. В ближайшие 30 дней.',autotag:'HH:MM или disable, чтобы выключить.',reset_time:'Формат HH:MM, например 03:00.',message_id:'ID сообщения бота из ответа на создание панели.',emoji:'Обычный эмодзи или серверный в формате <:name:ID>.',text:'Переносы строк поддерживаются. Сообщение не пингует участников и роли.'};
  if (parameter.name === 'reset_time') input.placeholder = '03:00';
  if (parameter.name === 'autotag') input.placeholder = t('20:00 или disable');
  if (parameter.name === 'realm') input.placeholder = 'silvermoon';
  if (parameter.name === 'level') input.placeholder = '10';
  label.append(input);
  if (help[parameter.name]) {
    const hint = ui('span','field-help',help[parameter.name]);
    hint.id = input.id + '-help'; input.setAttribute('aria-describedby',hint.id); label.append(hint);
  }
  return {label,input,parameter};
}
function playerFields() {
  const wrapper = node('div','field-row');
  const classLabel = node('label','field'), specLabel = node('label','field');
  const classSelect = node('select'), specSelect = node('select');
  classSelect.required = specSelect.required = true;
  classSelect.id = 'player-class-' + (++playerCounter);
  specSelect.id = 'player-spec-' + playerCounter;
  classSelect.append(option('',t('Выберите класс')));
  specSelect.append(option('',t('Сначала выберите класс')));
  for (const name of Object.keys(guildData.classes || {})) classSelect.append(option(name,name));
  classLabel.append(ui('span','field-label','Класс'),classSelect);
  specLabel.append(ui('span','field-label','Специализация'),specSelect);
  classSelect.addEventListener('change',() => {
    specSelect.replaceChildren(option('',t('Выберите специализацию')));
    for (const [spec,role] of Object.entries(guildData.classes?.[classSelect.value] || {})) specSelect.append(option(spec,`${spec} · ${t({tank:'танк',healer:'лекарь',dps:'DPS'}[role] || role)}`));
  });
  wrapper.append(classLabel,specLabel);
  return {wrapper,classSelect,specSelect};
}
function confirmAction(command) {
  if (!destructive[command.name]) return Promise.resolve(true);
  $('confirm-title').textContent = t(commandsText[command.name]?.[0] || command.name) + '?';
  $('confirm-text').textContent = t(destructive[command.name]);
  $('confirm-dialog').returnValue = 'cancel';
  $('confirm-dialog').showModal();
  return new Promise((resolve) => $('confirm-dialog').addEventListener('close', () => resolve($('confirm-dialog').returnValue === 'confirm'),{once:true}));
}
function renderCommand(command) {
  const card = node('article','command-card'), heading = node('div','command-header');
  heading.append(ui('h2','',commandsText[command.name]?.[0] || command.name),node('code','command-name','/' + command.name));
  card.append(heading,ui('p','command-description',commandsText[command.name]?.[1] || command.description));
  const form = node('form','command-form');
  form.dataset.command = command.name;
  const fields = (command.parameters || []).map((p,i) => buildField(p,command,i));
  for (const {label} of fields) form.append(label);
  let player;
  if (['mplus create','mplus join'].includes(command.name)) { player = playerFields(); form.append(player.wrapper); }
  const footer = node('div','form-footer'), status = node('p','form-feedback'), button = ui('button','button button-small' + (destructive[command.name] ? ' button-secondary' : ''),'Выполнить ↗');
  button.type = 'submit';
  footer.append(status,button);
  form.append(footer);
  form.addEventListener('submit',async (event) => {
    event.preventDefault();
    if (loading || executing || !guildData) return;
    const guildId = guildData.guild.id;
    if (command.name === 'cleanup' && !$('context-channel')?.value) {
      feedback(status, 'Выберите канал выполнения перед очисткой.', true); return;
    }
    if (!(await confirmAction(command)) || !guildData || guildData.guild.id !== guildId) return;
    const args = {};
    for (const {input,parameter} of fields) {
      if (input.value === '') continue;
      args[parameter.name] = parameter.type === 'integer' || parameter.type === 'number' ? Number(input.value) : parameter.type === 'boolean' ? input.value === 'true' : input.value;
    }
    const payload = {command:command.name,channel_id:$('context-channel')?.value || null,arguments:args};
    if (player) payload.player = {class_name:player.classSelect.value,spec:player.specSelect.value};
    executing = true; $('guild-select').disabled = true; $('refresh').disabled = true;
    for (const control of $('navigation').querySelectorAll('button')) control.disabled = true;
    $('language-select').disabled = true;
    button.disabled = true; feedback(status, 'Выполняется…');
    try {
      const result = await api('/api/guilds/' + encodeURIComponent(guildId) + '/commands',{method:'POST',body:JSON.stringify(payload)});
      feedback(status, 'Готово');
      renderResults(command,result);
      await loadGuild(guildId, true);
    } catch (error) { feedback(status, error.message, true); }
    finally {
      executing = false; $('language-select').disabled = false; button.disabled = false; $('guild-select').disabled = false; $('refresh').disabled = false;
      for (const control of $('navigation').querySelectorAll('button')) control.disabled = false;
    }
  });
  card.append(form);
  return card;
}
function renderContext() {
  const toolbar = node('div','context-toolbar');
  const label = node('label','field');
  label.append(ui('span','field-label','Канал выполнения'));
  const select = node('select'); select.id = 'context-channel';
  channelOptions(select,guildData.state?.channel_id || guildData.channels?.[0]?.id);
  label.append(select);
  toolbar.append(label,ui('p','field-help','Контекст для проверки прав и очистки сообщений. Канал публикации задаётся отдельно в командах настройки.'));
  return toolbar;
}
function renderPage() {
  if (!guildData) return;
  playerCounter = 0;
  renderNavigation();
  $('page-eyebrow').textContent = guildData.guild.name.toUpperCase();
  $('page-title').textContent = t(sections[section].title);
  $('page-description').textContent = t(sections[section].description);
  $('dashboard').hidden = false; $('welcome').hidden = true;
  renderSummary();
  $('overview').hidden = section !== 'overview';
  $('command-area').hidden = section === 'overview';
  if (section === 'overview') renderOverview();
  else {
    const commands = (guildData.commands || []).filter((command) => commandSection(command.name) === section);
    const fragment = document.createDocumentFragment();
    const context = renderContext(); context.classList.add('full-width'); fragment.append(context);
    if (!commands.length) fragment.append(ui('p','empty-state','В этом разделе нет доступных вам команд. Права определяются ролью на Discord-сервере.'));
    for (const command of commands) fragment.append(renderCommand(command));
    $('command-area').replaceChildren(fragment);
  }
}
function addEmbed(container, embed) {
  const box = node('div','embed-result');
  if (embed.title) box.append(node('h3','',embed.title));
  if (embed.description) box.append(node('p','',embed.description));
  for (const field of embed.fields || []) {box.append(node('h4','',field.name),node('p','',field.value));}
  if (embed.footer?.text) box.append(node('p','field-help',embed.footer.text));
  container.append(box);
}
function renderResults(command,result) {
  const item = node('div','result-item');
  item.append(node('div','result-command','/' + command.name + ' · ' + new Date().toLocaleTimeString(dateLocale())));
  const messages = result.messages || [];
  if (!messages.length) item.append(ui('p','result-content','Действие выполнено.'));
  for (const message of messages) {
    if (message.content) item.append(node('p','result-content',message.content));
    for (const embed of message.embeds || []) addEmbed(item,embed);
  }
  $('results').prepend(item);
  while ($('results').children.length > 10) $('results').lastElementChild.remove();
  $('results-section').hidden = false;
}
async function loadGuild(id, preservePage = false) {
  if (loading) return;
  loading = true;
  $('refresh').disabled = true;
  $('guild-select').disabled = true;
  try {
    guildData = await api('/api/guilds/' + encodeURIComponent(id));
    notice('');
    renderPage();
    if (!preservePage) {$('results').replaceChildren(); $('results-section').hidden = true;}
  } catch (error) {notice(error.message,true);}
  finally {loading = false; $('refresh').disabled = executing; $('guild-select').disabled = executing;}
}
async function start() {
  try {
    identity = await api('/api/me');
    $('account').replaceChildren();
    if (identity.user.avatar_url) {
      const avatar = node('img','account-avatar');
      if (String(identity.user.avatar_url).startsWith('https://cdn.discordapp.com/')) {avatar.src = identity.user.avatar_url; avatar.alt = ''; $('account').append(avatar);}
    }
    $('account').append(node('span','',identity.user.name));
    const logout = ui('button','text-button','Выйти'); logout.type='button'; logout.dataset.i18n='Выйти';
    logout.addEventListener('click',async () => {try {await api('/auth/logout',{method:'POST',body:'{}'});window.location.assign('/');} catch(error) {notice(error.message,true);}});
    $('account').append(logout);
    const select = $('guild-select');
    for (const guild of identity.guilds || []) select.append(option(guild.id,guild.name));
    if (!identity.guilds?.length) {
      $('welcome').hidden = true;
      notice('Нет доступных серверов. Войдите аккаунтом Discord, который состоит на сервере с ботом.');
      return;
    }
    $('server-picker').hidden = false;
    select.addEventListener('change', () => {guildData = null; $('dashboard').hidden = true; loadGuild(select.value);});
    await loadGuild(select.value);
  } catch (error) {
    if (error.message !== 'Войдите через Discord, чтобы продолжить.') notice(error.message,true);
  }
}
function changeLanguage(value) {
  const feedbacks = [...document.querySelectorAll('.command-form')].map((form) => ({
    command: form.dataset.command, source: form.querySelector('.form-feedback')?.dataset.source,
    error: form.querySelector('.form-feedback')?.classList.contains('error')
  }));
  const drafts = [...document.querySelectorAll('#command-area input, #command-area select, #command-area textarea')]
    .map((input) => ({id: input.id, value: input.value}));
  setLanguage(value);
  applyStatic();
  if (guildData) {
    renderPage();
    for (const {id,value: draft} of drafts) {
      const input = $(id);
      if (!input) continue;
      input.value = draft;
      if (id.startsWith('player-class-')) input.dispatchEvent(new Event('change'));
    }
  }
  for (const form of document.querySelectorAll('.command-form')) {
    const saved = feedbacks.find((entry) => entry.command === form.dataset.command);
    if (saved?.source) feedback(form.querySelector('.form-feedback'), saved.source, saved.error);
  }
  if (noticeSource) $('notice').textContent = translateError(noticeSource);
}
$('language-select').value = getLanguage();
$('language-select').addEventListener('change', (event) => {if (!executing) changeLanguage(event.target.value);});
applyStatic();
$('refresh').addEventListener('click',() => guildData && loadGuild(guildData.guild.id,true));
$('clear-results').addEventListener('click',() => {$('results').replaceChildren(); $('results-section').hidden = true;});
start();
