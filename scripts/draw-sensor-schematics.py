"""Generate editable KiCad 9 module wiring schematics; export SVG/PDF/netlists.

Requires KiCad 9 CLI + libraries, sexpdata, pypdf, scour. Standard symbol graphics
come from KiCad libraries (CC-BY-SA-4.0 with library exception).
"""
from __future__ import annotations
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

import sexpdata as sx
from pypdf import PdfWriter
from scour.scour import scourString, parse_args as scour_options

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'hardware/sensor'
EXPORT = ROOT / 'docs/sensors/schematics'
LIBDIR = Path(os.environ.get('KICAD9_SYMBOL_DIR', '/usr/share/kicad/symbols'))
ENV = {**os.environ, 'XDG_CACHE_HOME': '/tmp/termokontur-kicad-cache',
       'XDG_CONFIG_HOME': '/tmp/termokontur-kicad-config',
       'XDG_DATA_HOME': '/tmp/termokontur-kicad-data', 'KICAD9_SYMBOL_DIR': str(LIBDIR)}
S = sx.Symbol

def node(name, *values): return [S(name), *values]
def children(item, name): return [v for v in item if isinstance(v, list) and str(v[0]) == name]
def child(item, name): return children(item, name)[0]
def uid(key): return str(uuid.uuid5(uuid.NAMESPACE_URL, 'termokontur/schematics/' + key))
def q(value): return round(round(value / 1.27) * 1.27, 6)
def point(x, y): return (q(x), q(y))
def effects(size=1.7, left=False):
    result = node('effects', node('font', node('size', size, size)))
    if left: result.append(node('justify', S('left')))
    return result

def property_(name, value, x, y, hidden=False, left=False):
    result = node('property', name, value, node('at', x, y, 0), effects(left=left))
    if hidden: child(result, 'effects').append(node('hide', S('yes')))
    return result

CACHE = {}
def library_symbol(lib_id):
    if lib_id in CACHE: return copy.deepcopy(CACHE[lib_id])
    lib, name = lib_id.split(':')
    tree = sx.loads((LIBDIR / (lib + '.kicad_sym')).read_text())
    definitions = {v[1]: v for v in children(tree, 'symbol')}
    def resolve(key):
        source = copy.deepcopy(definitions[key])
        parents = children(source, 'extends')
        if not parents: return source
        result = resolve(parents[0][1])
        old = result[1]; result[1] = key
        for v in children(result, 'symbol'):
            v[1] = key + v[1][len(old):]
        for prop in children(source, 'property'):
            result = [v for v in result if not (isinstance(v, list) and str(v[0]) == 'property' and v[1] == prop[1])]
            result.append(prop)
        return result
    result = resolve(name)
    result[1] = lib_id
    CACHE[lib_id] = result
    return copy.deepcopy(result)

CUSTOM = {}
def module(name, left, right, width=40):
    # Terminals are labelled module pads, not bare-IC package pin numbers.
    half = q(width / 2)
    extent = max(abs(y) for _, y in left + right) + 5.08
    result = node('symbol', name, node('pin_names', node('offset', 1.27)),
                  node('pin_numbers', node('hide', S('yes'))), node('in_bom', S('yes')),
                  node('on_board', S('yes')),
                  property_('Reference', 'A', 0, extent + 6),
                  property_('Value', name, 0, extent + 3))
    graphics = node('symbol', name + '_0_1', node('rectangle', node('start', -half, extent),
                    node('end', half, -extent), node('stroke', node('width', .254), node('type', S('default'))),
                    node('fill', node('type', S('background')))))
    result.append(graphics)
    pins = node('symbol', name + '_1_1')
    for side, items in [('left', left), ('right', right)]:
        for label, y in items:
            x = -half - 5.08 if side == 'left' else half + 5.08
            pins.append(node('pin', S('passive'), S('line'), node('at', x, -q(y), 0 if side == 'left' else 180),
                             node('length', 5.08), node('name', label, effects(1.5)),
                             node('number', label, effects(1.5))))
    result.append(pins)
    CUSTOM[name] = result
    result = copy.deepcopy(result); result[1] = 'Termokontur:' + name
    CACHE[result[1]] = result
    return result

