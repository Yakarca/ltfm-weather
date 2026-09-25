#!/usr/bin/env python3
"""LTFM locked D0/D1 engine. Python standard library only; no LLM arithmetic.

Run: python ltfm_engine_v2.py --fetch --out run_directory
Replay: python ltfm_engine_v2.py --replay run_directory/snapshot.json --out replay
The snapshot, including the frozen reference time, is the complete input.
No historical fit is bundled. Default probabilities are UNCALIBRATED estimates.
"""
import argparse, bisect, collections, datetime as dt, hashlib, json, math
import pathlib, re, statistics, urllib.parse, urllib.request
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

VERSION = 'LTFM-D0D1-LOCKED-v2.1.0-HOURLY-RESIDUAL-PHYSICS'
TZ = ZoneInfo('Europe/Istanbul')
UTC = dt.timezone.utc
HOUR = dt.timedelta(hours=1)
BASE = 'https://raw.githubusercontent.com/Yakarca/ltfm-weather/refs/heads/main/data/'
NOAA = 'https://www.weather.gov/wrh/timeseries?site=LTFM'
FAMILIES = {
 'ecmwf_ifs':'IFS','ecmwf_ifs025_ensemble':'IFS',
 'ecmwf_aifs025_single':'AIFS','ecmwf_aifs025_ensemble':'AIFS',
 'ncep_gfs_seamless':'NCEP','ncep_hgefs025_ensemble_mean':'NCEP','ncep_gefs025':'NCEP',
 'icon_eu':'ICON','dwd_icon_eu_eps':'ICON',
 'ukmo_global_deterministic_10km':'UKMO','ukmo_global_ensemble_20km':'UKMO',
 'meteofrance_arpege_europe':'ARPEGE'}
BLOCKS = {'surface':('radiation','cloud','rain','gust'),
          'airmass':('advection','front','upper_humidity','shear'),
          'boundary':('mixing','humidity','pressure_trend')}
BLOCK_WEIGHTS = {'surface':.45,'airmass':.35,'boundary':.20}
RUN_TIME_KEYS = ('run_time','initialization_time','generated_at','updated_at',
                 'issue_time','forecast_reference_time')

def q(x):
    return float(Decimal(str(x)).quantize(Decimal('.000001'), rounding=ROUND_HALF_UP))

def clip(x,a,b): return max(a,min(b,x))
def valid(x): return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
def sign(x): return (x>0)-(x<0)
def median(xs):
    xs=sorted(x for x in xs if x is not None)
    return q(statistics.median(xs)) if xs else None
def stamp(x):
    t=dt.datetime.fromisoformat(x.replace('Z','+00:00'))
    return t.astimezone(TZ) if t.tzinfo else t.replace(tzinfo=TZ)
def model_run_time(data):
    """Return the first trustworthy model issue time exposed by the source."""
    containers=[data]
    for key in ('metadata','meta','api_metadata'):
        value=data.get(key) if isinstance(data,dict) else None
        if isinstance(value,dict):containers.append(value)
    for container in containers:
        for key in RUN_TIME_KEYS:
            value=container.get(key)
            if isinstance(value,str):
                try:return stamp(value)
                except (TypeError,ValueError):continue
    return None
def canonical(x): return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(x): return hashlib.sha256(canonical(x).encode()).hexdigest()
def wmedian(pairs):
    pairs=sorted((x,w) for x,w in pairs if w>0)
    if not pairs:return None
    total=math.fsum(w for x,w in pairs); acc=0
    for i,(x,w) in enumerate(pairs):
        acc+=w
        if abs(acc-total/2)<1e-12:
            return q((x+pairs[min(i+1,len(pairs)-1)][0])/2)
        if acc>total/2:return q(x)
    return q(pairs[-1][0])
def angle(a,b): return abs((b-a+180)%360-180)
def angular_lerp(a,b,z):
    delta=(b-a+180)%360-180
    if delta==-180:delta=180
    return q((a+z*delta)%360)%360
def quantile(xs,p):
    xs=sorted(xs); z=(len(xs)-1)*p; i=math.floor(z);j=math.ceil(z)
    return q(xs[i]+(z-i)*(xs[j]-xs[i]))

def solar(t):
    n=t.timetuple().tm_yday;r=math.radians
    b=r(360*(n-81)/364)
    e=9.87*math.sin(2*b)-7.53*math.cos(b)-1.5*math.sin(b)
    clock=t.hour+t.minute/60+t.second/3600
    st=clock+(4*(28.75194-45)+e)/60
    decl=r(23.44*math.sin(r(360*(284+n)/365)))
    alt=math.sin(r(41.27528))*math.sin(decl)+math.cos(r(41.27528))*math.cos(decl)*math.cos(r(15*(st-12)))
    return q(1361*(1+.033*math.cos(r(360*n/365)))*max(0,alt))

