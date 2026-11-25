import {SensorSerial, verifyIdentity} from "./sensor-usb.mjs";
import {createSerialLog} from "./sensor-log.mjs";
import {runBenchTest, manualChecks} from "./firmware-test.mjs";

const field = name => document.getElementById("debug-" + name);
const panelVersion = field("panel-version").textContent;
const supported = Boolean(window.isSecureContext && navigator.serial);
const log = field("log");
const journal = createSerialLog(log, field("log-end"), 65536);
const diagnostics = ["firmware", "hardware", "sensor-type", "pending", "temperature", "humidity", "battery"];
const clock = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const number = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2});
const measurementErrors = {
  sensor_crc: "Датчик вернул неверную контрольную сумму.",
  sensor_io: "Нет ответа датчика. Проверьте тип модуля и подключение.",
};

let serial = null;
let connecting = false;
let checking = false;
let bootArmed = false;
let testing = false, testCancelled = false, testReport = null;
let auto = false, timer = null, successes = 0, failures = 0;
function stats() { field("stats").textContent = "Успешно: "+successes+" · Ошибок: "+failures; }
function stopAuto() { auto = false; clearTimeout(timer); timer = null; controls(); }
function scheduleAuto() {
  clearTimeout(timer);
  if (auto && serial) timer = setTimeout(() => void checkBoard("test_measurement"), Number(field("interval").value)*1000);
}

function controls() {
  field("connect").disabled = !supported || connecting || Boolean(serial);
  field("disconnect").disabled = !serial;
  const queriesDisabled = !serial || checking || bootArmed || testing;
  for (const name of ["identify", "wifi", "boot"]) field(name).disabled = queriesDisabled || auto;
  field("measure").disabled = queriesDisabled || auto;
  field("auto").disabled = !serial || bootArmed || testing || (checking && !auto);
  field("auto").textContent = auto ? "Остановить автозамер" : "Запустить автозамер";
  field("interval").disabled = auto || testing;
  field("test-start").disabled = queriesDisabled || auto;
  field("test-stop").disabled = !testing || testCancelled;
  field("test-report").disabled = !testReport || testing;
  field("clear").disabled = log.textContent.length === 0;
  field("save").disabled = field("clear").disabled;
}

function appendLog(line) {
  if (!line) return;
  journal.append("[" + clock.format(new Date()) + "] " + line);
  controls();
}

function serialLog(line) {
  const clean = line.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
  appendLog(clean.replace(/^\s*SETUP password:.*$/i, "SETUP password: [скрыт]"));
  if (bootArmed && clean.startsWith("BOOT: ")) field("result").textContent = clean.slice(6);
}

function showIdentity(hello) {
  field("firmware").textContent = hello.firmware || "—";
  field("hardware").textContent = hello.hardware_id;
  field("sensor-type").textContent = (hello.sensor_type || "—").toUpperCase();
  field("pending").textContent = hello.pending_records ? "Есть неотправленные замеры" : "Всё отправлено";
  return hello.firmware === panelVersion ? "Версия платы совпадает с версией панели." :
    "На плате " + field("firmware").textContent + ", в панели " + panelVersion + ".";
}

function format(value, unit) {
  return Number.isFinite(value) ? number.format(value) + " " + unit : "—";
}

function showMeasurement(sample) {
  field("temperature").textContent = format(sample.temperature, "°C");
  field("humidity").textContent = format(sample.humidity, "%");
  const voltage = sample.battery_mv >= 2000 && sample.battery_mv <= 5000 ? sample.battery_mv / 1000 : null;
  field("battery").textContent = format(voltage, "В");
  const attempts = Number.isInteger(sample.attempts) ? " · попыток: "+sample.attempts : "";
  if (sample.sensor_error) return (measurementErrors[sample.sensor_error] || "Датчик сообщил об ошибке измерения.") + attempts;
  return "Пробный замер · " + field("temperature").textContent + " · влажность " +
    field("humidity").textContent + " · аккумулятор " + field("battery").textContent + attempts + ".";
}

function setSetupWifi(reply) {
  const visible = Boolean(reply);
  field("wifi-ssid").textContent = reply?.ssid ?? "";
  field("wifi-password").textContent = reply?.password ?? "";
  field("wifi-details").hidden = !visible;
  field("wifi").textContent = visible ? "Скрыть пароль точки Wi-Fi" : "Показать пароль точки Wi-Fi";
  field("wifi").setAttribute("aria-expanded", String(visible));
}

async function checkBoard(command) {
  const connection = serial;
  if (!connection || checking || bootArmed || testing) { if (auto) scheduleAuto(); return; }
  checking = true;
  controls();
  field("result").textContent = "Ждём ответа платы…";
  if (command === "test_measurement") {
    for (const name of ["temperature", "humidity", "battery"]) field(name).textContent = "—";
  }
  let message;
  try {
    const reply = await connection.request(command);
    if (command === "hello") await verifyIdentity(reply);
    if (serial !== connection) return;
    if (command === "hello") {
      message = showIdentity(reply);
    } else if (command === "setup_wifi") {
      if (!reply.ssid || !reply.password) throw Error("Плата не вернула данные точки Wi-Fi.");
      setSetupWifi(reply);
      message = "Получены данные точки настройки.";
    } else if (command === "arm_boot_debug") {
      const seconds = reply.duration_s;
      if (!Number.isInteger(seconds) || seconds <= 0) throw Error("Плата не подтвердила отладку по BOOT.");
      bootArmed = true;
      setSetupWifi(null);
      message = "Дождитесь приглашения в журнале и коротко нажмите BOOT в течение " + number.format(seconds / 60) + " мин.";
    } else {
      message = showMeasurement(reply);
      if (reply.sensor_error || !Number.isFinite(reply.temperature)) failures++; else successes++;
      stats();
    }
  } catch (error) {
    if (serial !== connection) return;
    message = error.message;
    if (command === "test_measurement") { failures++; stats(); }
  } finally {
    if (serial === connection) {
      checking = false;
      controls();
    }
  }
  field("result").textContent = message;
  appendLog(message);
  if (auto) scheduleAuto();
}

