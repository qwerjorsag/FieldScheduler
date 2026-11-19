"""Standalone SVG time tables, with workers or fields on the vertical axis."""
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


def render_timeline(fixture, rows, week_index, by_field=False, entity_ids=None, day_offset=None, title_suffix=''):
    zone = ZoneInfo(fixture['horizon']['timezone'])
    origin = datetime.fromisoformat(fixture['horizon']['start_at'].replace('Z', '+00:00')).astimezone(zone)
    horizon_end = (origin.astimezone(ZoneInfo('UTC')) + timedelta(hours=fixture['horizon']['horizon_hours'])).astimezone(zone)
    count = (horizon_end.date() - origin.date()).days
    if day_offset is None:
        dates = [origin.date() + timedelta(days=i) for i in range(week_index*7, min(count, (week_index+1)*7))]
    else:
        dates = [origin.date() + timedelta(days=day_offset)] if 0 <= day_offset < count else []
    if by_field:
        entities = [(x['field_id'], x['field_name']) for x in fixture['locations'] if entity_ids is None or x['field_id'] in entity_ids]
    else:
        entities = [(x['id'], x['name']) for x in fixture['resources']['workers'] if entity_ids is None or x['id'] in entity_ids]
    first_hour, last_hour = 4, 24
    left, top, row_h, scale, gap = 220, 145, 70, 20, 22
    day_w = (last_hour-first_hour)*scale
    width = left + len(dates)*(day_w+gap) + 20
    legend_types=sorted({r['operation_type'] for r in rows if r['start_at'][:10] in {day.isoformat() for day in dates}})
    legend_rows=(len(legend_types)+5)//6
    height = top+len(entities)*row_h+65+28*legend_rows+50
    colors = {'PLOUGH':'#a16235','SOW':'#267347','SPRAY':'#236f9c','HARVEST':'#9c731b'}
    labels = {'PLOUGH':'Orba','SOW':'Set\u00ed','SPRAY':'Post\u0159ik','HARVEST':'Sklize\u0148'}
    colors.update(PREPARE='#a16235',FERTILIZE='#a87020',FEED='#187c70',MILK='#236f9c',MILK_CLEAN='#447e9e',BEDDING='#8a5771',HEALTH='#658044',MOW='#3b8357',HAY_COLLECT='#9c731b')
    labels.update(PREPARE='P\u0159\u00edprava',FERTILIZE='V\u00fd\u017eiva',FEED='Krmen\u00ed',MILK='Dojen\u00ed',MILK_CLEAN='Sanitace',BEDDING='Stlan\u00ed',HEALTH='Kontrola',MOW='Se\u010den\u00ed',HAY_COLLECT='Sb\u011br')
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#f4f7fb"/>']
    def text(x,y,value,size=12,color='#34445c',bold=False):
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}">{escape(str(value))}</text>')
    period = dates[0].strftime('%d.%m.%Y') if day_offset is not None and dates else f't\u00fdden {week_index+1}'
    text(24,38,('Rozpis podle pol\u00ed' if by_field else 'Rozpis pracovn\u00edk\u016f')+f' / {period} {title_suffix}',26,bold=True)
    text(24,66,'04:00\u201324:00 | po 22:00 pouze neru\u0161iv\u00e9 pr\u00e1ce | Europe/Prague',13)
    text(24,91,'Přesné časy a úplné názvy úkolů najdete v denním rozpisu; detail bloku zobrazí najetí myší.',12)
    for i,(entity_id,name) in enumerate(entities):
        y=top+i*row_h
        parts.append(f'<rect x="16" y="{y}" width="{width-32}" height="{row_h}" fill="{"#ffffff" if i%2==0 else "#eef3f8"}"/>')
        text(25,y+28,name,15,bold=True)
    for d,day in enumerate(dates):
        x=left+d*(day_w+gap)
        weekday=('Po','Út','St','Čt','Pá','So','Ne')[day.weekday()]
        text(x,111,f'{weekday} {day:%d.%m.%Y}',14,bold=True)
        parts.append(f'<rect x="{x+18*scale}" y="{top}" width="{2*scale}" height="{len(entities)*row_h}" fill="#dee5f0"/>')
        if not by_field:
            worker_by_id={worker['id']:worker for worker in fixture['resources']['workers']}
            for entity_id,name in entities:
                absence=next((item for item in worker_by_id[entity_id].get('unavailable',[])
                              if item['start_date']<=day.isoformat()<=item['end_date']),None)
                if absence:
                    y=top+entities.index((entity_id,name))*row_h
                    parts.append(f'<rect x="{x}" y="{y+3}" width="{day_w}" height="{row_h-6}" rx="4" fill="#f3c9ca"/>')
                    text(x+8,y+40,absence.get('reason','NEPŘÍTOMNOST'),13,'#7f2734',True)
        for hour in range(first_hour,last_hour+1,2):
            hx=x+(hour-first_hour)*scale
            text(hx,126,f'{hour:02}',9)
            parts.append(f'<line x1="{hx}" x2="{hx}" y1="{top}" y2="{top+len(entities)*row_h}" stroke="#d3dce8"/>')
    indices = {key:i for i,(key,_) in enumerate(entities)}
    for index,row in enumerate(rows):
        s,e=datetime.fromisoformat(row['start_at']).astimezone(zone),datetime.fromisoformat(row['end_at']).astimezone(zone)
        if s.date() not in dates:
            continue
        d=dates.index(s.date())
        x=left+d*(day_w+gap)+(s.hour+s.minute/60-first_hour)*scale
        # This view shows local wall-clock positions; fixture work never spans midnight.
        eh=24 if e.date()>s.date() else e.hour+e.minute/60
        w=(eh-s.hour-s.minute/60)*scale
        keys=[row['field_id']] if by_field else row['worker_ids']
        for key in keys:
            if key not in indices:
                continue
            y=top+indices[key]*row_h+8
            clip=f'clip_{index}_{indices[key]}'
            parts.append(f'<clipPath id="{clip}"><rect x="{x}" y="{y}" width="{w}" height="48"/></clipPath>')
            title=f"{row['operation_name']} | {row['field_name']} | {s:%d.%m. %H:%M} - {e:%d.%m. %H:%M} | {', '.join(row['worker_names'])} | {row.get('milking_mode') or ''} | {', '.join(row['machine_names']+row['equipment_names'])}"
            color=colors.get(row.get('operation_type'),'#7b56a3')
            parts.append(f'<g><title>{escape(title)}</title><rect x="{x}" y="{y}" width="{w}" height="48" rx="4" fill="{color}"/><g clip-path="url(#{clip})">')
            label=labels.get(row.get('operation_type'),row['operation_name'])
            if row.get('operation_type')=='MILK':
                label += ' (robot)' if row.get('milking_mode')=='ROBOTIC' else ' (ručně)'
            text(x+4,y+18,label,11,'#ffffff',True)
            text(x+4,y+35,', '.join(row['worker_names']) if by_field else row['field_name'],11,'#ffffff')
            parts.append('</g></g>')
    legend_y=top+len(entities)*row_h+26
    text(24,legend_y-9,'Legenda činností:',14,bold=True)
    for i,operation in enumerate(legend_types):
        x=24+(i%6)*max(185,(width-40)//6)
        y=legend_y+(i//6)*28
        parts.append(f'<rect x="{x}" y="{y}" width="16" height="16" rx="3" fill="{colors.get(operation,"#7b56a3")}"/>')
        text(x+23,y+13,labels.get(operation,operation),12)
    text(24,legend_y+legend_rows*28+16,'Růžová = dovolená/nepřítomnost · Šedá zóna = 22:00–24:00 · celé názvy jsou v denním rozpisu.',12)
    text(24,height-34,'Barva rozlišuje druh činnosti; podrobnosti zobrazí najetí myší.',12)
    text(24,height-14,'P\u0159ejezdy nejsou certifikovan\u00e9; dokon\u010den\u00ed pr\u00e1ce zat\u00edm nen\u00ed evidov\u00e1no.',11)
    parts.append('</svg>')
    return ''.join(parts)


def render_daily_agenda(fixture, rows, day, by_field=False, entity_ids=None, title_suffix=''):
    """Printable daily SVG with full task names and times visible without hover."""
    zone = ZoneInfo(fixture['horizon']['timezone'])
    source = fixture['locations'] if by_field else fixture['resources']['workers']
    id_key = 'field_id' if by_field else 'id'
    name_key = 'field_name' if by_field else 'name'
    sections = []
    for entity in source:
        key = entity[id_key]
        if entity_ids is not None and key not in entity_ids:
            continue
        jobs = [r for r in rows if r['start_at'][:10] == day and
                (r['field_id'] == key if by_field else key in r['worker_ids'])]
        if jobs:
            sections.append((entity[name_key], sorted(jobs, key=lambda r: r['start_at'])))
    width, top, line_h, section_h = 1250, 155, 34, 42
    height = top + sum(section_h + len(jobs)*line_h + 12 for _,jobs in sections) + 50
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#f4f7fb"/>']
    def label(x, y, value, size=15, color='#26374e', weight=400):
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(str(value))}</text>')
    heading = 'Denní rozpis lokalit' if by_field else 'Denní rozpis pracovníků'
    label(24, 40, f'{heading} · {day} · {title_suffix}', 27, weight=700)
    label(24, 70, 'Čas · činnost · přidělený personál / pracoviště', 16)
    label(24, 96, 'Podmíněné úkoly vyžadují ověření. Časy jsou místní (Europe/Prague).', 14)
    label(24, 132, f'Pracovišť / pracovníků s úkoly: {len(sections)}', 14)
    y = top
    for name, jobs in sections:
        parts.append(f'<rect x="16" y="{y}" width="{width-32}" height="{section_h}" rx="5" fill="#dce8f3"/>')
        label(30, y+28, f'{name}  ·  {len(jobs)} úkolů', 18, weight=700)
        y += section_h
        for n, row in enumerate(jobs):
            start = datetime.fromisoformat(row['start_at']).astimezone(zone)
            end = datetime.fromisoformat(row['end_at']).astimezone(zone)
            parts.append(f'<rect x="16" y="{y}" width="{width-32}" height="{line_h}" fill="{"#ffffff" if n%2==0 else "#eef3f8"}"/>')
            label(30, y+23, f'{start:%H:%M}–{end:%H:%M}', 15, weight=700)
            task = row['operation_name']
            if row.get('operation_type') == 'MILK':
                task += ' · robotické' if row.get('milking_mode') == 'ROBOTIC' else ' · ruční'
            label(165, y+23, task, 15)
            detail = ', '.join(row['worker_names']) if by_field else row['field_name']
            label(760, y+23, detail, 15)
            y += line_h
        y += 12
    if not sections:
        absence=None
        if not by_field and entity_ids and len(entity_ids)==1:
            worker=next(w for w in source if w['id'] in entity_ids)
            absence=next((item.get('reason','NEPŘÍTOMNOST') for item in worker.get('unavailable',[])
                          if item['start_date']<=day<=item['end_date']),None)
        label(24, top+35, absence or 'Pro tento den nejsou v tomto pohledu naplánované úkoly.', 16,
              '#9b3340' if absence else '#26374e')
    parts.append('</svg>')
    return ''.join(parts)


def render_monthly_worker_agenda(worker, rows, days, month):
    """Monthly worker plan with every horizon day, including days off."""
    grouped = {day.isoformat(): [] for day in days}
    for row in rows:
        day = row['start_at'][:10]
        if day in grouped:
            grouped[day].append(row)
    for jobs in grouped.values():
        jobs.sort(key=lambda row: row['start_at'])
    task_count = sum(map(len, grouped.values()))
    work_days = sum(bool(jobs) for jobs in grouped.values())
    minutes = sum(int((datetime.fromisoformat(row['end_at']) - datetime.fromisoformat(row['start_at'])).total_seconds()//60)
                  for jobs in grouped.values() for row in jobs)
    width, top, day_h, task_h, gap = 1250, 155, 34, 30, 7
    height = top + sum(day_h + max(1,len(jobs))*task_h + gap for jobs in grouped.values()) + 35
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#f4f7fb"/>']
    def label(x, y, value, size=15, color='#26374e', weight=400):
        parts.append(f'<text x="{x}" y="{y}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(str(value))}</text>')
    label(24, 39, f'Měsíční rozpis · {worker["name"]} · {month}', 27, weight=700)
    label(24, 70, f'Období {days[0]:%d.%m.%Y}–{days[-1]:%d.%m.%Y} · {work_days} pracovních dní · {task_count} úkolů · {minutes//60} h {minutes%60:02d} min', 16)
    label(24, 99, 'Časy jsou místní (Europe/Prague). Podmíněné úkoly vyžadují ověření.', 14)
    label(24, 130, 'Den a čas', 14, weight=700)
    label(170, 130, 'Činnost', 14, weight=700)
    label(760, 130, 'Pracoviště', 14, weight=700)
    y = top
    weekdays = ('Po','Út','St','Čt','Pá','So','Ne')
    for day in days:
        jobs = grouped[day.isoformat()]
        parts.append(f'<rect x="16" y="{y}" width="{width-32}" height="{day_h}" rx="4" fill="#dce8f3"/>')
        label(28, y+24, f'{weekdays[day.weekday()]} {day:%d.%m.%Y}', 16, weight=700)
        absence=next((item.get('reason','NEPŘÍTOMNOST') for item in worker.get('unavailable',[])
                      if item['start_date']<=day.isoformat()<=item['end_date']),None)
        label(760, y+24, absence or (f'{len(jobs)} úkolů' if jobs else 'Volno'), 14,
              '#9b3340' if absence else '#26374e')
        y += day_h
        if not jobs:
            label(170, y+21, absence or 'Bez přidělené práce', 14, '#9b3340' if absence else '#6a788b')
            y += task_h
        for index, row in enumerate(jobs):
            parts.append(f'<rect x="16" y="{y}" width="{width-32}" height="{task_h}" fill="{"#ffffff" if index%2==0 else "#eef3f8"}"/>')
            start = datetime.fromisoformat(row['start_at'])
            end = datetime.fromisoformat(row['end_at'])
            label(28, y+21, f'{start:%H:%M}–{end:%H:%M}', 14, weight=700)
            task = row['operation_name']
            if row.get('operation_type') == 'MILK':
                task += ' · robotické' if row.get('milking_mode') == 'ROBOTIC' else ' · ruční'
            label(170, y+21, task, 14)
            label(760, y+21, row['field_name'], 14)
            y += task_h
        y += gap
    parts.append('</svg>')
    return ''.join(parts)
