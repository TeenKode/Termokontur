import {verifyIdentity} from "./sensor-usb.mjs";

const trials = 3;

function waitLine(lines, signal, accept, timeout = 0, message = "Плата не ответила.") {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    let timer;
    function finish(callback, value) {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      lines.removeEventListener("line", receive);
      callback(value);
    }
    const abort = () => finish(reject, signal.reason);
    const receive = event => {
      const result = accept(event.detail);
      if (result) finish(resolve, result);
    };
    signal.addEventListener("abort", abort, {once:true});
    lines.addEventListener("line", receive);
    if (timeout) timer = setTimeout(() => finish(reject, Error(message)), timeout);
  });
}

function holdOnWake(connection, lines, signal, timeout) {
  const held = waitLine(lines, signal, line => line.startsWith("TK READY "), timeout,
    "Не дождались повторного пробуждения платы.").then(() => {
    signal.throwIfAborted();
    return connection.request("hold", {seconds:15});
  });
  // The next wake can arrive while the last record is being checked on the server.
  void held.catch(() => {});
  return held;
}

function watchDelivery(lines, signal) {
  let stage = "";
  return waitLine(lines, signal, line => {
    const network = /^TK NET stage=(dns|tcp|tls) result=(\w+)/.exec(line);
    if (network) stage = network[2] === "ok" ? "" : network[1].toUpperCase();
    if (/^DNS: lookup .* failed:/.test(line)) stage = "DNS";
    if (/^HTTPS: \S+ (?:HTTP \d+$|failed at (?:request_body|response_headers|response_body)\b)/.test(line)) stage = "HTTP";
    if (line.startsWith("TK SYNC result=")) {
      const ack = Number(/\back_seq=(\d+)/.exec(line)?.[1]);
      const error = /\berror=([\w-]+)/.exec(line)?.[1];
      return {ack, stage:stage || (error === "wifi_unavailable" ? "Wi-Fi" : ""), error};
    }
    if (line.startsWith("TK SLEEP ")) return {ack:null, stage, error:"sleep"};
    return null;
  }, 645000, "Истекло время ожидания результата отправки.");
}

export async function runDeliveryTest(connection, lines, {signal, listDevices, readRecord, onState, onTrial}) {
  const devices = await listDevices(signal);
  signal.throwIfAborted();
  onState("Ждём пробуждения тестовой платы. Для быстрого старта можно нажать RESET.");
  let held = holdOnWake(connection, lines, signal, 0);
  let deviceId;
  let ack = 0;
  const attempts = [];
  for (let index = 1; index <= trials; index++) {
    await held;
    signal.throwIfAborted();
    const hello = await connection.request("hello");
    await verifyIdentity(hello);
    signal.throwIfAborted();
    if (!hello.delivery_test) throw Error("Обновите прошивку платы до 1.5.0 или новее для проверки доставки.");
    const device = devices.find(item => item.enabled && item.config?.enabled && item.hardware_id === hello.hardware_id &&
      (!item.identity_fingerprint || item.identity_fingerprint === hello.fingerprint) &&
      !["retired", "rejected", "authorization_pending"].includes(item.status));
    if (!device || (deviceId && device.id !== deviceId)) throw Error("Плата должна быть привязана к активному датчику в этой панели.");
    deviceId = device.id;
    onState(`${device.name} · ${device.building} / ${device.room_name} · отправка ${index} из ${trials}`);

    const trial = new AbortController();
    const observed = watchDelivery(lines, AbortSignal.any([signal, trial.signal]));
    void observed.catch(() => {});
    let reply, result;
    try {
      reply = await connection.request("delivery_test", {repeat:index < trials});
      if (!Number.isSafeInteger(reply.seq + 1) || reply.seq < 1 || !/^[a-f0-9]{16}$/.test(reply.boot_id)) {
        throw Error("Плата не вернула номер и идентификатор замера.");
      }
      result = await observed;
    } finally {
      trial.abort();
    }

    if (index < trials) held = holdOnWake(connection, lines, signal, 60000);
    const record = await readRecord(device.id, reply.seq, signal);
    signal.throwIfAborted();
    const stored = record?.seq === reply.seq && record.boot_id === reply.boot_id;
    if (Number.isSafeInteger(result.ack)) ack = Math.max(ack, result.ack);
    const attempt = {index, seq:reply.seq, bootId:reply.boot_id, stored, acknowledged:result.ack > reply.seq,
      stage:result.stage, error:result.error, measurementError:stored ? record.error : "", recovered:false};
    attempts.push(attempt);
    onTrial(attempt);
  }
  for (const attempt of attempts) {
    if (attempt.stored && attempt.acknowledged || ack <= attempt.seq) continue;
    const record = await readRecord(deviceId, attempt.seq, signal);
    signal.throwIfAborted();
    if (record?.seq === attempt.seq && record.boot_id === attempt.bootId) {
      attempt.recovered = true;
      attempt.measurementError = record.error;
      onTrial(attempt);
    }
  }
  return attempts;
}