def hourly_solar(t):
    # Twelve fixed midpoint samples over the SAME preceding hour as Open-Meteo SW.
    return q(math.fsum(solar(t-HOUR+dt.timedelta(minutes=2.5+5*i)) for i in range(12))/12)

class Series:
    def __init__(self,name,data,member=0,ensemble=False):
        self.name=name; self.family=FAMILIES[name];self.member=member;self.ensemble=ensemble
        self.run_time=model_run_time(data)
        self.key=f'{name}:{member:03d}';self.data=data;self.cache={};self.feature_cache={}
        self.audit=collections.Counter()
        self.times=[stamp(t) for t in data['hourly']['time']]
        if len(set(self.times))!=len(self.times):raise ValueError('duplicate model times')
        self.order=sorted(range(len(self.times)),key=lambda i:self.times[i])
        self.times=[self.times[i] for i in self.order]
    def field(self,var):
        return var+(f'_member{self.member:02d}' if self.member else '')
    def points(self,var):
        if var in self.cache:return self.cache[var]
        field=self.field(var);raw=self.data['hourly'].get(field,[])
        unit=self.data.get('hourly_units',{}).get(field)
        points=[]
        for t,i in zip(self.times,self.order):
            if i>=len(raw) or not valid(raw[i]):continue
            x=raw[i]
            if var.startswith('temperature') or var=='dew_point_2m':
                if unit in ('°F','F'):x=(x-32)*5/9
                elif unit not in ('°C','C'):continue
            elif var.startswith('wind_speed') or var.startswith('wind_gusts'):
                if unit=='m/s':x*=3.6
                elif unit=='mph':x*=1.609344
                elif unit in ('kn','knots','kt'):x*=1.852
                elif unit!='km/h':continue
            elif var=='precipitation':
                if unit in ('inch','in'):x*=25.4
                elif unit!='mm':continue
            elif var in ('pressure_msl','surface_pressure'):
                if unit=='Pa':x/=100
                elif unit not in ('hPa','mb'):continue
            elif var.startswith('geopotential_height') and unit!='m':continue
            elif var=='shortwave_radiation' and unit not in ('W/m²','W/m2'):continue
            elif var.startswith('wind_direction') and unit not in ('°','degree'):continue
            elif var.startswith(('cloud_cover','relative_humidity')) and unit!='%':continue
            if var.startswith('wind_direction'):x%=360
            if var=='temperature_2m' and not -90<=x<=65:continue
            if var.startswith(('cloud_cover','relative_humidity')) and not 0<=x<=100:continue
            if var in ('precipitation','shortwave_radiation') and x<0:continue
            points.append((t,q(x)))
        self.cache[var]=points
        return points
    def value(self,var,t,interpolate=True):
        pts=self.points(var)
        if not pts:return None
        times=[p[0] for p in pts];i=bisect.bisect_left(times,t)
        if i<len(pts) and times[i]==t:return pts[i][1]
        # Never fabricate interval sums or radiation averages; no extrapolation.
        if not interpolate or var in ('precipitation','shortwave_radiation') or i==0 or i==len(pts):return None
        a,x=pts[i-1];b,y=pts[i]
        if b-a>2*HOUR:return None
        z=(t-a)/(b-a)
        return angular_lerp(x,y,z) if var.startswith('wind_direction') else q(x+z*(y-x))

def load_series(det,ens,warnings,cutoff=None):
    ds=[];es=[]
    for ensemble,src,dest in ((False,det,ds),(True,ens,es)):
        for name,data in sorted(src.items()):
            if name not in FAMILIES:
                warnings.add('Tanınmayan model kullanılmadı: '+name);continue
            if not isinstance(data,dict) or 'hourly' not in data:continue
            run_time=model_run_time(data)
            if cutoff is not None and run_time is not None and run_time>cutoff:
                warnings.add('Karar saatinden sonraki model koşusu kullanılmadı: '+name)
                continue
            lat=data.get('latitude');lon=data.get('longitude');elev=data.get('elevation')
            if not valid(lat) or not valid(lon) or abs(lat-41.27528)>.5 or abs(lon-28.75194)>.5:
                warnings.add('Konumu doğrulanamayan model kullanılmadı: '+name);continue
            if not valid(elev) or abs(elev-99)>1:
                warnings.add('99 m hedef yüksekliği uyuşmayan model kullanılmadı: '+name);continue
            members=[0]
            if ensemble:
                members=sorted({0 if k=='temperature_2m' else int(k.rsplit('member',1)[1]) for k in data['hourly'] if re.fullmatch(r'temperature_2m(?:_member\d+)?',k)})
            for m in members:
                try:dest.append(Series(name,data,m,ensemble))
                except (ValueError,KeyError):warnings.add('Geçersiz zaman dizisi: '+name)
    return ds,es

