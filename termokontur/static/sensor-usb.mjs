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
    const writing=writer.write(new TextEncoder().encode("TKRPC "+JSON.stringify({...data,id,cmd})+"\n"))
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

export async function flashFactory(port,image,metadata,{checkBoard,progress,log,sdk=null}) {
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",image)),b=>b.toString(16).padStart(2,"0")).join("");
  if(image.byteLength!==metadata.size||digest!==metadata.sha256)throw Error("Контрольная сумма не совпадает. Загрузите комплект заново.");
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
    const bytes=new Uint8Array(image);const pieces=[];
    for(let offset=0;offset<bytes.length;offset+=8192)pieces.push(String.fromCharCode(...bytes.subarray(offset,offset+8192)));
    await loader.writeFlash({fileArray:[{data:pieces.join(""),address:0}],flashMode:"dio",flashFreq:"80m",flashSize:"4MB",eraseAll:true,compress:true,
      reportProgress:(index,written,total)=>progress(Math.round(100*written/total)),calculateMD5Hash:md5});
    await loader.after("hard_reset");
    return hardware;
  } finally {try{await transport.disconnect();}catch{}}
}
