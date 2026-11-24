const calendarWeekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
let schoolCalendar = null;

function calendarField(label, input) {
  const field = el("label", "", label);
  field.append(input);
  return field;
}

function addCalendarPeriod(rule) {
  const row = el("div", "field-grid");
  const building = el("select");
  option(building, "", "Все корпуса");
  for (const name of schoolCalendar.buildings) option(building, name, name);
  building.value = rule.building;
  const first = el("input"), last = el("input");
  for (const [input, value] of [[first, rule.start], [last, rule.end]]) {
    input.type = "date";
    input.required = true;
    input.value = value;
  }
  const expected = el("select");
  option(expected, "false", "Замеры не требуются");
  option(expected, "true", "Ждём замеры");
  expected.value = String(rule.expected);
  const remove = el("button", "btn secondary", "Удалить период");
  remove.type = "button";
  remove.addEventListener("click", () => {
    row.remove();
    markSettingsDirty($("calendar-form"));
  });
  row.append(calendarField("Корпус", building), calendarField("С даты", first),
    calendarField("По дату включительно", last), calendarField("Режим", expected), remove);
  $("calendar-exceptions").append(row);
}

function renderSchoolCalendar() {
  const {weekly, exceptions, default_weekdays: defaultDays, buildings} = schoolCalendar;
  $("calendar-default").textContent = "По умолчанию: " + defaultDays.map(day => calendarWeekdays[day]).join(", ") +
    ". Общие дни можно изменить в настройках уведомлений.";
  const list = $("calendar-weekly");
  list.replaceChildren();
  for (const building of buildings) {
    const row = el("fieldset", "weekdays");
    row.dataset.building = building;
    row.append(el("legend", "", building));
    const override = el("input");
    override.type = "checkbox";
    override.checked = Object.hasOwn(weekly, building);
    row.append(calendarField("Свой график", override));
    const days = calendarWeekdays.map((name, index) => {
      const input = el("input");
      input.type = "checkbox";
      input.checked = (override.checked ? weekly[building] : defaultDays).includes(index);
      row.append(calendarField(name, input));
      return input;
    });
    const sync = () => days.forEach(input => { input.disabled = !override.checked; });
    override.addEventListener("change", sync);
    sync();
    list.append(row);
  }
  $("calendar-exceptions").replaceChildren();
  for (const rule of exceptions) addCalendarPeriod(rule);
}

async function loadSchoolCalendar() {
  if ($("calendar-form").dataset.dirty === "true") return;
  try {
    schoolCalendar = await api("/api/school-calendar");
    renderSchoolCalendar();
  } catch (error) { toast(error.message, true); }
}

$("calendar-add").addEventListener("click", () => {
  if (!schoolCalendar) return;
  addCalendarPeriod({building:"", start:"", end:"", expected:false});
  markSettingsDirty($("calendar-form"));
});

onOperation($("calendar-form"),"Сохраняем календарь замеров…", async event => {
  event.preventDefault();
  if (!schoolCalendar) return;
  const form = event.currentTarget;
  const weekly = Object.fromEntries([...$("calendar-weekly").children].flatMap(row => {
    const [override, ...days] = row.querySelectorAll("input");
    return override.checked ? [[row.dataset.building, days.flatMap((day, index) => day.checked ? [index] : [])]] : [];
  }));
  const exceptions = [...$("calendar-exceptions").children].map(row => {
    const [building, first, last, expected] = row.querySelectorAll("select,input");
    return {building:building.value, start:first.value, end:last.value, expected:expected.value === "true"};
  });
  try {
    schoolCalendar = await api("/api/school-calendar", "PUT", {weekly, exceptions});
    renderSchoolCalendar();
    markSettingsSaved(form, "Календарь сохранён");
    await refresh();
  } catch (error) { toast(error.message, true); }
});