def getter(s,det,var,t):
    v=s.value(var,t)
    if v is not None:s.audit[var+':member' if s.ensemble else var+':deterministic']+=1;return v
    if var=='temperature_2m':return None
    candidates=sorted((d for d in det if d.family==s.family and d.key!=s.key),
                      key=lambda d:(0 if 'ensemble_mean' in d.name else 1,d.name))
    for d in candidates:
        v=d.value(var,t)
        if v is not None:s.audit[var+':fallback:'+d.name]+=1;return v
    s.audit[var+':missing']+=1
    return None

def features(s,det,t):
    if t in s.feature_cache:return s.feature_cache[t]
    get=lambda var,when=t:getter(s,det,var,when)
    end=t.replace(minute=0,second=0,microsecond=0)
    day=t.replace(hour=0,minute=0,second=0,microsecond=0)
    out={'daylight':int(solar(t)>0)}
    # Trailing samples only: no weather after the candidate maximum.
    past=[end-2*HOUR,end-HOUR,end]
    ownlayers=[median([s.value(v,z) for z in past]) for v in ('cloud_cover_low','cloud_cover_mid','cloud_cover_high')]
    owncloud=median([s.value('cloud_cover',z) for z in past])
    layers=[median([get(v,z) for z in past]) for v in ('cloud_cover_low','cloud_cover_mid','cloud_cover_high')]
    if all(v is not None for v in ownlayers):c=sum(w*v for w,v in zip((.6,.25,.15),ownlayers))
    elif owncloud is not None:c=owncloud
    elif all(v is not None for v in layers):c=sum(w*v for w,v in zip((.6,.25,.15),layers))
    else:c=get('cloud_cover')
    if c is not None:out['cloud']=q((1 if out['daylight'] else -1)*-clip((c-50)/50,-1,1))
    rain=[get('precipitation',z) for z in past]
    p=math.fsum(rain) if all(x is not None for x in rain) else None
    if p is not None:out['rain']=q(-clip(p,0,1))
    temp=s.value('temperature_2m',t);dew=get('dew_point_2m');rh=get('relative_humidity_2m')
    if temp is not None and dew is not None:
        out['humidity']=q((1 if out['daylight'] else -1)*clip((max(0,temp-dew)-5)/8,-1,1))
    elif rh is not None:
        out['humidity']=q((1 if out['daylight'] else -1)*clip((50-rh)/50,-1,1))
    sums=[];possible=0;z=day+HOUR
    while z<=end:
        ext=hourly_solar(z)
        if ext>=100:
            possible+=1;sw=get('shortwave_radiation',z)
            if sw is not None:sums.append((sw,ext))
        z+=HOUR
    if out['daylight'] and possible and len(sums)==possible:
        ratio=clip(math.fsum(x for x,y in sums)/math.fsum(y for x,y in sums),0,1.2)
        out['radiation']=q(clip((ratio-.55)/.30,-1,1))
    t925=get('temperature_925hPa'); t850=get('temperature_850hPa')
    # A front/air-mass signal is based on both 3-hour and 6-hour evolution,
    # so one noisy model step cannot dominate the regime match.
    changes=[]
    for lag,weight in ((3,.7),(6,.3)):
        old925=get('temperature_925hPa',t-lag*HOUR)
        old850=get('temperature_850hPa',t-lag*HOUR)
        if t925 is not None and old925 is not None:
            change=t925-old925
            if t850 is not None and old850 is not None:
                change=.7*change+.3*(t850-old850)
            changes.append((weight,change))
    a=None
    if changes:
        change=math.fsum(w*x for w,x in changes)/math.fsum(w for w,x in changes)
        a=clip(change/1.2,-1,1)
    d925=get('wind_direction_925hPa');d850=get('wind_direction_850hPa')
    align=(1+math.cos(math.radians(angle(d925,d850))))/2 if d925 is not None and d850 is not None else .5
    if a is not None:out['advection']=q(a*(.6+.4*align))
    upper_rh=median([get('relative_humidity_925hPa'),get('relative_humidity_850hPa')])
    if upper_rh is not None:out['upper_humidity']=q(clip((upper_rh-50)/50,-1,1))
    supports=[]
    nowp=get('pressure_msl');oldp=get('pressure_msl',t-3*HOUR)
    if nowp is None or oldp is None:
        nowp=get('surface_pressure');oldp=get('surface_pressure',t-3*HOUR)
    if nowp is not None and oldp is not None:
        pressure_change=nowp-oldp
        out['pressure_trend']=q(clip(pressure_change/2,-1,1))
        supports.append(clip((pressure_change-.5)/2,0,1))
    for var,threshold in [('wind_direction_10m',30),
                          ('wind_direction_925hPa',25),
                          ('wind_direction_850hPa',25)]:
        v=get(var);old=get(var,t-3*HOUR)
        if v is not None and old is not None:supports.append(clip((angle(v,old)-threshold)/90,0,1))
    if p is not None:supports.append(clip(p,0,1))
    if upper_rh is not None:supports.append(clip((upper_rh-60)/40,0,1))
    if a is not None and supports:out['front']=q(-max(0,-out['advection'])*max(supports))
    wind=median([get('wind_speed_10m',z) for z in past]);z925=get('geopotential_height_925hPa')
    gust=median([get('wind_gusts_10m',z) for z in past])
    if wind is not None and gust is not None:
        out['gust']=q(clip((gust-wind-3)/15,-1,1))
    upper_wind=median([get('wind_speed_925hPa'),get('wind_speed_850hPa')])
    if wind is not None and upper_wind is not None:
        out['shear']=q(clip((upper_wind-wind)/20,-1,1))
    if all(x is not None for x in (temp,t925,z925,wind)):
        out['mixing']=q(clip((t925+.0098*(z925-99)-temp-3)/4,-1,1)*clip((wind-10)/20,0,1))
    direction=get('wind_direction_10m')
    if direction is not None and wind is not None and wind>=1:out['direction']=direction
    s.feature_cache[t]=out
    return out

