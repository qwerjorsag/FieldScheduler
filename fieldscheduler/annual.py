"""Annual task expansion and deterministic resource-constrained list scheduling.

This is a bounded heuristic, not a proof of global optimality. Unschedulable
occurrences stay in the output with reasons. Agronomic conditions remain gates.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from uuid import uuid5, NAMESPACE_URL
from heapq import heappop, heappush
from itertools import combinations
from zoneinfo import ZoneInfo
from fieldscheduler.recurrence import expand_recurrence


def ident(key):
    return str(uuid5(NAMESPACE_URL,'annual-job/'+key))


def minute(value):
    h,m=map(int,value.split(':'))
    if not (0<=h<=24 and 0<=m<60 and (h!=24 or m==0)):
        raise ValueError('INVALID_LOCAL_TIME:'+value)
    return h*60+m


def instant(day, minutes, zone):
    return (datetime.combine(day,time.min,zone)+timedelta(minutes=minutes)).astimezone(timezone.utc)


def validate_inputs(data, settings):
    locations=data['farm_locations']['locations']; crops=data['crops']['crops']
    if len({l['id'] for l in locations})!=len(locations): raise ValueError('DUPLICATE_LOCATION')
    if settings['horizon_days']<1 or settings['slot_minutes'] not in (15,30,60): raise ValueError('INVALID_HORIZON')
    policy=settings.get('planning_policy',{})
    if not 0<=policy.get('day_start_hour',4)<policy.get('quiet_start_hour',22)<24 or policy.get('day_end_hour',24)!=24:
        raise ValueError('INVALID_PLANNING_POLICY')
    if not 0<policy.get('max_daily_minutes',720)<=720:raise ValueError('INVALID_DAILY_CAP')
    bpej={x['code']:x for x in data['bpej_catalog']['entries']}
    for collection in (crops,data['activities']['activities'],data['workers']['workers'],data['machines']['machines']):
        if len({x['id'] for x in collection})!=len(collection):raise ValueError('DUPLICATE_ENTITY_ID')
    for l in locations:
        if l['area_ha'] is not None and l['area_ha']<=0: raise ValueError('INVALID_AREA')
        if l.get('animal_count') is not None:
            if type(l['animal_count']) is not int or l['animal_count']<0 or (l.get('capacity_units') is not None and l['animal_count']>l['capacity_units']): raise ValueError('INVALID_ANIMAL_COUNT:'+l['id'])
            if sum(g['count'] for g in l.get('animal_groups',[]))!=l['animal_count']: raise ValueError('ANIMAL_GROUP_BALANCE:'+l['id'])
        if l['type']=='CATTLE':
            mode=l.get('milking_mode')
            staffing=l.get('milking_staffing') or {}
            if mode not in ('ROBOTIC','MANUAL'): raise ValueError('INVALID_MILKING_MODE:'+l['id'])
            if staffing.get('required_staff_count') != (1 if mode=='ROBOTIC' else 2) or staffing.get('required_skill')!='MILKING':
                raise ValueError('INVALID_MILKING_STAFFING:'+l['id'])
        if l['bpej']:
            if abs(sum(p['area_ha'] for p in l['bpej'])-l['area_ha'])>0.0001: raise ValueError('BPEJ_AREA_BALANCE')
            if any(p['code'] not in bpej or len(p['code'])!=5 or not p['code'].isdigit() for p in l['bpej']): raise ValueError('UNKNOWN_BPEJ')
    ids={l['id'] for l in locations}; cropids={c['id'] for c in crops}
    assignments=data['crop_assignments']['assignments']
    if len({(a['location_id'],a['harvest_year']) for a in assignments})!=len(assignments): raise ValueError('DUPLICATE_CROP_CYCLE')
    for a in assignments:
        if a['location_id'] not in ids or a['crop_id'] not in cropids: raise ValueError('UNKNOWN_CROP_OR_FIELD')
        if next(l for l in locations if l['id']==a['location_id'])['type']!='FIELD':raise ValueError('CROP_ASSIGNED_TO_NONFIELD')
    start=date.fromisoformat(settings['starting_date']);finish=start+timedelta(days=settings['horizon_days']-1)
    for year in range(start.year,finish.year+1):
        if max(start,date(year,3,1))<=min(finish,date(year,11,15)):
            for loc in locations:
                if loc['type']=='FIELD' and not any(a['location_id']==loc['id'] and a['harvest_year']==year for a in assignments):
                    raise ValueError(f'MISSING_CROP_ASSIGNMENT:{loc["id"]}:{year}')
    for activity in data['activities']['activities']:
        if activity['duration_minutes']<=0 or activity['window_minutes']<=0 or activity.get('date_flex_days',0)<0:raise ValueError('INVALID_ACTIVITY_DURATION')
        if type(activity['is_non_disruptive']) is not bool:raise ValueError('NOISE_MUST_BE_BOOLEAN')
        for t in activity['times']:minute(t)
        if any(float(v)<0 for v in activity.get('animal_time_minutes_by_location_type',{}).values()):raise ValueError('INVALID_ANIMAL_TIME:'+activity['id'])
        if any(float(v)<0 for v in activity.get('animal_time_minutes_by_milking_mode',{}).values()):raise ValueError('INVALID_ANIMAL_TIME:'+activity['id'])
    for w in data['workers']['workers']:
        if not 0<w['max_daily_minutes']<=720 or w['max_weekly_minutes']<=0: raise ValueError('INVALID_WORKER_LIMIT')
        if not set(w['allowed_location_ids'])<=ids: raise ValueError('UNKNOWN_PERMISSION_LOCATION')
        if w.get('hourly_wage_czk',0)<0 or w.get('employment_type','PERMANENT') not in ('PERMANENT','TEMPORARY'):raise ValueError('INVALID_WORKER_COST:'+w['id'])
        for item in w.get('unavailable',[]):
            if date.fromisoformat(item['start_date'])>date.fromisoformat(item['end_date']):raise ValueError('INVALID_ABSENCE:'+w['id'])
    for m in data['machines']['machines']:
        if m.get('operating_cost_czk_per_hour',0)<0:raise ValueError('INVALID_MACHINE_COST:'+m['id'])
        if not 0<float(m['ha_per_hour'])<10000 or m['setup_minutes']<0: raise ValueError('INVALID_PRODUCTIVITY')
        if type(m['is_non_disruptive']) is not bool:raise ValueError('NOISE_MUST_BE_BOOLEAN')


def expand_jobs(data, settings):
    validate_inputs(data,settings)
    start=date.fromisoformat(settings['starting_date']); end=start+timedelta(days=settings['horizon_days']-1)
    slot=settings['slot_minutes']
    locations={l['id']:l for l in data['farm_locations']['locations']}
    crops={c['id']:c for c in data['crops']['crops']}
    tasks=[]; external=[]; untriggered=[]
    series=defaultdict(list)
    # Expand a lookback too, to recognize predecessors just before the horizon.
    for a in data['activities']['activities']:
        if not a.get('enabled',True): continue
        if a['recurrence']['frequency']=='EVENT' and not a['recurrence'].get('dates'):
            untriggered.append({'activity_id':a['id'],'reason':'WAITING_FOR_EVENT','requires':a['requires']})
        dates=list(expand_recurrence(a['recurrence'],start-timedelta(days=370),end,a.get('excluded_dates',[])))
        for loc in locations.values():
            if loc['type'] not in a['location_types']: continue
            for day in dates:
                for occurrence_time in a['times']:
                    uid=ident(f'{a["id"]}/{loc["id"]}/{day}/{occurrence_time}')
                    animal_count=loc.get('animal_count') if loc['type'] in ('CATTLE','PIG','POULTRY','SHEEP','GOAT','STABLE','MILKING') else None
                    mode=loc.get('milking_mode')
                    per_animal=a.get('animal_time_minutes_by_milking_mode',{}).get(mode,a.get('animal_time_minutes_by_location_type',{}).get(loc['type'],a.get('animal_time_minutes_per_animal',0)))
                    required_staff=a.get('required_staff_by_milking_mode',{}).get(mode,a.get('required_staff_count',1))
                    fixed=a.get('fixed_setup_minutes',a['duration_minutes'])
                    total_duration=fixed+(animal_count or 0)*per_animal
                    if total_duration<=0: raise ValueError('INVALID_ANIMAL_TASK_DURATION:'+a['id'])
                    t=dict(id=uid,location_id=loc['id'],activity_id=a['id'],name=a['name'],crop_id=None,cycle_id=None,
                        nominal_date=day.isoformat(),earliest_date=day.isoformat(),latest_date=(day+timedelta(days=a.get('date_flex_days',0))).isoformat(),
                        window_start=minute(occurrence_time),window_end=min(1440,minute(occurrence_time)+a['window_minutes']),
                        duration_minutes=ceil(total_duration/slot)*slot,base_duration_minutes=fixed,animal_count=animal_count,animal_time_minutes_per_animal=per_animal,required_staff_count=required_staff,staffing_mode=mode,skill=a['skill'],machine_operation=None,
                        is_non_disruptive=a['is_non_disruptive'],priority=a['priority'],requires=a['requires'],method=a['method'],
                        dependencies=[],minimum_gap_minutes=a.get('minimum_gap_minutes',0),quantity=1,unit='visit',part=1,parts=1,
                        parent_activity=a.get('depends_on'),source='recurrence')
                    series[a['id'],loc['id']].append(t)
    for values in series.values():
        for t in values:
            day=date.fromisoformat(t['nominal_date'])
            if not start<=day<=end: continue
            if t['parent_activity']:
                parents=[p for p in series.get((t['parent_activity'],t['location_id']),[]) if (p['nominal_date'],p['window_start'])<=(t['nominal_date'],t['window_start'])]
                if parents:
                    parent=max(parents,key=lambda p:(p['nominal_date'],p['window_start']))
                    if parent['nominal_date']>=start.isoformat(): t['dependencies']=[parent['id']]
                    else:
                        t['requires']=t['requires']+['confirm_pre_horizon_predecessor']
                        external.append({'task_id':t['id'],'predecessor':parent['id'],'assumed_date':parent['nominal_date']})
                else: t['blocked_reason']='MISSING_PREDECESSOR_TEMPLATE'
            tasks.append(t)
    for assignment in data['crop_assignments']['assignments']:
        loc=locations[assignment['location_id']]; crop=crops[assignment['crop_id']]; stage_last={}; before={}
        for st in crop['stages']:
            year=assignment['harvest_year']+st['offset']
            lo=date.fromisoformat(f'{year}-{st["start"]}'); hi=date.fromisoformat(f'{year}-{st["end"]}')
            if hi<lo: hi=hi.replace(year=year+1)
            if hi<start:
                before[st['id']]=hi
                continue
            if lo>end: continue
            recurrence=dict(st['recurrence']); recurrence.setdefault('anchor_date','2026-01-01')
            occurrence_dates=list(expand_recurrence(recurrence,lo,hi,st.get('excluded_dates',[]))) if st.get('enabled',True) else []
            for occurrence in occurrence_dates:
                earliest=max(start,occurrence); latest=min(end,hi)
                machines=[m for m in data['machines']['machines'] if st['activity'] in m['operation_types']]
                if not machines: raise ValueError('NO_MACHINE_TYPE:'+st['activity'])
                rate=min(m['ha_per_hour'] for m in machines)*loc['productivity_factor']
                setup=max(m['setup_minutes'] for m in machines)
                parts=ceil(loc['area_ha']/12); quantity=loc['area_ha']/parts
                parent=st['after']; deps=[]; gates=list(st['requires'])
                gap_days=st['min_gap_days']
                if st['activity']=='FERTILIZE':
                    gates=list(dict.fromkeys(gates+data['fertilization_policy']['required_checks']))
                    if parent and parent.startswith('nutrition'):
                        gap_days=max(gap_days,data['fertilization_policy']['minimum_split_gap_days'])
                blocked=None
                if parent:
                    if parent in stage_last: deps=[stage_last[parent]]
                    elif parent in before:
                        earliest=max(earliest,before[parent]+timedelta(days=gap_days))
                        gates.append('confirm_pre_horizon_establishment')
                    else: blocked='MISSING_CROP_STAGE:'+parent
                for additional in st.get('additional_after',[]):
                    if additional in stage_last:deps.append(stage_last[additional])
                    elif additional in before:gates.append('confirm_pre_horizon_establishment')
                    else:blocked='MISSING_CROP_STAGE:'+additional
                last=None
                for part in range(1,parts+1):
                    uid=ident(f'{assignment["id"]}/{st["id"]}/{occurrence}/{part}')
                    task=dict(id=uid,location_id=loc['id'],activity_id=st['activity'],name=st['name'],crop_id=crop['id'],cycle_id=assignment['id'],
                        nominal_date=occurrence.isoformat(),earliest_date=earliest.isoformat(),latest_date=latest.isoformat(),window_start=360,window_end=1080,
                        duration_minutes=ceil((quantity/rate*60+setup)/slot)*slot,skill=st['activity'],machine_operation=st['activity'],
                        is_non_disruptive=False,priority=2,requires=gates,method=f'{crop["name"]}: {st["name"]}; ověřit fenologii a počasí.',
                        dependencies=[last] if last else deps,minimum_gap_minutes=0 if last else gap_days*1440,
                        quantity=quantity,unit='ha',part=part,parts=parts,source='crop_stage',effective_ha_per_hour=rate)
                    if blocked: task['blocked_reason']=blocked
                    tasks.append(task);last=uid
                stage_last[st['id']]=last
                if parent in before:
                    external.append({'task_id':last,'assumed_stage':parent,'assumed_completed_by':before[parent].isoformat(),'cycle_id':assignment['id']})
    # A new crop cycle cannot start soil preparation before the previous crop is harvested.
    cycles=defaultdict(list)
    for a in data['crop_assignments']['assignments']: cycles[a['location_id']].append(a)
    for assignments in cycles.values():
        ordered=sorted(assignments,key=lambda a:a['harvest_year'])
        for previous,current in zip(ordered,ordered[1:]):
            harvests=[t for t in tasks if t['cycle_id']==previous['id'] and t['activity_id']=='HARVEST']
            preparations=[t for t in tasks if t['cycle_id']==current['id'] and t['activity_id']=='PREPARE' and t['part']==1]
            if harvests:
                last=max(harvests,key=lambda t:(t['nominal_date'],t['part']))
                for t in preparations: t['dependencies'].append(last['id'])
    return tasks,external,untriggered


def overlaps(a,b,c,d): return a<d and c<b


def worker_can_fit(w,day,s,e,lid,bookings,day_minutes,week_minutes):
    wid=w['id']; minutes=(e-s).total_seconds()/60
    if any(date.fromisoformat(item['start_date'])<=day<=date.fromisoformat(item['end_date']) for item in w.get('unavailable',[])): return False
    if day_minutes[wid,day]+minutes>w['max_daily_minutes'] or week_minutes[wid,day.isocalendar()[:2]]+minutes>w['max_weekly_minutes']: return False
    items=bookings.get((wid,day),[])
    combined=sorted(items+[(s,e,lid)])
    if (combined[-1][1]-combined[0][0]).total_seconds()/60>780: return False
    block_start=combined[0][0]
    for i,(a,b,place) in enumerate(combined):
        if i:
            pa,pb,pl=combined[i-1]; gap=(a-pb).total_seconds()/60
            transfer=w['field_change_minutes'] if place!=pl else 0
            if gap<transfer: return False
            if gap>=w['break_minutes']+transfer: block_start=a
        if (b-block_start).total_seconds()/60>w['max_continuous_minutes']: return False
    previous=bookings.get((wid,day-timedelta(days=1)),[])
    following=bookings.get((wid,day+timedelta(days=1)),[])
    if previous and (combined[0][0]-max(x[1] for x in previous)).total_seconds()/60<w['min_rest_minutes']: return False
    if following and (min(x[0] for x in following)-combined[-1][1]).total_seconds()/60<w['min_rest_minutes']: return False
    return True


def schedule_annual(tasks,data,settings,excluded_worker_ids=frozenset()):
    workers=[w for w in data['workers']['workers'] if w['active'] and w['id'] not in excluded_worker_ids]; machines=data['machines']['machines']
    policy=settings.get('planning_policy',{})
    opening=policy.get('day_start_hour',4)*60;quiet_after=policy.get('quiet_start_hour',22)*60
    zone=ZoneInfo(settings['timezone']); slot=settings['slot_minutes']; origin=date.fromisoformat(settings['starting_date'])
    final=origin+timedelta(days=settings['horizon_days']-1)
    bookings=defaultdict(list); places=defaultdict(list); equipment=defaultdict(list)
    used_workers=set();used_machines=set()
    day_minutes=defaultdict(int); week_minutes=defaultdict(int); results={}; jobs={t['id']:t for t in tasks}
    dependents=defaultdict(list); indegree={t['id']:len(t['dependencies']) for t in tasks}
    ready=[]
    def push(t): heappush(ready,(t['priority'],t['earliest_date'],t['window_end']-t['window_start']-t['duration_minutes'],t['latest_date'],t['id']))
    for t in tasks:
        for p in t['dependencies']:
            if p not in jobs: raise ValueError('UNKNOWN_DEPENDENCY:'+p)
            dependents[p].append(t['id'])
        if not indegree[t['id']]: push(t)
    while ready:
        _,_,_,_,tid=heappop(ready);t=jobs[tid]; reason=t.get('blocked_reason'); candidate=None
        if any(results[p]['status']=='UNSCHEDULED' for p in t['dependencies']): reason='PREDECESSOR_UNSCHEDULED'
        eligible=[w for w in workers if t['skill'] in w['skills'] and t['location_id'] in w['allowed_location_ids']]
        required_staff=t.get('required_staff_count',1)
        resources=[m for m in machines if t['machine_operation'] in m['operation_types']] if t['machine_operation'] else [None]
        if not eligible: reason='NO_AUTHORIZED_WORKER'
        if not resources: reason='NO_COMPATIBLE_MACHINE'
        release=max((datetime.fromisoformat(results[p]['end_at'])+timedelta(minutes=t['minimum_gap_minutes']) for p in t['dependencies'] if results[p]['status']!='UNSCHEDULED'),default=None)
        earliest=max(origin,date.fromisoformat(t['earliest_date']));latest=min(final,date.fromisoformat(t['latest_date']))
        if release: earliest=max(earliest,release.astimezone(zone).date())
        if not reason:
            for offset in range(max(0,(latest-earliest).days+1)):
                day=earliest+timedelta(days=offset);best=None
                for team in combinations(eligible,required_staff):
                    lo=max([opening,t['window_start'],*(minute(w['shift'][0]) for w in team)])
                    hi=min([1440,t['window_end'],*(minute(w['shift'][1]) for w in team)])
                    if not t['is_non_disruptive']:hi=min(hi,quiet_after)
                    if any(day_minutes[w['id'],day]+t['duration_minutes']>policy.get('max_daily_minutes',720) for w in team):continue
                    for local in range(ceil(lo/slot)*slot,hi-t['duration_minutes']+1,slot):
                        s=instant(day,local,zone);e=s+timedelta(minutes=t['duration_minutes'])
                        if release and s<release: continue
                        if any(overlaps(s,e,a,b) for a,b in places[t['location_id'],day]):continue
                        if not all(worker_can_fit(w,day,s,e,t['location_id'],bookings,day_minutes,week_minutes) for w in team):continue
                        available_machines=[m for m in resources if m is None or
                                            ((local+t['duration_minutes']<=quiet_after or m['is_non_disruptive']) and
                                             not any(overlaps(s,e,a,b) for a,b in equipment[m['id'],day]))]
                        if not available_machines: continue
                        m=min(available_machines,key=lambda machine:(machine['id'] not in used_machines,
                              machine.get('operating_cost_czk_per_hour',0),machine['id']) if machine else (0,0,''))
                        same=sum(any(l==t['location_id'] for _,_,l in bookings[w['id'],day]) for w in team)
                        hourly=sum(w.get('hourly_wage_czk',0) for w in team)+(m.get('operating_cost_czk_per_hour',0) if m else 0)
                        score=(sum(w.get('employment_type')=='TEMPORARY' for w in team),
                               sum(w['id'] not in used_workers for w in team),
                               sum(not bookings[w['id'],day] for w in team),
                               int(hourly*t['duration_minutes']/60),required_staff-same,local,
                               tuple(w['id'] for w in team))
                        if best is None or score<best[0]:best=(score,team,m,s,e,day)
                        break
                if best:
                    candidate=best;break
        if candidate:
            _,team,m,s,e,day=candidate
            for w in team:bookings[w['id'],day].append((s,e,t['location_id']))
            used_workers.update(w['id'] for w in team)
            places[t['location_id'],day].append((s,e))
            if m:
                equipment[m['id'],day].append((s,e));used_machines.add(m['id'])
            for w in team:
                day_minutes[w['id'],day]+=t['duration_minutes'];week_minutes[w['id'],day.isocalendar()[:2]]+=t['duration_minutes']
            results[tid]={**t,'status':'CONDITIONAL' if t['requires'] else 'PLANNED','worker_id':team[0]['id'],'worker_ids':[w['id'] for w in team],'worker_name':', '.join(w['name'] for w in team),
                          'machine_id':m['id'] if m else None,'start_at':s.astimezone(zone).isoformat(),'end_at':e.astimezone(zone).isoformat()}
        else: results[tid]={**t,'status':'UNSCHEDULED','reason':reason or 'NO_CAPACITY_IN_WINDOW','worker_id':None,'worker_ids':[],'machine_id':None,'start_at':None,'end_at':None}
        for child in dependents[tid]:
            indegree[child]-=1
            if not indegree[child]: push(jobs[child])
    if len(results)!=len(tasks): raise ValueError('DEPENDENCY_CYCLE')
    return list(results.values())


def validate_annual(tasks,rows,data,settings):
    """Independent checks of exported assignments against their original task contracts."""
    issues=[]; workers={w['id']:w for w in data['workers']['workers']}; machines={m['id']:m for m in data['machines']['machines']}
    tasks_by_id={t['id']:t for t in tasks}; byid={r['id']:r for r in rows};groups=defaultdict(list);daily=defaultdict(int);weekly=defaultdict(int)
    if set(byid)!=set(tasks_by_id) or len(rows)!=len(byid):issues.append('OCCURRENCE_COVERAGE')
    zone=ZoneInfo(settings['timezone']);first=date.fromisoformat(settings['starting_date']);last=first+timedelta(days=settings['horizon_days']-1)
    policy=settings.get('planning_policy',{});opening=policy.get('day_start_hour',4)*60;quiet_after=policy.get('quiet_start_hour',22)*60
    worker_day=defaultdict(list)
    for r in rows:
        if r['status']=='UNSCHEDULED':continue
        t=tasks_by_id[r['id']];team=[workers[wid] for wid in r.get('worker_ids',[r['worker_id']])];w=team[0]
        s=datetime.fromisoformat(r['start_at']).astimezone(timezone.utc);e=datetime.fromisoformat(r['end_at']).astimezone(timezone.utc)
        local=s.astimezone(zone);end=e.astimezone(zone);day=local.date();sm=local.hour*60+local.minute;em=1440 if end.date()>day else end.hour*60+end.minute
        if not first<=day<=last or not t['earliest_date']<=day.isoformat()<=t['latest_date']:issues.append('DATE_WINDOW:'+r['id'])
        if end.date()!=day and (end.date()!=day+timedelta(days=1) or end.time()!=time.min):issues.append('CROSS_MIDNIGHT:'+r['id'])
        if sm<max(opening,t['window_start'],minute(w['shift'][0])) or em>min(1440,t['window_end'],minute(w['shift'][1])):issues.append('TIME_WINDOW:'+r['id'])
        if em>quiet_after and (not t['is_non_disruptive'] or r['machine_id'] and not machines[r['machine_id']]['is_non_disruptive']):issues.append('NIGHT_NOISE:'+r['id'])
        if len(team)!=t.get('required_staff_count',1):issues.append('STAFF_COUNT:'+r['id'])
        if any(not member['active'] or t['skill'] not in member['skills'] or t['location_id'] not in member['allowed_location_ids'] for member in team):issues.append('PERMISSION:'+r['id'])
        if any(any(date.fromisoformat(item['start_date'])<=day<=date.fromisoformat(item['end_date']) for item in member.get('unavailable',[])) for member in team):issues.append('UNAVAILABLE:'+r['id'])
        if t['machine_operation'] and (r['machine_id'] not in machines or t['machine_operation'] not in machines[r['machine_id']]['operation_types']):issues.append('MACHINE:'+r['id'])
        actual=(e-s).total_seconds()/60
        if actual!=t['duration_minutes']:issues.append('DURATION:'+r['id'])
        for member in team:
            daily[member['id'],day]+=actual;weekly[member['id'],day.isocalendar()[:2]]+=actual
            worker_day[member['id'],day].append((s,e,t['location_id']))
            groups[('worker',member['id'])].append((s,e,r['id']))
        groups[('location',t['location_id'])].append((s,e,r['id']))
        if r['machine_id']:groups['machine',r['machine_id']].append((s,e,r['id']))
        for dep in t['dependencies']:
            p=byid[dep]
            if p['status']=='UNSCHEDULED' or s<datetime.fromisoformat(p['end_at'])+timedelta(minutes=t['minimum_gap_minutes']):issues.append('DEPENDENCY:'+r['id'])
    for key,items in groups.items():
        ordered=sorted(items)
        for a,b in zip(ordered,ordered[1:]):
            if a[1]>b[0]:issues.append('OVERLAP:'+str(key))
    for (wid,day),mins in daily.items():
        if mins>min(workers[wid]['max_daily_minutes'],policy.get('max_daily_minutes',720)):issues.append('DAILY_LIMIT:'+wid)
    for (wid,week),mins in weekly.items():
        if mins>workers[wid]['max_weekly_minutes']:issues.append('WEEKLY_LIMIT:'+wid)
    for (wid,day),items in worker_day.items():
        ordered=sorted(items);w=workers[wid];block=ordered[0][0]
        if (ordered[-1][1]-block).total_seconds()/60>780:issues.append('DAY_SPAN:'+wid)
        for i,(s,e,lid) in enumerate(ordered):
            if i:
                p=ordered[i-1];gap=(s-p[1]).total_seconds()/60;transfer=w['field_change_minutes'] if p[2]!=lid else 0
                if gap<transfer:issues.append('TRANSFER_BUFFER:'+wid)
                if gap>=w['break_minutes']+transfer:block=s
            if (e-block).total_seconds()/60>w['max_continuous_minutes']:issues.append('CONTINUOUS_WORK:'+wid)
        prev=worker_day.get((wid,day-timedelta(days=1)),[])
        if prev and (ordered[0][0]-max(x[1] for x in prev)).total_seconds()/60<w['min_rest_minutes']:issues.append('REST:'+wid)
    return sorted(set(issues))