module('TP4056_protected', [('IN+', -30.48), ('IN-', -10.16), ('B+', 10.16), ('B-', 30.48)],
       [('OUT+', -10.16), ('OUT-', 10.16)], 42)
module('USB_charge', [], [('VBUS', -10.16), ('GND_USB', 10.16)], 28)
module('SuperMini_power', [('5V', -10.16), ('GND', 10.16)], [('3V3', -10.16)], 32)
module('SuperMini_monitor', [('GPIO4', -10.16), ('GPIO3', 10.16)], [('3V3', -10.16), ('GND', 10.16)], 32)
module('SuperMini_I2C', [], [('3V3', -30.48), ('GPIO6', -10.16), ('GPIO7', 10.16), ('GND', 30.48)], 40)
module('Sensor_I2C', [('VCC', -30.48), ('SDA', -10.16), ('SCL', 10.16), ('GND', 30.48)], [], 40)
module('BME280_module', [('VCC', -30.48), ('SDA', -10.16), ('SCL', 10.16), ('GND', 30.48)],
       [('CSB', -10.16), ('SDO', 10.16)], 32)
module('SuperMini_DATA', [], [('3V3', -20.32), ('GPIO5', 0), ('GND', 20.32)], 40)
module('DHT_module', [('VCC', -20.32), ('DATA', 0), ('GND', 20.32)], [], 40)