def similarity(a,b):
    if a['daylight']!=b['daylight']:return 0
    parts=[]
    for block,names in BLOCKS.items():
        names=[k for k in names if k in a and k in b]
        if names:parts.append((BLOCK_WEIGHTS[block],math.fsum(abs(a[k]-b[k])/2 for k in names)/len(names)))
    if not parts:return 0
    score=max(0,1-math.fsum(w*d for w,d in parts)/math.fsum(w for w,d in parts))
    if 'direction' in a and 'direction' in b:score*=(1+math.cos(math.radians(angle(a['direction'],b['direction']))))/2
    if 'advection' in a and 'advection' in b and a['advection']*b['advection']<0:score*=1-min(abs(a['advection']),abs(b['advection']))
    return q(score)

def normalize_obs(raw,asof):
    groups=collections.defaultdict(set)
    for row in raw:
        if row.get('station')!='LTFM' or not row.get('valid',True):continue
        try:t=stamp(row['time']);x=float(row['temp_c'])
        except (KeyError,ValueError,TypeError):continue
        if t>asof or not math.isfinite(x) or not -90<=x<=65:continue
        # Same Math.round behavior used by the NOAA metric table (including negatives).
        groups[t].add(math.floor(x+.5))
    return [(t,next(iter(vals))) for t,vals in sorted(groups.items()) if len(vals)==1]

def live_rows(family,ds,es,obs,asof):
    models=[s for s in es if s.family==family] or [s for s in ds if s.family==family]
    if not models:return []
    bins=collections.defaultdict(list)
    for t,x in obs:
        if not 0<=(asof-t).total_seconds()<=21600:continue
        vals=[s.value('temperature_2m',t) for s in models]
        modeltemp=median(vals)
        if modeltemp is None:continue
        fs=[features(s,ds,t) for s in models]
        merged={'daylight':int(solar(t)>0)}
        for k in sorted(set().union(*(set(a) for a in fs))-{'daylight','direction'}):
            v=median([a.get(k) for a in fs])
            if v is not None:merged[k]=v
        dirs=[a['direction'] for a in fs if 'direction' in a]
        if dirs:
            sx=math.fsum(math.sin(math.radians(x)) for x in dirs);cx=math.fsum(math.cos(math.radians(x)) for x in dirs)
            if math.hypot(sx,cx)>1e-8:merged['direction']=q(math.degrees(math.atan2(sx,cx))%360)
        bins[t.replace(minute=0,second=0,microsecond=0)].append((t,q(clip(x-modeltemp,-3,3)),merged))
    rows=[]
    for hour,items in sorted(bins.items()):
        # One independent hourly contribution: dense SPECI sequences cannot gain extra votes.
        t=items[-1][0];e=median([r[1] for r in items]);f=items[-1][2]
        age=(asof-t).total_seconds()/3600;w=3 if age<=2 else 2 if age<=4 else 1
        rows.append((t,e,w,f))
    return rows