async function closePort(connection, message) {
  if (serial !== connection) return;
  serial = null;
  if(testing)testCancelled=true;
  stopAuto();
  checking = false;
  connecting = true;
  bootArmed = false;
  setSetupWifi(null);
  controls();
  await connection.close();
  connecting = false;
  field("connection").textContent = message;
  appendLog(message);
}

field("connect").addEventListener("click", async () => {
  if (!supported || connecting || serial) return;
  connecting = true;
  controls();
  try {
    const port = await navigator.serial.requestPort({filters: [{usbVendorId: 0x303a}]});
    const connection = new SensorSerial(port, 15000, serialLog);
    await connection.open();
    serial = connection;
    successes = failures = 0; stats();
    for (const name of diagnostics) field(name).textContent = "—";
    field("connection").textContent = "Плата подключена · 115200 бод.";
    appendLog(field("connection").textContent);
    void connection.reading.then(() => closePort(connection, "Связь с платой завершилась. Подключите USB снова."));
    void checkBoard("hello");
  } catch (error) {
    field("connection").textContent = error.name === "NotFoundError" ? "Порт не выбран." :
      "Не удалось открыть порт. Закройте USB-мастер или другой монитор порта.";
  } finally {
    connecting = false;
    controls();
  }
});

field("disconnect").addEventListener("click", () => {
  if (serial) void closePort(serial, "Порт освобождён.");
});
field("identify").addEventListener("click", () => checkBoard("hello"));
field("measure").addEventListener("click", () => checkBoard("test_measurement"));
field("auto").addEventListener("click", () => {
  if (auto) { stopAuto(); return; }
  if (!serial || checking || bootArmed || testing) return;
  auto = true; controls(); void checkBoard("test_measurement");
});
field("boot").addEventListener("click", () => checkBoard("arm_boot_debug"));
const manual = new Map();
for(const [id, description] of manualChecks){
  const label=document.createElement("label");
  const title=document.createElement("span");title.textContent=description;
  const select=document.createElement("select");
  select.setAttribute("aria-label",description);
  for(const [value,text] of [["not_tested","Не проверено"],["pass","Пройдено"],["fail","Ошибка"]]){
    const option=document.createElement("option");option.value=value;option.textContent=text;select.append(option);
  }
  manual.set(id,select);label.append(title,select);field("test-manual").append(label);
}
function renderTest(report){
  testReport=report;
  const result=field("test-results");result.replaceChildren();
  const labels={pass:"Пройдено",fail:"Ошибка",warn:"Требует внимания",skipped:"Не завершено"};
  for(const step of report.steps){
    const row=document.createElement("p");row.className="firmware-test-result";
    const status=document.createElement("strong");status.textContent=labels[step.status]+": ";
    row.append(status,document.createTextNode(step.message));result.append(row);
  }
  for(const sample of report.samples){
    const row=document.createElement("p");row.className="firmware-test-result";
    row.textContent="Замер "+sample.index+": "+sample.reason+" · "+format(sample.temperature,"°C")+" · "+format(sample.humidity,"%")+" · "+format(sample.battery_mv===null?null:sample.battery_mv/1000,"В");
    result.append(row);
  }
  field("test-state").textContent=testing?"Выполняется · замеров "+report.samples.length+" из 5":report.cancelled?"Тест остановлен.":"Автоматическая часть завершена; проверьте результаты и ручные шаги.";
  controls();
}
field("test-start").addEventListener("click",async()=>{
  if(!serial||checking||bootArmed||testing||auto)return;
  const connection=serial;
  testing=true;testCancelled=false;testReport=null;
  for(const select of manual.values())select.value="not_tested";
  field("test-results").replaceChildren();
  field("test-state").textContent="Проверяем подключение…";controls();
  try{
    testReport=await runBenchTest(connection,panelVersion,{cancelled:()=>testCancelled||serial!==connection,onUpdate:renderTest});
  }finally{
    testing=false;
    if(testReport)renderTest(testReport);
    controls();
  }
});
field("test-stop").addEventListener("click",()=>{testCancelled=true;field("test-state").textContent="Останавливаем после текущего запроса…";controls();});
field("test-report").addEventListener("click",()=>{
  if(!testReport||testing)return;
  const report={...testReport,exported_at:new Date().toISOString(),manual:manualChecks.map(([id,description])=>({id,description,status:manual.get(id).value}))};
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)+"\n"],{type:"application/json"}));
  const link=document.createElement("a");link.href=url;link.download="termokontur-acceptance.json";link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
field("wifi").addEventListener("click", () => {
  if (field("wifi-details").hidden) void checkBoard("setup_wifi");
  else setSetupWifi(null);
});
field("clear").addEventListener("click", () => {
  journal.clear();
  controls();
});
field("save").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([log.textContent], {type: "text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "termokontur-usb.log.txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener("pagehide", () => {
  if (serial) void closePort(serial, "Порт освобождён.");
});

if (!supported) field("connection").textContent = "Откройте панель по HTTPS в Chrome или Edge на компьютере.";
controls();