class Sheet:
    def __init__(self, name, title, subtitle):
        self.name = name; self.root_id = uid(name)
        self.items = []; self.libs = {}; self.pins = {}; self.expected = {}
        self.text(title, 12.7, 12.7, 2.8)
        self.text(subtitle, 12.7, 21.59, 1.65)
    def text(self, value, x, y, size=1.65):
        self.items.append(node('text', value, node('at', x, y, 0), effects(size, True), node('uuid', uid(self.name + '/text/' + value))))
    def symbol(self, lib_id, ref, value, x, y, angle=0, mirror=None):
        x,y=point(x,y)
        definition = copy.deepcopy(CACHE[lib_id]) if lib_id in CACHE else library_symbol(lib_id)
        self.libs[lib_id] = definition
        symbol_id = uid(self.name + '/' + ref)
        instance = node('symbol', node('lib_id', lib_id), node('at', x,y,angle), node('unit',1),
                        node('in_bom', S('yes')), node('on_board',S('yes')), node('dnp',S('no')),
                        node('uuid',symbol_id))
        if mirror:
            instance.append(node('mirror',S(mirror)))
        custom = lib_id.startswith('Termokontur:')
        if custom:
            shape = child(child(definition,'symbol'),'rectangle')
            top = y - child(shape,'start')[2]
            rx,ry=x,top-7.62; vx,vy=x,top-3.81
        elif lib_id.endswith('R') or lib_id.endswith('C'):
            rx,ry=x+7.62,y-2.54; vx,vy=x+7.62,y+2.54
            if angle == 90: rx,ry=x,y-7.62; vx,vy=x,y-3.81
        elif lib_id == 'Switch:SW_SPST':
            rx,ry=x,y-12.7; vx,vy=x,y-8.89
        elif lib_id == 'Device:Battery_Cell':
            rx,ry=x-17.78,y-3.81; vx,vy=x-17.78,y+2.54
        else:
            rx,ry=x+10.16,y-5.08; vx,vy=x+10.16,y
        instance.extend([property_('Reference',ref,rx,ry,hidden=lib_id=='power:PWR_FLAG',left=lib_id in ('Device:R','Device:C') or lib_id.startswith(('Sensor_Temperature:','Transistor_BJT:'))),property_('Value',value,vx,vy,hidden=lib_id=='power:PWR_FLAG',left=lib_id in ('Device:R','Device:C') or lib_id.startswith(('Sensor_Temperature:','Transistor_BJT:'))),
                         property_('Footprint','',x,y,True),property_('Datasheet','',x,y,True)])
        if mirror == 'y':
            for prop in children(instance,'property')[:2]:
                justifications=children(child(prop,'effects'),'justify')
                if justifications:
                    justifications[0][1]=S('right')
        if angle == 90:
            for prop in children(instance,'property')[:2]:
                child(prop,'at')[3]=90
        for group in children(definition,'symbol'):
            for pin in children(group,'pin'):
                loc=child(pin,'at'); px,py=loc[1:3]; rad=math.radians(angle)
                if mirror=='y': px=-px
                if mirror=='x': py=-py
                pos=(round(x+px*math.cos(rad)-py*math.sin(rad),6), round(y-px*math.sin(rad)-py*math.cos(rad),6))
                number=child(pin,'number')[1]; self.pins[(ref,number)] = pos
                instance.append(node('pin',number,node('uuid',uid(self.name+'/'+ref+'/'+number))))
        instance.append(node('instances',node('project',self.name,node('path','/'+self.root_id,node('reference',ref),node('unit',1)))))
        self.items.append(instance)
        return ref
    def pin(self, ref, number): return self.pins[(ref,str(number))]
    def wire(self,*points):
        points=[point(*p) for p in points]
        for a,b in zip(points,points[1:]):
            if a==b: continue
            assert a[0]==b[0] or a[1]==b[1], (self.name,a,b)
            self.items.append(node('wire',node('pts',node('xy',*a),node('xy',*b)),
                                   node('stroke',node('width',0),node('type',S('default'))),
                                   node('uuid',uid(self.name+'/wire/'+str((a,b))))))
    def dot(self,p):
        self.items.append(node('junction',node('at',*p),node('diameter',0),node('color',0,0,0,0),node('uuid',uid(self.name+'/dot/'+str(p)))))
    def label(self,name,p):
        self.items.append(node('label',name,node('at',*p,0),effects(1.65,True),node('uuid',uid(self.name+'/label/'+name+str(p)))))
    def stub(self,ref,pin,name,dx=0,dy=0):
        a=self.pin(ref,pin); b=point(a[0]+dx,a[1]+dy)
        self.wire(a,b); self.label(name,b); self.expect(name,(ref,pin))
    def expect(self,name,*pins): self.expected.setdefault(name,[]).extend([[r,str(p)] for r,p in pins])
    def flag(self,point_):
        ref='#FLG'+str(len(self.libs)+len(self.items))
        self.symbol('power:PWR_FLAG',ref,'PWR_FLAG',*point_)
    def write(self):
        # KiCad connectivity requires wire segments to terminate at T junctions.
        wires = [v for v in self.items if str(v[0])=='wire']
        anchors = set(self.pins.values())
        for item in self.items:
            if str(item[0]) in ('junction','label'):
                anchors.add(tuple(child(item,'at')[1:3]))
        for wire in wires:
            anchors.update(tuple(p[1:3]) for p in child(wire,'pts')[1:])
        self.items = [v for v in self.items if str(v[0])!='wire']
        for wire in wires:
            a,b=[tuple(p[1:3]) for p in child(wire,'pts')[1:]]
            points=[p for p in anchors if
                    ((a[0]==b[0]==p[0] and min(a[1],b[1])<=p[1]<=max(a[1],b[1])) or
                     (a[1]==b[1]==p[1] and min(a[0],b[0])<=p[0]<=max(a[0],b[0])))]
            points.sort(key=lambda p:abs(p[0]-a[0])+abs(p[1]-a[1]))
            self.wire(*points)
        root=node('kicad_sch',node('version',20250114),node('generator',S('eeschema')),
                  node('uuid',self.root_id),node('paper','User',240,180),
                  node('lib_symbols',*self.libs.values()),*self.items,
                  node('sheet_instances',node('path','/',node('page','1'))))
        path=SOURCE/(self.name+'.kicad_sch')
        path.write_text(sx.dumps(root)+'\n')
        path.with_suffix('.kicad_pro').write_text(json.dumps({'meta': {'version': 1}},indent=2)+'\n')
        return path

