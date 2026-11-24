/* Physical transport is isolated for browser tests. No Wi-Fi secrets are logged. */
export class SensorSerial {
  constructor(port, timeout=12000, onLine=null) {this.port=port;this.timeout=timeout;this.onLine=onLine;this.buffer="";this.pending=new Map();this.closed=false;}
  async open() {
    await this.port.open({baudRate:115200});
    this.opened=true;
    try {
      await this.port.setSignals({dataTerminalReady:false,requestToSend:false});
      this.reader=this.port.readable.getReader();
      this.reading=this.readLoop();
    } catch(error) {
      await this.close();
      throw error;
    }
  }
  async readLoop() {
    const decoder=new TextDecoder();
    try {
      while(!this.closed) {
        const {value,done}=await this.reader.read();if(done)break;
        this.buffer+=decoder.decode(value,{stream:true});
        if(this.buffer.length>32768)this.buffer=this.buffer.slice(-16384);
        let end;
        while((end=this.buffer.indexOf("\n"))>=0) {
          const line=this.buffer.slice(0,end).trimEnd();this.buffer=this.buffer.slice(end+1);
          const frame=line.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").trimStart();
          if(!frame.startsWith("TKRPC ")){this.onLine?.(line);continue;}
          try {const reply=JSON.parse(frame.slice(6));const pending=this.pending.get(reply.id);if(!pending)continue;
            this.pending.delete(reply.id);clearTimeout(pending.timer);
            if(reply.error)pending.reject(Error(reply.error));else pending.resolve(reply);
          }catch{/* Ignore unrelated firmware logs. */}
        }
      }
    } catch(error) {if(!this.closed)this.failure=error;}
    finally {if(!this.closed)this.failure=this.failure||Error("USB-соединение закрыто");this.failAll(this.failure||Error("USB-соединение закрыто"));this.reader.releaseLock();}
  }
  failAll(error) {for(const item of this.pending.values()){clearTimeout(item.timer);item.reject(error);}this.pending.clear();}
  async request(cmd,data={}) {
    if(this.closed||this.failure)throw this.failure||Error("USB-соединение закрыто");
    const id=crypto.randomUUID();
    const writer=this.port.writable.getWriter();
    const response=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);const error=Error("Нет ответа платы по USB за "+(this.timeout/1000)+" с. Проверьте порт и режим настройки.");error.name="USBTimeoutError";reject(error);},this.timeout);
      this.pending.set(id,{resolve,reject,timer});
    });
    const writing=writer.write(new TextEncoder().encode("\nTKRPC "+JSON.stringify({...data,id,cmd})+"\n"))
      .catch(error=>this.failAll(error)).finally(()=>writer.releaseLock());
    const [reply]=await Promise.all([response,writing]);
    return reply;
  }
  async close() {
    if(this.closed)return;
    this.closed=true;this.failAll(Error("USB-соединение закрыто"));
    try{await this.reader?.cancel();await this.reading;}catch{}
    try{if(this.opened)await this.port.close();}catch{}
  }
}

export async function verifyIdentity(hello,expectedHardware=null) {
  if(hello.protocol!==1||!/^04[a-f0-9]{128}$/.test(hello.public_key)||!/^[a-f0-9]{12}$/.test(hello.hardware_id))throw Error("Плата не поддерживает настройку по USB. Нужна прошивка 0.1.4 или новее.");
  const bytes=Uint8Array.from(hello.public_key.match(/../g),b=>parseInt(b,16));
  const fp=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),b=>b.toString(16).padStart(2,"0")).join("");
  if(fp!==hello.fingerprint)throw Error("Отпечаток ключа не совпадает");
  if(expectedHardware&&hello.hardware_id!==expectedHardware)throw Error("Выбран порт другой платы: настройки не отправлены");
  return fp;
}

const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

export async function connectSensor(port,{expectedHardware=null,getPorts=async()=>[],onAttempt=()=>{},attempts=5,timeout=2200,delay=400}={}){
  let lastError;
  for(let attempt=1;attempt<=attempts;attempt++){
    onAttempt(attempt,attempts);
    let candidates=[port];
    if(expectedHardware){
      try{candidates=[...new Set([port,...(await getPorts()).filter(p=>!p.getInfo||p.getInfo().usbVendorId===0x303a)])];}catch{}
    }
    for(const candidate of candidates){
      const serial=new SensorSerial(candidate,timeout);
      try{
        await serial.open();
        const hello=await serial.request("hello");
        const fingerprint=await verifyIdentity(hello,expectedHardware);
        serial.timeout=20000;
        return {serial,hello,fingerprint};
      }catch(error){lastError=error;await serial.close();}
    }
    if(attempt<attempts)await pause(delay);
  }
  throw Error("Не удалось открыть режим настройки ESP32-C3: "+(lastError?.message||"USB недоступен")+". Отключите USB зарядки TP4056, коротко нажмите RESET и выберите «Настроить без прошивки».");
}

