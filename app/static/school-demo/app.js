"use strict";

const roles = {
  student: {title: "Кабинет ученика", actions: [["Мои задания", "/learning"], ["Оценки и явка", "/journal"], ["Мои кружки", "/clubs"], ["Консультации", "/consultations"], ["После пропусков", "/recovery"]]},
  teacher: {title: "Кабинет преподавателя", actions: [["Мои материалы", "/materials"], ["Работы учеников", "/learning"], ["Журнал класса", "/journal"], ["Консультации", "/consultations"], ["Пожелания", "/preferences"]]},
  planner: {title: "Кабинет завуча", actions: [["Замены уроков", "/replacements"], ["Редактор расписания", "/schedule menu"], ["Календарь", "/calendar"], ["Обращения", "/requests"], ["Публикации", "/publish"], ["Материалы замен", "/materials"], ["Отработки", "/makeups"], ["Отчёты", "/analytics"]]},
  director: {title: "Кабинет директора", actions: [["Управление", "/section management"], ["Учебная работа", "/section study"], ["Работа школы", "/section school"], ["Замены уроков", "/replacements"], ["Публикации", "/publish"], ["Оценки и явка", "/journal"]]},
};
const linkedActions = {
  student: {learning: "/learning"},
  teacher: {materials: "/materials"},
  planner: {replacements: "/replacements"},
  director: {management: "/section management"},
};
const fileLimit = 2 * 1024 * 1024;
const days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];
const assignmentStatus = {
  sent: "Назначено", submitted: "Сдано", draft: "Черновик",
  reviewed: "Проверено", returned: "На доработке", revision_requested: "На доработке",
};
const roleSelect = document.querySelector("#role");
const profileName = document.querySelector("#profile");
const workspace = document.querySelector("#workspace");
const sections = document.querySelector("#sections");
const title = document.querySelector("#title");
const intro = document.querySelector("#intro");
const status = document.querySelector("#status");
const theme = document.querySelector("#theme");
const query = new URLSearchParams(location.search);
let role = Object.hasOwn(roles, query.get("role")) ? query.get("role") : "student";
let view = query.get("view") === "schedule" ? "schedule" : "overview";
let school = null;
let reply = null;
let busy = false;
let draft = false;
let refreshNeeded = false;

function element(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text != null) result.textContent = String(text);
  return result;
}

function button(label, action, primary = false) {
  const result = element("button", primary ? "primary" : "", label);
  result.type = "button";
  result.disabled = busy;
  result.addEventListener("click", action);
  return result;
}

function panel(heading) {
  const result = element("section", "panel card");
  result.append(element("h2", "", heading));
  return result;
}

function labelledActions(entries) {
  const result = element("div", "actions");
  for (const [label, command] of entries) {
    result.append(button(label, () => run(command)));
  }
  return result;
}

function scheduleCard() {
  const result = panel(draft ? "Черновик расписания" : "Расписание");
  const schedule = draft ? school.sections?.draftTimetable : school.timetable;
  const lessons = Array.isArray(schedule?.lessons) ? schedule.lessons : [];
  if (schedule?.periodStart) result.append(element("p", "muted", "Неделя с " + displayDate(schedule.periodStart)));
  if (school.sections?.draftTimetable) {
    const choices = element("div", "actions");
    choices.append(button(draft ? "Показать публикацию" : "Показать черновик", () => {
      draft = !draft;
      render();
    }));
    choices.append(button("Редактор расписания", () => run("/schedule menu")));
    result.append(choices);
  }
  if (!lessons.length) result.append(element("p", "muted", "В этой сетке пока нет занятий."));
  for (const [dayIndex, name] of days.entries()) {
    const rows = lessons.filter(lesson => lesson.day === dayIndex).sort((a, b) => a.slot - b.slot);
    if (!rows.length) continue;
    const group = element("section", "day");
    group.append(element("h3", "", name));
    for (const lesson of rows) {
      const row = element("div", "lesson");
      row.append(element("strong", "", lesson.subject || "Занятие"));
      const time = [lesson.start, lesson.end].filter(Boolean).join("–");
      const details = [time, lesson.className, ...(lesson.teachers || []), ...(lesson.rooms || [])];
      row.append(element("span", "", details.filter(Boolean).join(" · ")));
      group.append(row);
    }
    result.append(group);
  }
  return result;
}