sheets=[]
s=Sheet('01-power','01. Питание и разделение USB','Соединения модулей; маркировка выводов имеет приоритет над расположением на рисунке.')
s.symbol('Termokontur:USB_charge','J1','USB-C TP4056',25.4,54.61)
s.symbol('Termokontur:TP4056_protected','A2','TP4056 + защита',101.6,74.93)
s.symbol('Device:Battery_Cell','BT1','BL-5C, 1S',35.56,90.17)
s.symbol('Switch:SW_SPST','SW1','разомкнуть при USB',151.13,64.77)
s.symbol('Termokontur:SuperMini_power','A1','ESP32-C3 SuperMini',198.12,74.93)
for jp,tp,net in [('VBUS','IN+','CHG_5V'),('GND_USB','IN-','USB_RETURN')]:
 a=s.pin('J1',jp); b=s.pin('A2',tp);s.wire(a,b);s.label(net,point((a[0]+b[0])/2,a[1]));s.expect(net,('J1',jp),('A2',tp));s.flag(a)
a=s.pin('BT1',1);b=s.pin('A2','B+');s.wire(a,b);s.label('BAT_PLUS',a);s.expect('BAT_PLUS',('BT1',1),('A2','B+'));s.flag(a)
a=s.pin('BT1',2);b=s.pin('A2','B-');s.wire(a,(a[0],b[1]),b);s.label('BAT_MINUS',point(a[0],b[1]));s.expect('BAT_MINUS',('BT1',2),('A2','B-'));s.flag(a)
s.wire(s.pin('A2','OUT+'),s.pin('SW1',1));s.label('PROT_PLUS',s.pin('A2','OUT+'));s.expect('PROT_PLUS',('A2','OUT+'),('SW1',1))
s.wire(s.pin('SW1',2),s.pin('A1','5V'));s.label('VBAT_SW',s.pin('SW1',2));s.expect('VBAT_SW',('SW1',2),('A1','5V'))
s.wire(s.pin('A2','OUT-'),s.pin('A1','GND'));s.label('GND_SYS',s.pin('A2','OUT-'));s.expect('GND_SYS',('A2','OUT-'),('A1','GND'))
s.stub('A1','3V3','3V3',dx=10.16)
for ref,x,net in [('C2',106.68,'VBAT_SW'),('C3',172.72,'3V3')]:
 s.symbol('Device:C',ref,'47u / 10V *',x,130.81);s.stub(ref,1,net,dy=-7.62);s.stub(ref,2,'GND_SYS',dy=7.62)
s.text('* Дополнительные ёмкости: уточнить требования фактического LDO и питание на стенде.',12.7,153.67)
s.text('B− и OUT− не соединять перемычкой. Общая земля логики — только GND_SYS = OUT−.',12.7,161.29)
s.text('5V — имя вывода платы: от батареи здесь 3,3…4,2 В. USB SuperMini — сервисный, SW1 OFF.',12.7,168.91)
sheets.append(s)

