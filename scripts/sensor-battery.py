#!/usr/bin/env python3
"""Estimate runtime using measured battery-side inputs; no built-in cell capacity."""
import argparse
import math
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--capacity-mah',type=float,required=True,help='Measured usable capacity down to working cutoff')
p.add_argument('--sleep-ua',type=float,required=True,help='Whole assembled device at battery input')
p.add_argument('--wake-mah',type=float,required=True,help='Extra charge per measurement wake including Flash/ADC')
p.add_argument('--wifi-mah',type=float,required=True,help='Extra charge per daily sync, including retries')
p.add_argument('--self-discharge-mah-day',type=float,required=True)
p.add_argument('--measure-seconds',type=int,default=1800)
a=p.parse_args()
values=[a.capacity_mah,a.sleep_ua,a.wake_mah,a.wifi_mah,a.self_discharge_mah_day]
if any(not math.isfinite(v) or v<0 for v in values) or a.capacity_mah<=0 or a.measure_seconds<=0:p.error('Positive capacity/interval and finite non-negative charge values required')
charge=24*a.sleep_ua/1000+86400/a.measure_seconds*a.wake_mah+a.wifi_mah+a.self_discharge_mah_day
if charge<=0:p.error('Daily consumption must be positive')
print(f'{charge:.4f} mAh/day; {a.capacity_mah/charge:.1f} days (estimate from supplied inputs)')