def correction(feat,rows,t):
    matched=[(e,q(w*similarity(feat,f)),ot) for ot,e,w,f in rows]
    matched=[x for x in matched if x[1]>0]
    if len(matched)<3:return 0.,0
    bias=wmedian([(e,w) for e,w,ot in matched])
    signed=[(e,w) for e,w,ot in matched if abs(e)>=.2]
    if not bias or not signed:return 0.,len(matched)
    agree=math.fsum(w for e,w in signed if sign(e)==sign(bias))/math.fsum(w for e,w in signed)
    cons=.4 if agree<.6 else .7 if agree<.8 else 1
    lead=max(0,(t-max(ot for e,w,ot in matched)).total_seconds()/3600)
    decay=clip(1-lead/36,.25,1)
    return q(clip(.45*bias*cons*decay,-.6,.6)),len(matched)

def raw_bandwidth(xs):
    n=len(xs)
    if n<5:return .35
    spread=min(statistics.stdev(xs),(quantile(xs,.75)-quantile(xs,.25))/1.349)
    return q(clip(.9*spread*n**(-.2),.35,.60)) if spread>0 else .35

def probability(centers):
    # Finite support at ten kernel sigmas; omitted mass is below floating-point precision.
    lo=math.floor(min(x-10*h for x,h,w in centers)-.5)
    hi=math.ceil(max(x+10*h for x,h,w in centers)+.5)
    def cdf(z):return .5*math.erfc(-z/math.sqrt(2))
    p={k:math.fsum(w*max(0,cdf((k+.5-x)/h)-cdf((k-.5-x)/h)) for x,h,w in centers) for k in range(lo,hi+1)}
    total=math.fsum(p.values())
    return {k:v/total for k,v in p.items()}

def observed_floor(p,b):
    if b is None:return dict(p)
    out={k:v for k,v in p.items() if k>b}
    out[b]=math.fsum(v for k,v in p.items() if k<=b)
    return dict(sorted(out.items()))

def percentages(p):
    p={k:Decimal(str(v)) for k,v in p.items()};total=sum(p.values())
    scaled={k:v/total*10000 for k,v in p.items()};units={k:int(v) for k,v in scaled.items()}
    order=sorted(p,key=lambda k:(-(scaled[k]-units[k]),-p[k],k))
    for k in order[:10000-sum(units.values())]:units[k]+=1
    return {k:f'{v/100:.2f}' for k,v in units.items() if v}

def peak_window(weighted_hours):
    # Linear target-day timeline: 23:00 is never joined to 00:00 of the SAME day.
    start=max(range(22),key=lambda a:(math.fsum(w for h,w in weighted_hours if a<=h<a+3),-a))
    return f'{start:02d}:00–{start+3:02d}:00 TRT'

