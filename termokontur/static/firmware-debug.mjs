import {SensorSerial, verifyIdentity} from "./sensor-usb.mjs";

const field = name => document.getElementById("debug-" + name);
const panelVersion = field("panel-version").textContent;
const supported = Boolean(window.isSecureContext && navigator.serial);
const log = field("log");
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

function controls() {
  field("connect").disabled = !supported || connecting || Boolean(serial);
  field("disconnect").disabled = !serial;
  field("identify").disabled = !serial || checking;
  field("measure").disabled = !serial || checking;
  field("clear").disabled = log.textContent.length === 0;
  field("save").disabled = field("clear").disabled;
}

function appendLog(line) {
  if (!line) return;
  const follow = log.scrollHeight - log.clientHeight - log.scrollTop < 20;
  const entry = "[" + clock.format(new Date()) + "] " + line + "\n";
  log.textContent = (log.textContent + entry).slice(-65536);
  if (follow) log.scrollTop = log.scrollHeight;
  controls();
}

function serialLog(line) {
  const clean = line.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
  appendLog(clean.replace(/^\s*SETUP password:.*$/i, "SETUP password: [скрыт]"));
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
  if (sample.sensor_error) return measurementErrors[sample.sensor_error] || "Датчик сообщил об ошибке измерения.";
  return "Пробный замер · " + field("temperature").textContent + " · влажность " +
    field("humidity").textContent + " · аккумулятор " + field("battery").textContent + ".";
}

async function checkBoard(command) {
  const connection = serial;
  if (!connection || checking) return;
  checking = true;
  controls();
  field("result").textContent = "Ждём ответа платы…";
  if (command === "test_measurement") {
    for (const name of ["temperature", "humidity", "battery"]) field(name).textContent = "—";
  }
  let message;
  try {
    const reply = await connection.request(command);
    if (command === "hello") {
      await verifyIdentity(reply);
      if (serial !== connection) return;
      message = showIdentity(reply);
    } else {
      if (serial !== connection) return;
      message = showMeasurement(reply);
    }
  } catch (error) {
    if (serial !== connection) return;
    message = error.message;
  } finally {
    if (serial === connection) {
      checking = false;
      controls();
    }
  }
  field("result").textContent = message;
  appendLog(message);
}

async function closePort(connection, message) {
  if (serial !== connection) return;
  serial = null;
  checking = false;
  connecting = true;
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
    const connection = new SensorSerial(port, 4000, serialLog);
    await connection.open();
    serial = connection;
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
field("clear").addEventListener("click", () => {
  log.textContent = "";
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