s=Sheet('02-charge-adc','02. Детектор зарядного USB и напряжение батареи','GPIO4: активный ноль. GPIO3: ADC1_CH3. GND_SYS подключается к OUT− TP4056.')
s.symbol('Termokontur:SuperMini_monitor','A1','ESP32-C3 SuperMini',193.04,69.85)
s.symbol('Transistor_BJT:2N3904','Q1','2N3904',95.25,69.85)
s.symbol('Device:R','R1','100k',120.65,44.45)
s.symbol('Device:R','R2','100k',54.61,69.85,90)
s.symbol('Device:R','R3','1M',74.93,90.17)
s.stub('R2',1,'CHG_5V',dx=-17.78);s.flag(point(s.pin('R2',1)[0]-17.78,s.pin('R2',1)[1]))
a=s.pin('R2',2); b=s.pin('Q1',2); bp=(s.pin('R3',1)[0],a[1]);s.wire(a,b);s.wire(bp,s.pin('R3',1));s.dot(bp);s.label('USB_BASE',bp);s.expect('USB_BASE',('Q1',2),('R2',2),('R3',1))
s.stub('R3',2,'GND_SYS',dy=7.62);s.stub('Q1',1,'GND_SYS',dy=26.67)
a=s.pin('Q1',3);g=s.pin('A1','GPIO4');node_=(a[0],g[1]);s.wire(a,node_,g);s.label('USB_DET_N',node_);s.expect('USB_DET_N',('Q1',3),('R1',2),('A1','GPIO4'))
r=s.pin('R1',2);bp=(r[0],g[1]);s.wire(r,bp);s.dot(bp);s.stub('R1',1,'3V3',dy=-7.62)
s.stub('A1','3V3','3V3',dx=7.62);s.stub('A1','GND','GND_SYS',dx=7.62)
s.symbol('Device:R','R4','1M / 1%',146.05,95.25);s.symbol('Device:R','R5','1M / 1%',146.05,125.73);s.symbol('Device:C','C1','100n',196.85,125.73)
s.stub('R4',1,'VBAT_SW',dy=-5.08);s.flag(point(s.pin('R4',1)[0],s.pin('R4',1)[1]-5.08))
node_=point(146.05,110.49);s.wire(s.pin('R4',2),node_,s.pin('R5',1));s.wire(node_,(s.pin('C1',1)[0],node_[1]),s.pin('C1',1));s.dot(node_)
g=s.pin('A1','GPIO3');bend=point(167.64,g[1]);s.wire(g,bend,(bend[0],node_[1]));s.dot((bend[0],node_[1]));s.label('VBAT_ADC',node_)
s.expect('VBAT_ADC',('R4',2),('R5',1),('C1',1),('A1','GPIO3'));s.stub('R5',2,'GND_SYS',dy=7.62);s.stub('C1',2,'GND_SYS',dy=7.62)
s.text('CHG_5V = IN+ TP4056. Проверить связь IN− и OUT− на конкретном модуле; не шунтировать защиту.',12.7,151.13)
s.text('E/B/C сверять с datasheet транзистора. Обозначение не задаёт ориентацию корпуса на макетке.',12.7,158.75)
s.text('При VBAT_SW = 4,2 В: ADC ≈ 2,1 В; ток делителя 2,1 мкА; τ ≈ 50 мс; ожидание в ПО 500 мс.',12.7,166.37)
sheets.append(s)

def bus_sheet(name,title,sensor_lib,sensor_value,sensor_x=187.96):
 s=Sheet(name,title,'Один выбранный модуль на плату. Питание и уровни SDA/SCL — 3,3 В.')
 s.symbol('Termokontur:SuperMini_I2C','A1','ESP32-C3 SuperMini',45.72,74.93)
 s.symbol(sensor_lib,'A3',sensor_value,sensor_x,74.93)
 for ap,sp,net in [('3V3','VCC','3V3'),('GPIO6','SDA','SDA'),('GPIO7','SCL','SCL'),('GND','GND','GND_SYS')]:
  a=s.pin('A1',ap);b=s.pin('A3',sp);s.wire(a,b);s.label(net,point((a[0]+b[0])/2,a[1]));s.expect(net,('A1',ap),('A3',sp))
 for ref,x,data in [('R6',97.79,'SDA'),('R7',121.92,'SCL')]:
  data_pin=s.pin('A3',data);center=point(x,data_pin[1]-10.16)
  s.symbol('Device:R',ref,'4k7 *',*center)
  b=(s.pin(ref,2)[0],data_pin[1]);s.wire(s.pin(ref,2),b);s.dot(b);s.expect(data,(ref,2));s.stub(ref,1,'3V3',dy=-2.54)
 s.symbol('Device:C','C4','100n *',180.34,130.81);s.stub('C4',1,'3V3',dy=-5.08);s.stub('C4',2,'GND_SYS',dy=5.08)
 return s