def evaluate(snapshot,offset):
    warnings=set(snapshot.get('warnings',[]));asof=stamp(snapshot['reference_time'])
    decision=stamp(snapshot.get('decision_time',snapshot['reference_time']))
    day=dt.date.fromisoformat(snapshot['target_day'])+dt.timedelta(days=offset)
    start=dt.datetime.combine(day,dt.time(),TZ);end=start+24*HOUR
    obs=normalize_obs(snapshot.get('observations',[]),asof)
    todays=[(t,x) for t,x in obs if start<=t<end]
    floor=max((x for t,x in todays),default=None) if offset==0 else None
    if not obs:warnings.add('Canlı NOAA gözlemi yok; gözlem düzeltmesi uygulanmadı.')
    elif (asof-obs[-1][0])>2*HOUR:warnings.add('Son NOAA gözlemi iki saatten eski.')
    if offset==0 and floor is None:warnings.add('Bugünün gerçekleşmiş maksimumu doğrulanamadı.')
    if offset==0 and todays:
        obs_times=[t for t,x in todays]
        if obs_times[0]>start+HOUR or any(b-a>HOUR for a,b in zip(obs_times,obs_times[1:])):
            warnings.add('NOAA gözlem dizisinde boşluk var; görülmemiş ara zirve riski mevcut.')
    ds,es=load_series(snapshot.get('deterministic',{}),snapshot.get('ensemble',{}),warnings,decision)
    missing_run_times=sorted({s.name for s in ds+es if s.run_time is None})
    if missing_run_times:
        warnings.add('Bazı model koşu saatleri doğrulanamıyor: '+', '.join(missing_run_times))
    requested=[start+i*HOUR for i in range(24) if offset or start+i*HOUR>asof]
    if offset==0 and start<=asof<end:requested=sorted(set([asof]+requested))
    family_results=[];live={};seen_audit={}
    for family in sorted({s.family for s in ds+es}):
        rows=live_rows(family,ds,es,obs,asof);live[family]=rows
        candidates=[s for s in es if s.family==family]
        if not candidates:
            d=sorted((s for s in ds if s.family==family),
                     key=lambda s:(0 if 'ensemble_mean' in s.name else 1,s.name))
            candidates=d[:1]
        accepted=[]
        for s in candidates:
            times=list(requested)
            # Include half-hour samples only when bracketed by real model values.
            times+= [start+dt.timedelta(minutes=30*i) for i in range(48) if (offset or start+dt.timedelta(minutes=30*i)>asof) and s.value('temperature_2m',start+dt.timedelta(minutes=30*i)) is not None]
            times=sorted(set(times))
            raw=[s.value('temperature_2m',t) for t in times]
            if any(x is None for x in raw) or not raw:continue
            missing=sum(s.value('temperature_2m',t,False) is None for t in requested if t.minute==0)
            if missing>2:continue
            if s.value('temperature_2m',end) is None:warnings.add('Son günün 23:00 sonrası sıcaklık eğrisi tam kapsanmıyor.')
            adjusted=[]
            for t,temp in zip(times,raw):
                f=features(s,ds,t);c,n=correction(f,rows,t)
                adjusted.append((q(temp+c),t,c,n,f))
            best=max(adjusted,key=lambda r:(r[0],-r[1].timestamp()))
            accepted.append({'model':s.name,'member':s.member,'max':best[0],
                            'time':best[1].isoformat(),'live_correction':best[2],
                            'matched_hours':best[3],'features':best[4],
                            'run_time':s.run_time.isoformat() if s.run_time else None})
            seen_audit[s.key]=dict(sorted(s.audit.items()))
        if len(accepted)<math.ceil(.8*len(candidates)):
            warnings.add('Yetersiz sıcaklık kapsamı nedeniyle aile kullanılmadı: '+family);continue
        if not accepted:continue
        xs=[a['max'] for a in accepted];h=raw_bandwidth(xs)
        # Recent dispersion is a live diagnostic, never called a historical skill estimate.
        errs=[r[1] for r in rows]
        residual_scale=0.
        if len(errs)>=3:
            mid=median(errs);residual_scale=q(1.4826*median([abs(e-mid) for e in errs]))
        h=q(math.sqrt(h*h+residual_scale*residual_scale))
        family_results.append({'family':family,'members':accepted,'bandwidth':h,
                               'recent_residual_scale':residual_scale,
                               'median_max':median(xs),'min_max':min(xs),'max_max':max(xs)})
    if not family_results:
        if offset==0 and asof>=end and floor is not None:p={floor:1.};centers=[];hours=[(t.hour+t.minute/60,1/len(todays)) for t,x in todays if x==floor]
        else:return {'date':day.isoformat(),'status':'unavailable','message':'Kaynaklara erişilemedi veya hedef gün kapsaması yetersiz; yeni tahmin hesaplanamadı.','warnings':sorted(warnings)}
    else:
        centers=[];hours=[]
        observed_time=next((t for t,x in todays if x==floor),None)
        for f in family_results:
            weight=1/len(family_results)/len(f['members'])
            for m in f['members']:
                centers.append((m['max'],f['bandwidth'],weight))
                t=stamp(m['time'])
                if offset==0 and observed_time is not None:
                    # Kernel probability of the maximum staying at the observed class.
                    stay=.5*math.erfc(-(floor+.5-m['max'])/f['bandwidth']/math.sqrt(2))
                    hours.append((observed_time.hour+observed_time.minute/60,weight*stay))
                    hours.append((t.hour+t.minute/60,weight*(1-stay)))
                else:hours.append((t.hour+t.minute/60,weight))
        p=observed_floor(probability(centers),floor)
    med=wmedian([(max(x,floor) if floor is not None else x,w) for x,h,w in centers]) if centers else floor
    mode=min(p,key=lambda k:(-round(p[k],12),abs(k-med),k))
    shown=percentages(p)
    warnings.add('Olasılıklar geçmiş LTFM sonuçlarıyla kalibre edilmemiştir.')
    return {'date':day.isoformat(),'status':'ok','main_c':mode,'percentages':shown,
            'raw_probabilities':{str(k):round(v,12) for k,v in p.items()},'peak_window':peak_window(hours),
            'observed_max':floor,'families':family_results,'warnings':sorted(warnings),
            'feature_audit':seen_audit,'calibration':'UNFITTED; additive physics correction disabled',
            'live_matching':'same daylight + normalized surface/airmass/boundary similarity + circular wind'}

