// Read-only bench test: no configuration, no journal writes, no credentials.
import {verifyIdentity} from "./sensor-usb.mjs";
export const manualChecks = [
  ["reference", "Сравнить температуру и влажность с контрольным термометром после стабилизации."],
  ["server", "После настройки нажать «Отправить замер по BOOT», затем BOOT; убедиться, что новый замер появился у нужного датчика на сервере."],
  ["offline", "На тестовой плате отключить доступ к Wi-Fi на время замеров, затем восстановить сеть и убедиться в доставке накопленных данных без дублей."],
  ["battery", "Отключить USB, запитать от аккумулятора и проверить замеры, сон и повторное пробуждение."],
  ["charging", "Подключить зарядку и убедиться, что обычные замеры и передача приостановлены; после зарядки работа возобновляется."],
  ["ota", "При наличии более новой версии проверить OTA на тестовой плате и сохранение настроек после перезапуска."],
  ["current", "Измерить ток сна и активного режима внешним прибором; наблюдать реальную автономность."]
];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const humidityTypes = new Set(["sht4x","sht3x","aht20","bme280","dht11","dht22"]);
const sensorTypes = new Set([...humidityTypes,"bmp280","ds18b20"]);
export function assessSample(sample, sensorType) {
  if (sample.sensor_error) return {ok:false, reason:"Ошибка датчика: "+(sample.sensor_error==="sensor_crc"?"CRC":sample.sensor_error==="sensor_io"?"нет ответа":"неизвестная")};
  if (!Number.isFinite(sample.temperature) || sample.temperature < -55 || sample.temperature > 125)
    return {ok:false,reason:"Некорректная температура"};
  if (humidityTypes.has(sensorType) && (!Number.isFinite(sample.humidity) || sample.humidity < 0 || sample.humidity > 100))
    return {ok:false,reason:"Некорректная влажность"};
  return {ok:true,reason:"Замер получен"};
}
export async function runBenchTest(connection, panelVersion, {onUpdate=()=>{}, cancelled=()=>false, wait=pause, samples=5}={}) {
  const report={schema:1, started_at:new Date().toISOString(),panel_version:panelVersion,board:null,steps:[],samples:[],cancelled:false};
  function step(id,status,message){report.steps.push({id,status,message});onUpdate(report);}
  const stop=()=>{report.cancelled=true;step("cancelled","skipped","Тест остановлен");return report;};
  if(cancelled())return stop();
  let hello;
  try{
    hello=await connection.request("hello");
    await verifyIdentity(hello);
    if(cancelled())return stop();
    report.board={hardware_id:hello.hardware_id,firmware:String(hello.firmware||""),sensor_type:String(hello.sensor_type||""),provisioned:hello.provisioned===true};
    step("usb","pass","USB отвечает, отпечаток открытого ключа совпадает.");
  }catch{
    if(cancelled())return stop();
    step("usb","fail","Не удалось прочитать или проверить идентификатор платы.");
    return report;
  }
  step("version",hello.firmware===panelVersion?"pass":"warn",hello.firmware===panelVersion?"Версия платы совпадает с панелью.":"Версии платы и панели различаются; обновите плату перед приёмкой.");
  step("setup",hello.provisioned===true?"pass":"warn",hello.provisioned===true?"Плата сообщает, что настроена. Связь с сервером ещё не проверена.":"Плата не подтверждает настройку.");
  if(!sensorTypes.has(hello.sensor_type)){step("sensor","fail","Неизвестный тип датчика.");return report;}
  for(let i=0;i<samples;i++){
    if(cancelled())return stop();
    try{
      const sample=await connection.request("test_measurement");
      if(cancelled())return stop();
      const result=assessSample(sample,hello.sensor_type);
      report.samples.push({index:i+1,ok:result.ok,reason:result.reason,temperature:Number.isFinite(sample.temperature)?sample.temperature:null,humidity:Number.isFinite(sample.humidity)?sample.humidity:null,battery_mv:Number.isFinite(sample.battery_mv)?sample.battery_mv:null,attempts:Number.isInteger(sample.attempts)?sample.attempts:null});
    }catch{
      if(cancelled())return stop();
      report.samples.push({index:i+1,ok:false,reason:"Нет успешного ответа на пробный замер",temperature:null,humidity:null,battery_mv:null,attempts:null});
    }
    onUpdate(report);
    if(i+1<samples)await wait(2200);
  }
  const valid=report.samples.filter(x=>x.ok).length;
  step("sensor",valid===samples?"pass":"fail",valid+" из "+samples+" пробных замеров без ошибок.");
  const volts=report.samples.map(x=>x.battery_mv);
  const adc=volts.every(x=>Number.isFinite(x)&&x>=2000&&x<=5000);
  step("adc",adc?"pass":"warn",adc?"ADC возвращает правдоподобное напряжение по USB. Реальное напряжение батареи нужно проверить отдельно.":"ADC не вернул правдоподобное напряжение во всех замерах. Проверьте питание и калибровку; USB не заменяет тест аккумулятора.");
  return report;
}