function displayDate(value) {
  const date = new Date(value + "T00:00:00Z");
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru", {day: "numeric", month: "long", year: "numeric", timeZone: "UTC"}).format(date);
}

function overviewCard() {
  const learningRole = role === "student" || role === "teacher";
  const result = panel(learningRole ? "Задания" : "Учебная работа");
  if (learningRole) {
    const items = school.assignments || [];
    if (!items.length) result.append(element("p", "muted", "В этой роли пока нет заданий."));
    for (const item of items.slice(0, 6)) {
      const assignment = item.assignment || item;
      const row = element("div", "assignment");
      row.append(element("strong", "", assignment.title || "Учебное задание"));
      const subtitle = [assignment.subject, assignmentStatus[assignment.status]].filter(Boolean);
      if (subtitle.length) row.append(element("span", "", subtitle.join(" · ")));
      result.append(row);
    }
    result.append(labelledActions(roles[role].actions.slice(0, 3)));
  } else {
    result.append(element("p", "muted", "Откройте замены, календарь или подготовку публикации."));
    result.append(labelledActions(roles[role].actions.slice(0, 4)));
  }
  return result;
}

function uploadForm(text) {
  const details = element("details", "upload");
  details.open = text.startsWith("Прикрепите");
  details.append(element("summary", "", "Прикрепить тестовый файл"));
  const form = element("form");
  const label = element("label", "", "XLSX, CSV, JSON или TXT до 2 МиБ");
  label.htmlFor = "demo-file";
  const input = element("input");
  input.id = "demo-file";
  input.type = "file";
  input.accept = ".xlsx,.csv,.json,.txt";
  input.required = true;
  input.disabled = busy;
  const submit = element("button", "primary", "Загрузить файл");
  submit.disabled = busy;
  form.append(label, input, submit);
  form.addEventListener("submit", event => {
    event.preventDefault();
    const file = input.files[0];
    if (!file || !file.size || file.size > fileLimit) {
      status.textContent = "Выберите тестовый файл размером до 2 МиБ.";
      return;
    }
    const reader = new FileReader();
    const uploadRole = role;
    reader.onerror = () => { status.textContent = "Не удалось прочитать файл."; };
    reader.onload = () => {
      if (role !== uploadRole || !form.isConnected) return;
      execute("/api/demo/upload", {document: {
        name: file.name,
        contentType: file.type || "application/octet-stream",
        base64: reader.result.split(",", 2)[1],
      }});
    };
    reader.readAsDataURL(file);
  });
  details.append(form);
  return details;
}

function actionCard() {
  const result = panel(reply ? "Рабочая область" : "Действия по роли");
  if (!reply) {
    const actions = new Map(roles[role].actions.map(([label, command]) => [command, label]));
    for (const {label, command} of school.menu?.buttons || []) {
      if (command !== "/schedule" && !actions.has(command)) actions.set(command, label);
    }
    result.append(labelledActions(Array.from(actions, ([command, label]) => [label, command])));
    return result;
  }
  result.append(element("p", "response", reply.text));
  const picture = reply.image;
  if (picture && ["image/jpeg", "image/png"].includes(picture.contentType) && picture.base64) {
    const img = element("img", "reply-image");
    img.alt = "Карточка рабочего раздела";
    img.src = "data:" + picture.contentType + ";base64," + picture.base64;
    result.append(img);
  }
  if (reply.document?.base64) {
    const document = reply.document;
    const link = element("a", "download", "Скачать " + document.name);
    link.href = "data:application/octet-stream;base64," + document.base64;
    link.download = document.name;
    result.append(link);
  }
  const options = (reply.buttons || []).filter(item => typeof item.command === "string");
  result.append(labelledActions(options.map(item => [item.label, item.command])));
  const form = element("form");
  const label = element("label", "", "Ответ на шаг");
  label.htmlFor = "answer";
  const input = element("textarea");
  input.id = "answer";
  input.rows = 2;
  input.maxLength = 2000;
  input.required = true;
  input.disabled = busy;
  const submit = element("button", "primary", "Отправить");
  submit.disabled = busy;
  form.append(label, input, submit);
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (input.value.trim()) run(input.value);
  });
  result.append(form, uploadForm(reply.text));
  return result;
}