def execute(snapshot):
    output={'engine_version':VERSION,'input_sha256':digest(snapshot),'reference_time':snapshot['reference_time'],
            'calibration_version':'UNFITTED-REGIME-LIVE-ROBUST-KDE-v2','days':[evaluate(snapshot,0),evaluate(snapshot,1)]}
    output['result_sha256']=digest(output)
    return output

def render(result):
    months=['Ocak','Şubat','Mart','Nisan','Mayıs','Haziran','Temmuz','Ağustos','Eylül','Ekim','Kasım','Aralık']
    sections=[]
    for i,d in enumerate(result['days']):
        date=dt.date.fromisoformat(d['date']);label='Bugün' if i==0 else 'Yarın'
        lines=[f"### İstanbul Havalimanı (LTFM) — {date.day:02d} {months[date.month-1]} {date.year} ({label})",'']
        if d['status']!='ok':
            lines += [d['message']];sections.append('\n'.join(lines));continue
        lines += [f"**🌡️ Ana tahmin: {d['main_c']}°C**",'', '**Olasılıklar**','']
        probs={int(k):Decimal(v) for k,v in d['percentages'].items()}
        selected=sorted(probs,key=lambda k:(-probs[k],k))[:3]
        for k in selected:lines.append(f'- **{k}°C — %{probs[k]:.2f}**')
        other=Decimal(100)-sum(probs[k] for k in selected)
        if other:lines.append(f'- Diğer dereceler — %{other:.2f}')
        lines += ['',f"**⏰ En sıcak saatler:** {d['peak_window']}",'','**Neden?**']
        lines.append(f"Kullanılabilir {len(d['families'])} model ailesinin saatlik senaryoları birlikte değerlendirildi; en yüksek olasılık {d['main_c']}°C'de toplandı.")
        if i==0 and d['observed_max'] is not None:
            lines.append(f"NOAA'da bugün {d['observed_max']}°C görüldü; günün kalan saatleri bu gerçekleşmiş maksimumla birleştirildi.")
        elif any(m['matched_hours']>=3 for f in d['families'] for m in f['members']):
            lines.append('Canlı sıcaklık sapması, gündüz/gece ve hava koşulları benzer olan saatlere sabit kuralla uygulandı.')
        else:lines.append('Uygun canlı gözlem eşleşmesi olmayan saatlerde ek sıcaklık düzeltmesi yapılmadı.')
        summaries=[]
        for f in d['families']:
            summaries.append(f"{f['family']}: {f['min_max']}–{f['max_max']}°C "
                            f"(medyan {f['median_max']}°C, {len(f['members'])} senaryo)")
        if summaries:lines.append('Model aileleri: '+'; '.join(summaries)+'.')
        neighbors=sorted((k for k in probs if abs(k-d['main_c'])==1),key=lambda k:(-probs[k],k))
        if neighbors:
            k=neighbors[0];risk=f"Gerçek sıcaklık zirvesinin {k}°C sınıfına taşınması; bu sınıfın hesaplanan olasılığı %{probs[k]:.2f}."
        else:risk='Saatler arasındaki kısa sıcaklık zirveleri ve eksik gözlemler.'
        lines += ['',f'**⚠️ En büyük risk:** {risk}']
        visible=[]
        if any('NOAA' in w for w in d['warnings']):
            visible.extend(w for w in d['warnings'] if 'NOAA' in w and 'HTTP' not in w and 'doğrulanamadı:' not in w)
        if any('23:00 sonrası' in w for w in d['warnings']):visible.append('Son günün son saatinde model kapsamı eksik.')
        if visible:lines += ['','**Veri durumu:** '+' '.join(dict.fromkeys(visible))]
        sections.append('\n'.join(lines))
    return '\n\n---\n\n'.join(sections)+'\n\n*Olasılıklar, geçmiş LTFM sonuçlarıyla henüz kalibre edilmemiş model tahminleridir.*\n'