s=bus_sheet('03-i2c','03. SHT3x / SHT4x / AHT20','Termokontur:Sensor_I2C','SHT / AHT, модуль')
s.text('Выбрать один тип в настройке. SHT3x: 0x44/0x45; SHT4x: 0x44/0x45/0x46; AHT20: 0x38.',12.7,151.13)
s.text('* R6/R7 и C4 добавлять, если их нет на модуле. Не удваивать подтяжки без расчёта нагрузки.',12.7,158.75)
s.text('Дополнительные выводы конкретного breakout-модуля подключать по его документации.',12.7,166.37)
sheets.append(s)
s=bus_sheet('04-bme280','04. BME280 в режиме I²C','Termokontur:BME280_module','BME280, модуль',162.56)
s.stub('A3','CSB','3V3',dx=10.16);s.stub('A3','SDO','GND_SYS',dx=10.16)
s.text('Показан адрес 0x76: SDO → GND_SYS. Для 0x77 вместо этого подключить SDO к 3V3.',12.7,151.13)
s.text('CSB → 3V3 для I²C. Выводы названы по модулю, это не номера контактов голой LGA-микросхемы.',12.7,158.75)
s.text('* Подтяжки и 100 нФ — при отсутствии на модуле. BMP280 не имеет влажности и не подходит.',12.7,166.37)
sheets.append(s)

s=Sheet('05-dht','05. DHT11 / DHT22 / AM2302','Модуль с питанием и выходом данных, совместимыми с логикой 3,3 В; один датчик.')
s.symbol('Termokontur:SuperMini_DATA','A1','ESP32-C3 SuperMini',45.72,74.93)
s.symbol('Termokontur:DHT_module','A3','DHT11 / DHT22',187.96,74.93)
for ap,sp,net in [('3V3','VCC','3V3'),('GPIO5','DATA','DATA'),('GND','GND','GND_SYS')]:
 a=s.pin('A1',ap);b=s.pin('A3',sp);s.wire(a,b);s.label(net,point((a[0]+b[0])/2,a[1]));s.expect(net,('A1',ap),('A3',sp))
s.symbol('Device:R','R8','4k7 *',115.57,64.77)
a=s.pin('R8',1);b=(a[0],s.pin('A1','3V3')[1]);s.wire(a,b);s.dot(b)
a=s.pin('R8',2);b=(a[0],s.pin('A1','GPIO5')[1]);s.wire(a,b);s.dot(b)
s.expect('DATA',('R8',2));s.expect('3V3',('R8',1))
s.text('* R8 добавить, если подтяжки нет на модуле. Нельзя подтягивать GPIO5 к 5 В.',12.7,125.73)
s.text('Проверить минимальное питание купленного DHT11; некоторые варианты требуют более 3,3 В.',12.7,135.89)
s.text('У голого датчика и breakout-модуля разный набор выводов. NC не подключать; сверить datasheet.',12.7,146.05)
s.text('Если модуль не совместим с 3,3 В, эта схема к нему неприменима без отдельного согласования уровней.',12.7,156.21)
sheets.append(s)

s=Sheet('06-ds18b20','06. DS18B20 с внешним питанием','Один датчик на GPIO5. Паразитное питание и несколько датчиков на линии не поддерживаются.')
s.symbol('Termokontur:SuperMini_DATA','A1','ESP32-C3 SuperMini',45.72,74.93)
s.symbol('Sensor_Temperature:DS18B20','U1','DS18B20',175.26,74.93,mirror='y')
# Resolve logical package pin positions from the KiCad library, not guessed drawing coordinates.
for ap,dp,net in [('3V3','3','3V3'),('GPIO5','2','DQ'),('GND','1','GND_SYS')]:
 a=s.pin('A1',ap);b=s.pin('U1',dp)
 if ap=='GPIO5': s.wire(a,b)
 else: s.wire(a,(b[0],a[1]),b)
 s.label(net,point(97.79,a[1]));s.expect(net,('A1',ap),('U1',dp))
s.symbol('Device:R','R8','4k7',115.57,64.77)
for pin,ap in [(1,'3V3'),(2,'GPIO5')]:
 a=s.pin('R8',pin);b=(a[0],s.pin('A1',ap)[1]);s.wire(a,b);s.dot(b)