// The factory bundle already contains an esptool-merged, configured bootloader.
// Never ask esptool-js to rewrite its binary header or invalidate its digest.
function validateFactoryHeader(bytes) {
  const appOffset = 0x20000;
  if (bytes.length < appOffset + 256 || bytes.length > 0x1e0000 ||
      bytes[0] !== 0xe9 || bytes[12] !== 5 || bytes[13] !== 0 ||
      bytes[appOffset] !== 0xe9 || bytes[appOffset+12] !== 5 || bytes[appOffset+13] !== 0)
    throw Error("Неверный заводской образ ESP32-C3. Запись не начата.");
  // ESP image header: DIO=2, 4MB=0x20, 80MHz=0x0f.
  if (bytes[2] !== 2 || bytes[3] !== 0x2f)
    throw Error("Неверные параметры Flash заводского образа: нужны DIO, 80 МГц, 4 МиБ. Запись не начата.");
}

export function factorySeedImage(bytes, settings) {
  const start=0x12000, size=0x4000, view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  let found=false;
  for(let pos=0x8000;pos<0x9000-31;pos+=32){
    if(view.getUint16(pos,true)!==0x50aa)break;
    const name=new TextDecoder().decode(bytes.subarray(pos+12,pos+28)).split("\0")[0];
    if(name==="factory_setup"&&bytes[pos+2]===1&&bytes[pos+3]===0x41&&view.getUint32(pos+4,true)===start&&view.getUint32(pos+8,true)===size)found=true;
  }
  if(!found||!bytes.subarray(start,start+size).every(b=>b===255))throw Error("Заводской образ не содержит чистый раздел настройки");
  const payload=new TextEncoder().encode(JSON.stringify(settings));
  if(!payload.length||payload.length>12288)throw Error("Настройки слишком большие для заводского образа");
  let crc=0xffffffff;
  for(const b of payload){crc^=b;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}
  const output=bytes.slice(), header=new DataView(output.buffer);
  header.setUint32(start,0x31534b54,true);header.setUint32(start+4,payload.length,true);header.setUint32(start+8,(crc^0xffffffff)>>>0,true);
  output.set(payload,start+12);
  return output;
}

export async function flashFactory(port,image,metadata,{checkBoard,progress,log,sdk=null,prepareSettings=null}) {
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",image)),b=>b.toString(16).padStart(2,"0")).join("");
  if(image.byteLength!==metadata.size||digest!==metadata.sha256)throw Error("Контрольная сумма не совпадает. Загрузите комплект заново.");
  const bytes=new Uint8Array(image);
  validateFactoryHeader(bytes);
  const {ESPLoader,Transport,md5}=sdk||await import("./vendor/esptool.bundle.js");
  const transport=new Transport(port,true);
  const loader=new ESPLoader({transport,baudrate:115200,terminal:{clean(){},write:log,writeLine:log},debugLogging:false});
  try {
    const chip=await loader.main();
    if(!String(chip).includes("ESP32-C3"))throw Error("Нужна ESP32-C3. Запись не начата.");
    if(!((await loader.getFlashSize())>=4096))throw Error("Нужна Flash-память не меньше 4 МиБ. Запись не начата.");
    const hardware=String(await loader.chip.readMac(loader)).replace(/:/g,"").toLowerCase();
    if(!/^[a-f0-9]{12}$/.test(hardware))throw Error("Не удалось прочитать MAC платы");
    await checkBoard(hardware);
    const prepared=prepareSettings?factorySeedImage(bytes,await prepareSettings(hardware)):bytes;
    const pieces=[];
    for(let offset=0;offset<prepared.length;offset+=8192)pieces.push(String.fromCharCode(...prepared.subarray(offset,offset+8192)));
    await loader.writeFlash({fileArray:[{data:pieces.join(""),address:0}],flashMode:"keep",flashFreq:"keep",flashSize:"keep",eraseAll:true,compress:true,
      reportProgress:(index,written,total)=>progress(Math.round(100*written/total)),calculateMD5Hash:md5});
    try{await loader.after("hard_reset");}catch{log("Запись проверена. USB-порт исчез при перезапуске; ожидаем повторного подключения.");}
    return hardware;
  } finally {try{await transport.disconnect();}catch{}}
}
