"""Generate a complete year, including recurrence coverage and resource allocation."""
import csv
import json
import sqlite3
import sys
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, time, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fieldscheduler.annual import expand_jobs, schedule_annual, validate_annual
from timeline import render_timeline, render_daily_agenda, render_monthly_worker_agenda


def write_json(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def write_csv(path,rows,columns):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');writer.writeheader()
        for row in rows:
            writer.writerow({k:json.dumps(row.get(k),ensure_ascii=False) if isinstance(row.get(k),(list,dict)) else row.get(k) for k in columns})


def worker_labels(workers):
    counts=Counter(w['name'] for w in workers)
    seen=Counter()
    labels={}
    for worker in workers:
        name=worker['name'];seen[name]+=1
        label=f'{name} {seen[name]}' if counts[name]>1 else name
        slug=re.sub(r'[^a-z0-9]+','-',unicodedata.normalize('NFKD',label).encode('ascii','ignore').decode().lower()).strip('-')
        labels[worker['id']]=(label,f'{slug or "pracovnik"}-{worker["id"][:8]}')
    return labels


def overview(locations,rows,settings,month=None):
    start=date.fromisoformat(settings['starting_date']);end=start+timedelta(days=settings['horizon_days']-1)
    days=[start+timedelta(days=i) for i in range(settings['horizon_days'])]
    if month: days=[d for d in days if d.strftime('%Y-%m')==month]
    columns=[d.isoformat() for d in days] if month else sorted({d.strftime('%Y-%m') for d in days})
    grouped=defaultdict(list)
    for row in rows:
        d=row['start_at'][:10] if row['start_at'] else row['nominal_date']
        key=d if month else d[:7]
        grouped[row['location_id'],key].append(row)
    left=220;colw=44 if month else 112;top=112;rowh=40
    width=left+len(columns)*colw+24;height=top+rowh*len(locations)+54
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">','<rect width="100%" height="100%" fill="#f4f7fb"/>']
    def text(x,y,s,size=12,color='#34445c'):
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" fill="{color}">{escape(str(s))}</text>')
    text(24,38,'Roční plán farmy / '+(month or f'{start} až {end}'),25)
    text(24,65,'Číslo = počet úkolů. Modrá = plán, oranžová = podmíněný zásah, červená = chybějící kapacita.',12)
    text(24,86,'Podrobnosti činností jsou v popisku buňky; přesné časy v týdenních tabulkách a CSV.',12)
    for c,col in enumerate(columns):text(left+c*colw+3,105,col[-2:] if month else col,11)
    for i,l in enumerate(locations):
        y=top+i*rowh
        text(24,y+24,l['name'],13)
        for c,col in enumerate(columns):
            items=grouped[l['id'],col];x=left+c*colw
            color='#eef1f5' if not items else '#cce2f0'
            if any(r['status']=='CONDITIONAL' for r in items):color='#f8d5a3'
            if any(r['status']=='UNSCHEDULED' for r in items):color='#f6b4b4'
            title='\n'.join(f"{r['start_at'] or r['nominal_date']}: {r['name']} | {r.get('worker_name','bez pracovníka')} | {r['status']}" for r in items)
            parts.append(f'<g><title>{escape(title)}</title><rect x="{x}" y="{y+3}" width="{colw-3}" height="{rowh-5}" rx="3" fill="{color}"/>')
            if items:text(x+8,y+25,len(items),13)
            parts.append('</g>')
    text(24,height-22,'Podmíněné zásahy vyžadují ověření počasí, stavu porostu nebo schválení. Jde o vzorovou farmu.',12)
    parts.append('</svg>');return ''.join(parts)


def main():
    data_dir=ROOT/'data'/'annual'
    data={p.stem:json.loads(p.read_text(encoding='utf-8')) for p in data_dir.glob('*.json')}
    settings=json.loads((ROOT/'data'/'starting_date.json').read_text(encoding='utf-8'))
    settings['planning_policy']=json.loads((ROOT/'data'/'planning_policy.json').read_text(encoding='utf-8'))
    tasks,external,events=expand_jobs(data,settings)
    print(f'Expanded {len(tasks)} occurrences; assigning workers and machinery...',flush=True)
    rows=schedule_annual(tasks,data,settings)
    excluded=set();rejected=set()
    for attempt in range(8):
        days_by_worker=defaultdict(set)
        for row in rows:
            if row['start_at']:
                for worker_id in row['worker_ids']:
                    days_by_worker[worker_id].add(row['start_at'][:10])
        short=[worker_id for worker_id,days in days_by_worker.items() if len(days)<=2 and worker_id not in excluded|rejected]
        if not short: break
        worker_id=min(short,key=lambda wid:(len(days_by_worker[wid]),wid))
        proposed=schedule_annual(tasks,data,settings,excluded|{worker_id})
        if sum(r['status']=='UNSCHEDULED' for r in proposed)<=sum(r['status']=='UNSCHEDULED' for r in rows):
            rows=proposed;excluded.add(worker_id)
        else:
            rejected.add(worker_id)
    issues=validate_annual(tasks,rows,data,settings)
    if issues:raise RuntimeError('Annual validation failed: '+str(issues[:20]))
    locations=data['farm_locations']['locations'];loc_by_id={l['id']:l for l in locations}
    workers=data['workers']['workers'];worker_by_id={w['id']:w for w in workers}
    display=worker_labels(workers)
    bpej={b['code']:b for b in data['bpej_catalog']['entries']}
    for l in locations:
        l['bpej_weighted_productivity_points']=round(sum(p['area_ha']*bpej[p['code']]['productivity_points'] for p in l['bpej'])/l['area_ha'],2) if l['bpej'] else None
    crops={c['id']:c for c in data['crops']['crops']};machines={m['id']:m for m in data['machines']['machines']}
    for r in rows:
        r['worker_name']=', '.join(display[wid][0] for wid in r['worker_ids']) if r['worker_ids'] else None
        r['location_name']=loc_by_id[r['location_id']]['name'];r['location_type']=loc_by_id[r['location_id']]['type']
        r['crop_name']=crops[r['crop_id']]['name'] if r['crop_id'] else None
        r['machine_name']=machines[r['machine_id']]['name'] if r['machine_id'] else None
        r['labor_cost_czk']=round(r['duration_minutes']/60*sum(worker_by_id[wid]['hourly_wage_czk'] for wid in r['worker_ids']),2) if r['start_at'] else 0
        r['machine_cost_czk']=round(r['duration_minutes']/60*machines[r['machine_id']]['operating_cost_czk_per_hour'],2) if r['machine_id'] and r['start_at'] else 0
        r['total_cost_czk']=round(r['labor_cost_czk']+r['machine_cost_czk'],2)
        r['bpej_codes']=[x['code'] for x in loc_by_id[r['location_id']]['bpej']]
    rows.sort(key=lambda r:(r['start_at'] or r['nominal_date'],r['location_name'],r['id']))
    run=ROOT/'Schedules'/datetime.now().strftime('annual_%Y%m%d_%H%M%S_%f');run.mkdir(parents=True)
    write_json(run/'input_snapshot.json',{'settings':settings,'data':data})
    write_json(run/'schedule.json',rows)
    cols=['id','status','start_at','end_at','nominal_date','earliest_date','latest_date','location_name','location_type','activity_id','name','crop_name','worker_id','worker_ids','worker_name','required_staff_count','staffing_mode','machine_name','labor_cost_czk','machine_cost_czk','total_cost_czk','animal_count','animal_time_minutes_per_animal','base_duration_minutes','quantity','unit','part','parts','duration_minutes','bpej_codes','requires','method','reason']
    write_csv(run/'schedule.csv',rows,cols)
    used_worker_ids={wid for r in rows if r['start_at'] for wid in r['worker_ids']}
    used_machine_ids={r['machine_id'] for r in rows if r['machine_id']}
    unused=run/'Unused';unused.mkdir()
    write_csv(unused/'workers.csv',[{'id':w['id'],'name':display[w['id']][0],'hourly_wage_czk':w['hourly_wage_czk'],'reason':'NO_ASSIGNMENT'} for w in workers if w['id'] not in used_worker_ids],['id','name','hourly_wage_czk','reason'])
    write_csv(unused/'machines.csv',[{'id':m['id'],'name':m['name'],'operating_cost_czk_per_hour':m['operating_cost_czk_per_hour'],'reason':'NO_ASSIGNMENT'} for m in machines.values() if m['id'] not in used_machine_ids],['id','name','operating_cost_czk_per_hour','reason'])
    write_csv(run/'cost_summary.csv',[{'category':'labor','cost_czk':round(sum(r['labor_cost_czk'] for r in rows),2)},
                                      {'category':'machines','cost_czk':round(sum(r['machine_cost_czk'] for r in rows),2)},
                                      {'category':'total','cost_czk':round(sum(r['total_cost_czk'] for r in rows),2)}],['category','cost_czk'])
    worker_minutes=defaultdict(int);worker_days=defaultdict(set);machine_minutes=defaultdict(int)
    for row in rows:
        if not row['start_at']:continue
        for worker_id in row['worker_ids']:
            worker_minutes[worker_id]+=row['duration_minutes']
            worker_days[worker_id].add(row['start_at'][:10])
        if row['machine_id']:machine_minutes[row['machine_id']]+=row['duration_minutes']
    write_csv(run/'worker_costs.csv',[
        {'worker_id':w['id'],'worker_name':display[w['id']][0],'work_days':len(worker_days[w['id']]),
         'work_hours':round(worker_minutes[w['id']]/60,2),'hourly_wage_czk':w['hourly_wage_czk'],
         'estimated_cost_czk':round(worker_minutes[w['id']]/60*w['hourly_wage_czk'],2)} for w in workers],
        ['worker_id','worker_name','work_days','work_hours','hourly_wage_czk','estimated_cost_czk'])
    write_csv(run/'machine_costs.csv',[
        {'machine_id':m['id'],'machine_name':m['name'],'operating_hours':round(machine_minutes[m['id']]/60,2),
         'operating_cost_czk_per_hour':m['operating_cost_czk_per_hour'],
         'estimated_cost_czk':round(machine_minutes[m['id']]/60*m['operating_cost_czk_per_hour'],2)} for m in machines.values()],
        ['machine_id','machine_name','operating_hours','operating_cost_czk_per_hour','estimated_cost_czk'])
    write_json(run/'unscheduled.json',[r for r in rows if r['status']=='UNSCHEDULED'])
    write_json(run/'external_dependencies.json',external)
    write_json(run/'untriggered_activities.json',events)
    fert=[{**r,'dose_kg_per_ha':None,'approval_status':'PENDING_SOIL_TEST_BUDGET_AND_AGRONOMIST'} for r in rows if r['activity_id']=='FERTILIZE']
    write_csv(run/'fertilization_plan.csv',fert,cols+['dose_kg_per_ha','approval_status'])
    crop_rows=[]
    for a in data['crop_assignments']['assignments']:
        loc=loc_by_id[a['location_id']];crop=crops[a['crop_id']]
        crop_rows.append({**a,'location_name':loc['name'],'area_ha':loc['area_ha'],'crop_name':crop['name'],'demands':crop['demands'],'bpej':loc['bpej']})
    write_csv(run/'crop_assignments.csv',crop_rows,['location_name','area_ha','harvest_year','crop_name','demands','bpej','prior_establishment_status','rotation_status'])
    write_csv(run/'locations.csv',locations,['id','name','type','area_ha','capacity_units','bpej','bpej_weighted_productivity_points','bpej_assignment_status'])
    # SQLite is an export database; editable JSON remains the authoritative source.
    with sqlite3.connect(run/'farm.sqlite') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE crops(id TEXT PRIMARY KEY,name TEXT NOT NULL,details_json TEXT NOT NULL)')
        db.execute('CREATE TABLE bpej_catalog(code TEXT PRIMARY KEY,productivity_points REAL,protection_class TEXT,source_url TEXT)')
        db.execute('CREATE TABLE locations(id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,area_ha REAL,details_json TEXT NOT NULL)')
        db.execute('CREATE TABLE crop_assignments(id TEXT PRIMARY KEY,location_id TEXT REFERENCES locations,crop_id TEXT REFERENCES crops,harvest_year INTEGER,UNIQUE(location_id,harvest_year))')
        db.execute('CREATE TABLE location_bpej(location_id TEXT REFERENCES locations,code TEXT REFERENCES bpej_catalog,area_ha REAL,PRIMARY KEY(location_id,code))')
        db.execute('CREATE TABLE activities(id TEXT PRIMARY KEY,name TEXT,recurrence_json TEXT,details_json TEXT)')
        db.execute('CREATE TABLE occurrences(id TEXT PRIMARY KEY,location_id TEXT REFERENCES locations,crop_id TEXT REFERENCES crops,status TEXT,start_at TEXT,end_at TEXT,details_json TEXT NOT NULL)')
        db.executemany('INSERT INTO crops VALUES(?,?,?)',[(c['id'],c['name'],json.dumps(c,ensure_ascii=False)) for c in crops.values()])
        db.executemany('INSERT INTO bpej_catalog VALUES(?,?,?,?)',[(b['code'],b['productivity_points'],b['protection_class'],b['source_url']) for b in bpej.values()])
        db.executemany('INSERT INTO locations VALUES(?,?,?,?,?)',[(l['id'],l['name'],l['type'],l['area_ha'],json.dumps(l,ensure_ascii=False)) for l in locations])
        db.executemany('INSERT INTO location_bpej VALUES(?,?,?)',[(l['id'],p['code'],p['area_ha']) for l in locations for p in l['bpej']])
        db.executemany('INSERT INTO crop_assignments VALUES(?,?,?,?)',[(a['id'],a['location_id'],a['crop_id'],a['harvest_year']) for a in data['crop_assignments']['assignments']])
        db.executemany('INSERT INTO activities VALUES(?,?,?,?)',[(a['id'],a['name'],json.dumps(a['recurrence']),json.dumps(a,ensure_ascii=False)) for a in data['activities']['activities']])
        db.executemany('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?)',[(r['id'],r['location_id'],r['crop_id'],r['status'],r['start_at'],r['end_at'],json.dumps(r,ensure_ascii=False)) for r in rows])
        db.execute('CREATE INDEX occurrence_location_time ON occurrences(location_id,start_at)')
    (run/'annual_overview.svg').write_text(overview(locations,rows,settings),encoding='utf-8')
    months=sorted({(date.fromisoformat(settings['starting_date'])+timedelta(days=i)).strftime('%Y-%m') for i in range(settings['horizon_days'])})
    monthly=run/'monthly';monthly.mkdir()
    for month in months:
        items=[r for r in rows if (r['start_at'] or r['nominal_date']).startswith(month)]
        write_csv(monthly/(month+'.csv'),items,cols)
        (monthly/(month+'.svg')).write_text(overview(locations,items,settings,month),encoding='utf-8')
    zone=ZoneInfo(settings['timezone']);start=date.fromisoformat(settings['starting_date']);end=start+timedelta(days=settings['horizon_days'])
    utc_start=datetime.combine(start,time.min,zone).astimezone(timezone.utc);utc_end=datetime.combine(end,time.min,zone).astimezone(timezone.utc)
    fixture={'horizon':{'start_at':utc_start.isoformat(),'horizon_hours':int((utc_end-utc_start).total_seconds()/3600),'timezone':settings['timezone']},
             'resources':{'workers':[{**w,'name':display[w['id']][0]} for w in workers]},'locations':[{'field_id':l['id'],'field_name':l['name'],'location_type':l['type']} for l in locations]}
    chart_rows=[{'start_at':r['start_at'],'end_at':r['end_at'],'field_id':r['location_id'],'field_name':r['location_name'],
        'worker_ids':r.get('worker_ids',[r['worker_id']]),'worker_names':r['worker_name'].split(', ') if r['worker_name'] else [],'machine_names':[r['machine_name']] if r['machine_name'] else [],
        'equipment_names':[],'operation_type':r['activity_id'],'milking_mode':r.get('staffing_mode') if r['activity_id']=='MILK' else None,
        'operation_name':('Podmíněně: ' if r['status']=='CONDITIONAL' else '')+r['name']} for r in rows if r['start_at']]
    weekly=run/'weekly';weekly.mkdir()
    location_groups={
        'fields':[l['id'] for l in locations if l['type']=='FIELD'],
        'animals':[l['id'] for l in locations if l['type'] in ('CATTLE','PIG','POULTRY','MILKING','SHEEP','GOAT','STABLE')],
        'other':[l['id'] for l in locations if l['type'] not in ('FIELD','CATTLE','PIG','POULTRY','MILKING','SHEEP','GOAT','STABLE')],
    }
    for i in range(ceil_div(settings['horizon_days'],7)):
        for group,entity_ids in location_groups.items():
            (weekly/f'locations_week_{i+1:02}_{group}.svg').write_text(render_timeline(fixture,chart_rows,i,True,entity_ids=entity_ids,title_suffix=group),encoding='utf-8')
        (weekly/f'workers_week_{i+1:02}.svg').write_text(render_timeline(fixture,chart_rows,i,False),encoding='utf-8')
    daily_dir=run/'daily';daily_dir.mkdir()
    daily_index=[]
    worker_daily_index=[]
    worker_dir=daily_dir/'workers';worker_dir.mkdir()
    worker_folders={w['id']:worker_dir/display[w['id']][1] for w in workers}
    for folder in worker_folders.values():
        folder.mkdir()
    rows_by_day=defaultdict(list)
    for row in chart_rows:
        rows_by_day[row['start_at'][:10]].append(row)
    for day_offset in range(settings['horizon_days']):
        day=(start+timedelta(days=day_offset)).isoformat()
        for group,entity_ids in location_groups.items():
            filename=f'locations_{day}_{group}.svg'
            (daily_dir/filename).write_text(render_daily_agenda(fixture,rows_by_day[day],day,True,entity_ids=entity_ids,title_suffix=group),encoding='utf-8')
            daily_index.append({'date':day,'view':'locations','group':group,'file':filename})
        filename=f'workers_{day}.svg'
        (daily_dir/filename).write_text(render_daily_agenda(fixture,rows_by_day[day],day),encoding='utf-8')
        daily_index.append({'date':day,'view':'workers','group':'all','file':filename})
        for worker in data['workers']['workers']:
            worker_id=worker['id']
            worker_rows=[row for row in rows_by_day[day] if worker_id in row['worker_ids']]
            relative_file=f'workers/{display[worker_id][1]}/{day}.svg'
            (daily_dir/relative_file).write_text(
                render_daily_agenda(fixture,worker_rows,day,entity_ids={worker_id},title_suffix=display[worker_id][0]),encoding='utf-8')
            entry={'date':day,'view':'worker','group':'individual','file':relative_file,
                   'worker_id':worker_id,'worker_name':display[worker_id][0],'task_count':len(worker_rows)}
            daily_index.append(entry)
            worker_daily_index.append(entry)
    index_columns=['date','view','group','file','worker_id','worker_name','task_count']
    write_csv(daily_dir/'index.csv',daily_index,index_columns)
    write_csv(worker_dir/'index.csv',worker_daily_index,index_columns)
    monthly_worker_dir=monthly/'workers';monthly_worker_dir.mkdir()
    month_worker_rows=defaultdict(list)
    for row in chart_rows:
        for worker_id in row['worker_ids']:
            month_worker_rows[row['start_at'][:7],worker_id].append(row)
    monthly_worker_index=[]
    for worker in data['workers']['workers']:
        worker_id=worker['id']
        (monthly_worker_dir/display[worker_id][1]).mkdir()
        for month in months:
            days=[start+timedelta(days=i) for i in range(settings['horizon_days'])
                  if (start+timedelta(days=i)).strftime('%Y-%m')==month]
            worker_rows=month_worker_rows[month,worker_id]
            filename=f'{display[worker_id][1]}/{month}.svg'
            (monthly_worker_dir/filename).write_text(
                render_monthly_worker_agenda({**worker,'name':display[worker_id][0]},worker_rows,days,month),encoding='utf-8')
            monthly_worker_index.append({'month':month,'from_date':days[0].isoformat(),'to_date':days[-1].isoformat(),
                                         'worker_id':worker_id,'worker_name':display[worker_id][0],
                                         'work_days':len({row['start_at'][:10] for row in worker_rows}),
                                         'task_count':len(worker_rows),'file':filename})
    write_csv(monthly_worker_dir/'index.csv',monthly_worker_index,
              ['month','from_date','to_date','worker_id','worker_name','work_days','task_count','file'])
    coverage=defaultdict(lambda:Counter())
    daily=defaultdict(int);weekly_load=defaultdict(int)
    for r in rows:
        coverage[r['location_id'],r['activity_id']][r['status']]+=1
        if r['start_at']:
            d=datetime.fromisoformat(r['start_at']).date()
            for worker_id in r.get('worker_ids',[r['worker_id']]):
                daily[worker_id,d.isoformat()]+=r['duration_minutes'];weekly_load[worker_id,str(d.isocalendar()[:2])]+=r['duration_minutes']
    write_json(run/'recurrence_coverage.json',[{'location_id':loc,'location_name':loc_by_id[loc]['name'],'activity_id':a,'expected':sum(counts.values()),'counts':dict(counts)} for (loc,a),counts in coverage.items()])
    write_csv(run/'worker_daily_load.csv',[{'worker_id':w,'date':d,'minutes':m} for (w,d),m in sorted(daily.items())],['worker_id','date','minutes'])
    counts=Counter(r['status'] for r in rows)
    summary={'run_id':run.name,'algorithm':'DETERMINISTIC_RESOURCE_CONSTRAINED_LIST_SCHEDULING','globally_optimal':False,
        'start_date':start.isoformat(),'end_date_inclusive':(end-timedelta(days=1)).isoformat(),'days':settings['horizon_days'],
        'field_count':sum(l['type']=='FIELD' for l in locations),'location_count':len(locations),'crop_count':len(crops),'worker_count':len(workers),
        'used_worker_count':len(used_worker_ids),'unused_worker_count':len(workers)-len(used_worker_ids),'used_machine_count':len(used_machine_ids),'unused_machine_count':len(machines)-len(used_machine_ids),
        'estimated_cost_czk':round(sum(r['total_cost_czk'] for r in rows),2),
        'occurrence_count':len(tasks),'status_counts':dict(counts),'scheduled_constraints_valid':not issues,'production_ready':False,
        'max_daily_work_minutes':max(daily.values(),default=0),'max_weekly_work_minutes':max(weekly_load.values(),default=0),
        'validation_issues':issues,'limitations':['Synthetic BPEJ assignment and productivity','Conditional agronomic tasks, unknown fertilizer doses','Travel buffer is not a route certificate or paid work accounting','Pre-horizon crop establishment is an explicit assumption','No measured weather, disease or crop maturity inputs','Facility durations are sample estimates; team staffing is modeled where required'],
        'files':['annual_overview.svg','schedule.csv','schedule.json','cost_summary.csv','worker_costs.csv','machine_costs.csv','Unused/','AUDIT_OPTIMALIZACE.txt','farm.sqlite','crop_assignments.csv','locations.csv','fertilization_plan.csv','recurrence_coverage.json','worker_daily_load.csv','unscheduled.json','external_dependencies.json','untriggered_activities.json','input_snapshot.json','monthly/','weekly/','daily/']}
    write_json(run/'run_summary.json',summary)
    audit=(data_dir/'AUDIT_OPTIMALIZACE.txt').read_text(encoding='utf-8')
    (run/'AUDIT_OPTIMALIZACE.txt').write_text(
        f'VÝSLEDEK BĚHU {run.name}\nVyužití pracovníci: {len(used_worker_ids)}/{len(workers)}; '
        f'využité stroje: {len(used_machine_ids)}/{len(machines)}; '
        f'odhad přímých nákladů: {summary["estimated_cost_czk"]} Kč; '
        f'nepřiřazené úkoly: {counts["UNSCHEDULED"]}.\n\n'+audit,encoding='utf-8')
    (run/'README.txt').write_text('ROČNÍ PLÁN FARMY\n\n'+f'{start} až {end-timedelta(days=1)}; {len(locations)} lokalit, {len(tasks)} úkolů.\n'+
        f'Stavy: {dict(counts)}. Kontrola modelovaných omezení: {not issues}.\n\n'+
        'Začněte annual_overview.svg. Měsíce jsou v monthly/, týdenní časové osy ve weekly/ a čitelné denní rozpisy v daily/index.csv. Samostatné rozpisy pracovníků: daily/workers/index.csv a monthly/workers/index.csv.\n'+
        'CONDITIONAL = rezervace kapacity, provedení vyžaduje uvedené kontroly. PLANNED také neznamená hotovo.\n'+
        'BPEJ je skutečný katalogový kód přiřazený vzorovému pozemku. Hnojení nemá automaticky určenou dávku.\n'+
        'SQLite je export aktuálního běhu. Editujte zdrojové JSON v data/annual a data/starting_date.json.\n'+
        'Nejde o globálně optimální ani provozně certifikovaný plán. Podrobnosti omezení jsou v run_summary.json.\n',encoding='utf-8')
    write_json(ROOT/'data'/'annual_latest.json',{'run_id':run.name,'relative_path':str(run.relative_to(ROOT))})
    print(json.dumps(summary,ensure_ascii=True,indent=2))


def ceil_div(a,b): return (a+b-1)//b


if __name__=='__main__':main()