s.expect('DQ',('R8',2));s.expect('3V3',('R8',1))
s.symbol('Device:C','C5','100n',175.26,125.73);s.stub('C5',1,'3V3',dy=-5.08);s.stub('C5',2,'GND_SYS',dy=5.08)
s.flag(point(97.79,s.pin('A1','3V3')[1]));s.flag(point(97.79,s.pin('A1','GND')[1]))
s.text('Для TO-92 показаны номера DS18B20: 1 = GND, 2 = DQ, 3 = VDD. Ориентацию сверить с datasheet.',12.7,151.13)
s.text('У герметичных щупов цвета жил не стандартизованы: проверить маркировку поставщика.',12.7,158.75)
s.text('Влажность отсутствует. C5 располагать рядом с датчиком; для длинного кабеля нужна проверка линии.',12.7,166.37)
sheets.append(s)

SOURCE.mkdir(parents=True,exist_ok=True); EXPORT.mkdir(parents=True,exist_ok=True)
(SOURCE/'Termokontur.kicad_sym').write_text(sx.dumps(node('kicad_symbol_lib',node('version',20241209),node('generator',S('kicad_symbol_editor')),*CUSTOM.values()))+'\n')
libs=['Termokontur','Device','Switch','Transistor_BJT','Sensor_Temperature','power']
table=node('sym_lib_table')
for lib in libs:
 uri='${KIPRJMOD}/Termokontur.kicad_sym' if lib=='Termokontur' else '${KICAD9_SYMBOL_DIR}/'+lib+'.kicad_sym'
 table.append(node('lib',node('name',lib),node('type','KiCad'),node('uri',uri),node('options',''),node('descr','')))
(SOURCE/'sym-lib-table').write_text(sx.dumps(table)+'\n')
# A private CLI library table is needed on a fresh headless KiCad profile.
profile=Path(ENV['XDG_CONFIG_HOME'])/'kicad/9.0'
profile.mkdir(parents=True,exist_ok=True)
global_table=copy.deepcopy(table)
for lib in global_table[1:]:
    uri=child(lib,'uri')
    uri[1]=uri[1].replace('${KIPRJMOD}',str(SOURCE))
(profile/'sym-lib-table').write_text(sx.dumps(global_table)+'\n')
ENV['KICAD_CONFIG_HOME']=str(profile)

reports=[]; writer=PdfWriter()
for sheet in sheets:
 path=sheet.write()
 def run(*args):
  result=subprocess.run(['kicad-cli',*map(str,args)],env=ENV,text=True,capture_output=True)
  if result.returncode: raise RuntimeError(result.stdout+result.stderr)
  return result
 run('sch','export','svg','--exclude-drawing-sheet','--output',EXPORT,path)
 svg=EXPORT/(sheet.name+'.svg')
 svg.write_text(scourString(svg.read_text(),scour_options(['--set-precision=6','--remove-metadata','--strip-xml-prolog','--no-line-breaks'])))
 run('sch','export','pdf','--exclude-drawing-sheet','--output',EXPORT/(sheet.name+'.pdf'),path)
 run('sch','export','netlist','--format','kicadxml','--output',SOURCE/(sheet.name+'.net'),path)
 run('sch','erc','--format','json','--output',SOURCE/(sheet.name+'.erc.json'),path)
 netlist=ET.parse(SOURCE/(sheet.name+'.net'))
 actual={n.attrib['name'].lstrip('/'): {(v.attrib['ref'],v.attrib['pin']) for v in n.findall('node')} for n in netlist.findall('.//nets/net')}
 for net,pins in sheet.expected.items():
  assert set(map(tuple,pins)).issubset(actual.get(net,set())),(sheet.name,net,pins,actual)
 report=json.loads((SOURCE/(sheet.name+'.erc.json')).read_text())
 violations=[v for entry in report.get('sheets',[]) for v in entry.get('violations',[])]
 assert not violations,(sheet.name,violations)
 reports.append({'sheet':sheet.name,'expected_nets':sheet.expected,'erc':report})
 writer.append(EXPORT/(sheet.name+'.pdf'))
 print(sheet.name, 'exported and net connections checked',flush=True)
for page in writer.pages:
 page.compress_content_streams()
writer.compress_identical_objects()
writer.write(EXPORT/'termokontur-circuits.pdf')
for sheet in sheets:
 (EXPORT/(sheet.name+'.pdf')).unlink()
(SOURCE/'validation.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n')
print('Six KiCad schematics exported; netlist checks passed; all ERC reports clean.')