def fetch(url,attempts=3):
    headers={'User-Agent':'LTFM-forecast-engine/2.1'}
    if urllib.parse.urlparse(url).hostname=='api.synopticdata.com':
        # This is the public feed used by the NOAA page, with its normal page origin.
        headers.update({'Referer':'https://www.weather.gov/','Origin':'https://www.weather.gov','User-Agent':'Mozilla/5.0'})
    request=urllib.request.Request(url,headers=headers)
    last=None
    for _ in range(max(1,attempts)):
        try:
            with urllib.request.urlopen(request,timeout=30) as r:return r.read()
        except Exception as e:
            last=e
    raise last

def collect():
    started=dt.datetime.now(TZ).replace(microsecond=0)
    snapshot={'reference_time':started.isoformat(),'decision_time':started.isoformat(),
              'target_day':started.date().isoformat(),
              'deterministic':{},'ensemble':{},'observations':[],'warnings':[],'source_hashes':{}}
    for name in ('deterministic','ensemble'):
        try:
            b=fetch(BASE+name+'.json');snapshot[name]=json.loads(b)
            snapshot['source_hashes'][name]=hashlib.sha256(b).hexdigest()
        except Exception as e:snapshot['warnings'].append(name+' kaynağı okunamadı: '+type(e).__name__)
    try:
        page=fetch(NOAA).decode();script=fetch('https://www.weather.gov/source/wrh/timeseries/obs.js?v202601121730').decode()
        # Verify the metric table still uses the same field and rounding rule.
        if not re.search(r'Math\.round\s*\(\s*DATA\.STATION\[0\]\.OBSERVATIONS\.air_temp_set_1\s*\[\s*j\s*\]\s*\)',script):
            raise ValueError('NOAA table schema changed')
        if '/source/wrh/apiKey.js' not in page or 'https://api.synopticdata.com/v2/stations/timeseries?' not in script:
            raise ValueError('NOAA data endpoint changed')
        key_js=fetch('https://www.weather.gov/source/wrh/apiKey.js').decode()
        token=re.search(r'mesoToken\s*=\s*[\"\x27]([^\"\x27]+)',key_js).group(1)
        params={'STID':'LTFM','showemptystations':1,'recent':4320,'complete':1,'token':token,'obtimezone':'local'}
        b=fetch('https://api.synopticdata.com/v2/stations/timeseries?'+urllib.parse.urlencode(params));j=json.loads(b)
        snapshot['source_hashes']['noaa_data']=hashlib.sha256(b).hexdigest()
        if j.get('SUMMARY',{}).get('RESPONSE_CODE')!=1:raise ValueError('NOAA upstream response')
        if j.get('UNITS',{}).get('air_temp') not in ('Celsius','C','celsius'):raise ValueError('NOAA unit')
        stations=[s for s in j.get('STATION',[]) if s.get('STID')=='LTFM']
        if len(stations)!=1:raise ValueError('NOAA station')
        obs=stations[0]['OBSERVATIONS']
        for t,x in zip(obs.get('date_time',[]),obs.get('air_temp_set_1',[])):
            if valid(x) and stamp(t)<=started:snapshot['observations'].append({'station':'LTFM','time':t,'temp_c':x,'valid':True})
    except Exception as e:snapshot['warnings'].append('NOAA kaynağı doğrulanamadı: '+type(e).__name__)
    # Freeze the observation cutoff separately from collection completion.
    # With unavailable current-day observations, D0 is explicitly model-only from midnight.
    current=[stamp(r['time']) for r in snapshot['observations'] if stamp(r['time']).date()==started.date()]
    snapshot['reference_time']=(max(current) if current else started.replace(hour=0,minute=0,second=0)).isoformat()
    if current and started-max(current)>2*HOUR:snapshot['warnings'].append('Canlı NOAA gözlemi iki saatten eski; veri gecikmesi var.')
    snapshot['decision_time']=dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    return snapshot

def main():
    p=argparse.ArgumentParser();p.add_argument('--fetch',action='store_true');p.add_argument('--replay');p.add_argument('--out',default='ltfm-run');a=p.parse_args()
    if a.fetch==bool(a.replay):p.error('choose --fetch OR --replay')
    out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
    snapshot=collect() if a.fetch else json.loads(pathlib.Path(a.replay).read_text())
    (out/'snapshot.json').write_text(canonical(snapshot),encoding='utf-8')
    result=execute(snapshot)
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'forecast.md').write_text(render(result),encoding='utf-8')
    compact={k:v for k,v in result.items() if k!='days'}
    compact['days']=[{k:v for k,v in d.items() if k not in ('families','feature_audit','raw_probabilities')} for d in result['days']]
    print(json.dumps(compact,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