function render() {
  roleSelect.value = role;
  roleSelect.disabled = busy;
  profileName.textContent = school?.profile?.name || "";
  title.textContent = view === "overview" ? roles[role].title : view === "schedule" ? "Расписание" : "Рабочие разделы";
  intro.textContent = "Все ученики, уроки и документы вымышлены.";
  sections.replaceChildren();
  for (const [key, name] of [["overview", "Обзор"], ["schedule", "Расписание"], ["actions", "Действия"]]) {
    const tab = button(name, () => openView(key));
    if (view === key) tab.setAttribute("aria-current", "page");
    sections.append(tab);
  }
  if (!school) {
    workspace.replaceChildren();
    if (!busy) workspace.append(button("Повторить загрузку", load, true));
    return;
  }
  if (view === "schedule") workspace.replaceChildren(scheduleCard());
  else if (view === "actions") workspace.replaceChildren(actionCard());
  else {
    const grid = element("div", "grid");
    grid.append(scheduleCard(), overviewCard());
    workspace.replaceChildren(grid);
  }
}

function setView(next) {
  view = next;
  const url = new URL(location.href);
  url.searchParams.set("role", role);
  if (next === "overview") url.searchParams.delete("view");
  else url.searchParams.set("view", next);
  history.replaceState(null, "", url);
}

function openView(next) {
  setView(next);
  if (refreshNeeded && next !== "actions") return load();
  render();
}

async function request(path, options) {
  const result = await fetch(path, {...options, credentials: "same-origin", cache: "no-store"});
  const data = await result.json();
  if (!result.ok) throw new Error(data.detail || "Данные сейчас недоступны. Попробуйте ещё раз.");
  return data;
}

async function load() {
  busy = true;
  status.textContent = "Загружаем кабинет…";
  render();
  try {
    school = await request("/api/demo/state?role=" + encodeURIComponent(role));
    refreshNeeded = false;
    status.textContent = "";
  } catch (error) {
    school = null;
    status.textContent = error.message;
  } finally {
    busy = false;
    render();
  }
}

function run(text) {
  return execute("/api/demo/action", {text}, text === "/home" || text === "/start");
}

async function execute(path, payload, goHome = false) {
  if (busy) return;
  view = "actions";
  busy = true;
  refreshNeeded = true;
  status.textContent = "Открываем действие…";
  render();
  try {
    const result = await request(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({role, ...payload, event_id: crypto.randomUUID()}),
    });
    reply = goHome ? null : result.reply;
    if (goHome) setView("overview");
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    busy = false;
    if (view === "overview") await load();
    else render();
  }
}

roleSelect.addEventListener("change", () => {
  if (busy || !Object.hasOwn(roles, roleSelect.value)) return;
  role = roleSelect.value;
  school = null;
  reply = null;
  draft = false;
  setView("overview");
  load();
});

function applyTheme(value) {
  document.documentElement.dataset.theme = value;
  theme.textContent = value === "dark" ? "Светлая тема" : "Тёмная тема";
}

let preferred = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
try {
  const saved = localStorage.getItem("school-demo-theme");
  if (saved === "dark" || saved === "light") preferred = saved;
} catch {}
applyTheme(preferred);
theme.addEventListener("click", () => {
  const value = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(value);
  try { localStorage.setItem("school-demo-theme", value); } catch {}
});

load().then(() => {
  const initialView = query.get("view");
  if (school && Object.hasOwn(linkedActions[role], initialView)) run(linkedActions[role][initialView]);
});
