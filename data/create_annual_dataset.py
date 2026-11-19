"""Create editable demonstration master data once; never overwrite edited files."""
import json
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

DATA = Path(__file__).resolve().parent
OUT = DATA / 'annual'


def uid(value):
    return str(uuid5(NAMESPACE_URL, 'fieldscheduler/annual/' + value))


def write(name, value):
    path = OUT / name
    if path.exists():
        raise FileExistsError(f'Refusing to overwrite editable data: {path}')
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    OUT.mkdir(exist_ok=True)
    if list(OUT.glob('*.json')):
        raise FileExistsError('Annual data already exists; edit JSON directly.')
    bpej = [dict(code=code, climate_region=5, main_soil_unit=code[1:3],
                 slope_exposure_code=code[3], stoniness_depth_code=code[4],
                 productivity_points=points, protection_class=protection,
                 source_url='https://bpej.vumop.cz/'+code, verified_on='2026-11-19')
            for code, points, protection in [('51400',70,'I'),('51502',60,'II'),('52901',51,'II')]]
    write('bpej_catalog.json', {'schema_version':1,'entries':bpej,
        'note':'Real catalog codes; assignment to synthetic locations is illustrative. Protection class is not a linear productivity score.'})
    # Month-day windows are broad planning estimates for a Czech temperate site.
    specs = [
        ('WINTER_WHEAT','Pšenice ozimá','POACEAE',True,'09-15','10-20','07-15','08-20',3,3,3,2,2),
        ('SPRING_BARLEY','Ječmen jarní','POACEAE',False,'03-10','04-15','07-15','08-15',2,2,2,2,2),
        ('SILAGE_MAIZE','Kukuřice na siláž','POACEAE',False,'04-20','05-20','09-01','10-10',4,4,4,3,2),
        ('WINTER_RAPE','Řepka ozimá','BRASSICACEAE',True,'08-15','09-05','07-01','07-25',4,4,4,4,4),
        ('PEA','Hrách setý','FABACEAE',False,'03-10','04-15','07-01','08-05',2,2,1,3,5),
        ('POTATO','Brambory','SOLANACEAE',False,'04-10','05-10','08-20','10-10',5,4,4,5,4),
        ('SUGAR_BEET','Cukrovka','AMARANTHACEAE',False,'03-20','04-20','09-20','11-10',5,4,4,4,4),
        ('OATS','Oves','POACEAE',False,'03-10','04-15','08-01','08-25',2,3,2,2,2),
        ('SUNFLOWER','Slunečnice','ASTERACEAE',False,'04-15','05-15','09-01','10-15',3,3,3,3,5),
        ('SOY','Sója','FABACEAE',False,'04-25','05-20','09-10','10-20',3,3,1,3,4),
    ]
    crops=[]
    for cid,name,family,winter,ss,se,hs,he,overall,water,nutrients,labor,rotation in specs:
        sow_offset=-1 if winter else 0
        prep_start=('08-01' if cid=='WINTER_RAPE' else '09-01') if winter else '03-01'
        stages=[
            dict(id='prepare',activity='PREPARE',name='Příprava půdy',start=prep_start,end=ss,offset=sow_offset,after=None,min_gap_days=0,requires=['soil_trafficable']),
            dict(id='sow',activity='SOW',name='Setí / sázení',start=ss,end=se,offset=sow_offset,after='prepare',min_gap_days=0,requires=['soil_temperature','seedbed_ready']),
        ]
        if family!='FABACEAE':
            stages.append(dict(id='nutrition_1',activity='FERTILIZE',name='Výživa: rozhodnutí a podmíněná aplikace',
                start='03-01' if winter else se,end='03-31' if winter else '05-31',offset=0,after='sow',min_gap_days=7,
                requires=['soil_test','nutrient_budget','agronomist_approval','legal_window','weather_suitable']))
            if winter or cid in ('SILAGE_MAIZE','SUGAR_BEET'):
                stages.append(dict(id='nutrition_2',activity='FERTILIZE',name='Přihnojení podle růstové fáze',start='04-01' if winter else '06-01',end='05-10' if winter else '06-30',offset=0,after='nutrition_1',min_gap_days=21,
                    requires=['crop_stage','nutrient_budget','agronomist_approval','legal_window','weather_suitable']))
        stages.append(dict(id='harvest',activity='HARVEST',name='Sklizeň',start=hs,end=he,offset=0,after='sow',min_gap_days=80 if not winter else 180,
            requires=['maturity','moisture','withdrawal_periods','haulage_storage']))
        if not winter and family!='FABACEAE':
            base=next(st for st in stages if st['id']=='nutrition_1')
            base.update(name='Základní výživa před založením porostu',start=prep_start,end=se,after='prepare',min_gap_days=0)
            next(st for st in stages if st['id']=='sow')['after']='nutrition_1'
            stages.remove(base);stages.insert(1,base)
        for st in stages:
            if st['id']=='nutrition_2':st['additional_after']=['sow']
        for st in stages:
            st['recurrence']={'frequency':'YEARLY','interval':1,'month_days':[st['start']]}
            st['is_non_disruptive']=False
        crops.append(dict(id=cid,name=name,family=family,winter_crop=winter,
            demands={'scale':'1 low - 5 high; illustrative agronomic classification','overall':overall,'water':water,'nutrients':nutrients,'labor':labor},
            soil_requirements={'drainage':'well_drained','ph_range':[5.5,6.5] if cid=='POTATO' else [6.0,7.2],'assessment':'Needs measured pH/texture; BPEJ alone is insufficient.'},
            recommended_rotation_break_years=rotation,
            fertilization={'routine_mineral_n':family!='FABACEAE','dose_kg_per_ha':None,'method':'Measured soil supply + previous crop/manure credits + target yield; approved nutrient budget before application. P/K and liming follow soil tests. No automatic nitrogen for legumes.'},
            stages=stages,calendar_basis='Broad illustrative windows, temperate Czech region; adjust to cultivar and phenology.'))
    write('crops.json', {'schema_version':1,'crops':crops})
    names=['Sever','Jih','Údolí','Západ','Východ','Za tratí','Hájské pole','U sadu','Dolina','Rovina','Vrchy','Meze','U potoka','Dlouhé','Na kopci','Za dvorem','Horní lán','Dolní lán','U lesa','Pod cestou']
    locations=[]
    for i,name in enumerate(names):
        area=18+(i%7)*4
        locations.append(dict(id=uid('field'+str(i+1)),name=name,type='FIELD',area_ha=area,capacity_units=None,
            productivity_factor=round(0.85+(i%4)*0.05,2),bpej=[{'code':bpej[i%3]['code'],'area_ha':area*0.8},{'code':bpej[(i+1)%3]['code'],'area_ha':area*0.2}],
            bpej_assignment_status='SYNTHETIC_NOT_VERIFIED_AGAINST_PARCEL',soil_test=None))
    groups=[('ORCHARD',4,'Sad',6),('MEADOW',5,'Louka',12),('PASTURE',3,'Pastvina',10),('POND',2,'Rybník',4),('GROVE',2,'Háj',3),
            ('CATTLE',2,'Kravín',None),('PIG',2,'Vepřín',None),('POULTRY',2,'Drůbežárna',None),('MILKING',1,'Dojírna',None),
            ('SHEEP',1,'Ovčín',None),('GOAT',1,'Kozín',None),('STABLE',1,'Stáj koní',None),
            ('VINEYARD',1,'Vinice',5),('GREENHOUSE',1,'Skleník',1),('STORAGE',1,'Obilní sklad',None),('WORKSHOP',1,'Dílna',None)]
    for kind,count,name,area in groups:
        for i in range(count):
            soil_applicable=kind in ('ORCHARD','MEADOW','PASTURE','VINEYARD')
            locations.append(dict(id=uid(kind+str(i)),name=f'{name} {i+1}',type=kind,area_ha=area,productivity_factor=0.9,
                capacity_units={'CATTLE':60,'PIG':150,'POULTRY':300,'MILKING':120,'SHEEP':50,'GOAT':40,'STABLE':12}.get(kind),
                animal_count={'CATTLE':48,'PIG':132,'POULTRY':260,'MILKING':84,'SHEEP':42,'GOAT':32,'STABLE':10}.get(kind),
                animal_groups=[{'id':'default','name':'Výchozí skupina','count':{'CATTLE':48,'PIG':132,'POULTRY':260,'MILKING':84,'SHEEP':42,'GOAT':32,'STABLE':10}.get(kind)}] if kind in ('CATTLE','PIG','POULTRY','MILKING','SHEEP','GOAT','STABLE') else [],
                milking_mode='ROBOTIC' if kind=='CATTLE' and i==0 else 'MANUAL' if kind=='CATTLE' else None,
                milking_staffing={'required_staff_count':1 if kind=='CATTLE' and i==0 else 2,'required_skill':'MILKING'} if kind=='CATTLE' else None,
                bpej=[{'code':'51400','area_ha':area}] if soil_applicable else [],
                bpej_assignment_status='SYNTHETIC_NOT_VERIFIED_AGAINST_PARCEL' if soil_applicable else 'NOT_APPLICABLE',soil_test=None))
    assert len(locations)==50
    write('farm_locations.json',{'schema_version':1,'synthetic':True,'locations':locations})
    rotation={'WINTER_WHEAT':'PEA','SPRING_BARLEY':'WINTER_RAPE','SILAGE_MAIZE':'SPRING_BARLEY','WINTER_RAPE':'WINTER_WHEAT','PEA':'WINTER_WHEAT','POTATO':'SPRING_BARLEY','SUGAR_BEET':'SILAGE_MAIZE','OATS':'PEA','SUNFLOWER':'SPRING_BARLEY','SOY':'WINTER_WHEAT'}
    assignments=[]
    for i,field in enumerate(locations[:20]):
        cid=crops[i%len(crops)]['id']
        for year,crop in [(2027,cid),(2028,rotation[cid])]:
            assignments.append(dict(id=uid(f'crop/{field["id"]}/{year}'),location_id=field['id'],crop_id=crop,harvest_year=year,
                prior_establishment_status='ASSUMED_ESTABLISHED_IF_SOWING_BEFORE_HORIZON',rotation_status='ILLUSTRATIVE_REQUIRES_HISTORY_REVIEW'))
    write('crop_assignments.json',{'schema_version':1,'assignments':assignments})
    # Every activity owns the same editable recurrence contract.
    activities=[]
    def activity(aid,name,types,skill,minutes,recurrence,times=('07:00',),window=180,priority=2,method='',quiet=False,requires=()):
        activities.append(dict(id=aid,name=name,location_types=types,skill=skill,duration_minutes=minutes,recurrence=recurrence,
            times=list(times),window_minutes=window,priority=priority,is_non_disruptive=quiet,method=method,
            requires=list(requires),enabled=True,excluded_dates=[],depends_on=None,minimum_gap_minutes=0))
    daily={'frequency':'DAILY','interval':1,'anchor_date':'2026-01-01'}
    weekly={'frequency':'WEEKLY','interval':1,'weekdays':[1],'anchor_date':'2026-01-05'}
    yearly=lambda dates:{'frequency':'YEARLY','interval':1,'month_days':dates,'anchor_date':'2026-01-01'}
    livestock=['CATTLE','PIG','POULTRY','SHEEP','GOAT','STABLE']
    activity('FEED','Krmení a kontrola napájení',livestock,'ANIMAL_CARE',30,daily,('06:00','16:00'),240,0,'Založit podle dávky skupiny; zkontrolovat vodu, funkci krmítek a příjem.')
    activity('HEALTH','Zdravotní obchůzka',livestock,'ANIMAL_CARE',30,daily,('08:00',),180,0,'Chování, pohyb, příjem, úhyny; abnormality předat zootechnikovi.',True)
    activity('BEDDING','Očista a doplnění podestýlky',livestock,'ANIMAL_CARE',45,daily,('09:00',),480,1,'Odstranit mokrá místa a doplnit čistou podestýlku podle systému.')
    activity('MILK','Dojení laktujících krav',['CATTLE'],'MILKING',90,daily,('05:00','17:00'),240,-10,'Příprava, hygiena, dojení, chlazení. Jen laktující skupiny.')
    activities[-1].update(required_staff_by_milking_mode={'ROBOTIC':1,'MANUAL':2},animal_time_minutes_by_milking_mode={'ROBOTIC':0.6,'MANUAL':1.8},fixed_setup_minutes=30)
    activity('MILK_CLEAN','Sanitace dojírny',['CATTLE'],'MILKING',30,daily,('08:30','20:30'),120,0,'Sanitace po příslušném dojení.')
    activities[-1].update(animal_time_minutes_by_location_type={'MILKING':0.0},fixed_setup_minutes=30)
    activities[-1].update(depends_on='MILK',minimum_gap_minutes=0)
    activity('EGGS','Sběr a evidence vajec',['POULTRY'],'ANIMAL_CARE',30,daily,('10:00','15:00'),120,1,'Sběr, kontrola, označení šarže a uložení.')
    activity('WATER_CHECK','Kontrola vody a ryb',['POND'],'GENERAL',30,daily,('05:00',),180,0,'Kyslík, teplota, hladina, přítok a chování ryb; alarmy řešit ihned.',True)
    activity('FISH_FEED','Krmení ryb podle teploty',['POND'],'GENERAL',30,{**daily,'season':['04-01','10-31']},('10:00',),180,1,'Krmit podle biomasy, příjmu, teploty a kyslíku; mimo sezonu individuální režim.',False,('water_temperature','dissolved_oxygen','feeding_plan'))
    activity('PASTURE_CHECK','Kontrola pastviny a vody',['PASTURE'],'GENERAL',30,daily,('08:00',),360,1,'Napájení, oplocení, zvířata, porost; stav určuje další rotaci.',True)
    activity('GREENHOUSE_CARE','Klima, závlaha a kontrola porostu',['GREENHOUSE'],'GENERAL',60,daily,('07:00',),240,1,'Zkontrolovat čidla, závlahu, klima a porost; zásahy podle měření.')
    activity('STORAGE_CHECK','Kontrola skladu',['STORAGE'],'GENERAL',30,daily,('09:00',),360,1,'Teplota, vlhkost, škůdci, stav šarží a kapacita.',True)
    activity('SCOUT','Monitoring porostu',['FIELD','ORCHARD','VINEYARD'],'FIELD_SCOUT',30,{**weekly,'season':['03-01','10-31']},('08:00',),480,2,'Fenologie, choroby, škůdci, vláha; záznam rozhodnutí.',True)
    activity('PRUNE','Řez a údržba opor',['ORCHARD','VINEYARD'],'GENERAL',240,yearly(['02-15']),('08:00',),480,2,'Druh a počasí určují termín; sanitace a údržba opor.',False,('species_specific_pruning','weather'))
    activity('FRUIT_PICK','Sklizeň a třídění ovoce',['ORCHARD','VINEYARD'],'GENERAL',240,{'frequency':'WEEKLY','interval':1,'weekdays':[0,3],'anchor_date':'2026-01-05','season':['09-01','10-15']},('08:00',),480,2,'Postupné sklizňové průchody dle zralosti, navázat chlazení.',False,('maturity','withdrawal_periods'))
    activity('MOW','Sečení louky',['MEADOW'],'GENERAL',180,yearly(['05-20','07-15','09-10']),('08:00',),480,2,'Zralost píce, předpověď, ochrana zvěře a režim pozemku.',False,('forage_stage','dry_weather','wildlife_check'))
    activity('HAY_COLLECT','Sběr a uložení píce',['MEADOW'],'GENERAL',180,yearly(['05-23','07-18','09-13']),('08:00',),480,2,'Až po dosažení vhodné sušiny; zajistit sklad a dopravu.',False,('forage_moisture','storage'))
    activities[-1].update(depends_on='MOW',minimum_gap_minutes=2880)
    activity('MEADOW_NUTRITION','Posouzení výživy louky',['MEADOW'],'FIELD_SCOUT',45,yearly(['03-20','05-25','07-20']),('08:00',),480,2,'Rozbor, bilance a odvezená píce; dávka až po schválení.',True,('soil_test','nutrient_budget'))
    activity('GROVE_CHECK','Péče o háj a bezpečnost stromů',['GROVE'],'GENERAL',90,{'frequency':'MONTHLY','interval':1,'monthdays':[10],'anchor_date':'2026-01-01'},('08:00',),480,2,'Kontrola stability, cest, odpadů a poškození; zásahy odborně.',True)
    activity('SERVICE','Servis a kontrola zásob dílny',['WORKSHOP'],'GENERAL',180,weekly,('08:00',),480,2,'Motohodiny, závady, plán oprav a díly; podle návodu výrobce.')
    activity('TREATMENT','Podmíněná ochrana rostlin',['FIELD','ORCHARD','VINEYARD'],'FIELD_SCOUT',60,{'frequency':'EVENT','dates':[]},('08:00',),480,1,'Pouze po zjištění potřeby, schválení přípravku a podmínek.',False,('agronomist_approval','label','weather','withdrawal_periods'))
    activity('NUTRIENT_REVIEW','Rozbor a plán výživy pole',['FIELD'],'FIELD_SCOUT',45,yearly(['02-20','09-01']),('08:00',),480,1,'Vyhodnotit půdní rozbor, předplodinu, statková hnojiva a cílový výnos; schválit bilanci.',True)
    for a in activities:
        a['date_flex_days']=21 if a['id']=='PRUNE' else 4 if a['id'] in ('MOW','HAY_COLLECT','NUTRIENT_REVIEW') else 1 if a['id']=='SCOUT' else 0
    write('activities.json',{'schema_version':1,'activities':activities})
    # Qualified shift groups make repeated morning/evening care possible without 20-hour days.
    workers=[]
    field_names=['Jan','Petr','Eva','Martin','Lucie','Tomáš','Tereza','Jakub','Anna','Michal','Klára','Pavel','Veronika','David','Barbora']
    all_fields=[l['id'] for l in locations if l['type']=='FIELD']
    for i,name in enumerate(field_names):
        workers.append(dict(id=uid('worker/'+name),name=name,skills=['PREPARE','SOW','HARVEST','FERTILIZE','FIELD_SCOUT'],
            allowed_location_ids=all_fields+[l['id'] for l in locations if l['type'] in ('ORCHARD','VINEYARD','MEADOW')],shift=['06:00','18:00']))
    for i in range(12):
        workers.append(dict(id=uid('care/'+str(i)),name=['Alena','Bohdan','Cyril','Dana','Eliška','Filip','Gabriela','Hynek','Ivana','Jitka','Karel','Lenka'][i],
            skills=['ANIMAL_CARE','MILKING'],allowed_location_ids=[l['id'] for l in locations if l['type'] in livestock+['MILKING']],
            shift=['04:00','13:00'] if i<6 else ['13:00','22:00']))
    for i in range(8):
        workers.append(dict(id=uid('general/'+str(i)),name=['Marek','Nina','Oldřich','Petra','Radek','Simona','Václav','Zuzana'][i],
            skills=['GENERAL','FIELD_SCOUT'],allowed_location_ids=[l['id'] for l in locations if l['type'] not in livestock+['MILKING']],shift=['05:00','17:00']))
    for w in workers:
        w.update(active=True,max_daily_minutes=720,max_weekly_minutes=2400,min_rest_minutes=660,max_continuous_minutes=360,break_minutes=30,field_change_minutes=30,
                 hourly_wage_czk=225 if 'ANIMAL_CARE' in w['skills'] else 240 if 'PREPARE' in w['skills'] else 210,
                 employment_type='PERMANENT',unavailable=[])
    write('workers.json',{'schema_version':1,'workers':workers})
    machines=[]
    for op,rate,count in [('PREPARE',5,4),('SOW',6,4),('HARVEST',5,4),('FERTILIZE',12,3)]:
        for i in range(count):
            machines.append(dict(id=uid('machine/'+op+str(i)),name=f'{op} souprava {i+1}',operation_types=[op],ha_per_hour=rate,setup_minutes=30,is_non_disruptive=False,synthetic=True,
                                 operating_cost_czk_per_hour={'PREPARE':1100,'SOW':1250,'HARVEST':2200,'FERTILIZE':950}[op]))
    write('machines.json',{'schema_version':1,'machines':machines})
    write('fertilization_policy.json',{'schema_version':1,'mode':'CONDITIONAL_CAPACITY_RESERVATION',
        'no_automatic_dose':True,'minimum_split_gap_days':21,'routine_n_for_legumes':False,
        'required_checks':['soil_test','nutrient_budget','previous_crop_and_manure_credits','yield_target','growth_stage','agronomist_approval','current_local_legal_windows','soil_not_frozen_snowcovered_saturated','water_buffers','weather'],
        'sources':['https://ukzuz.gov.cz/public/portal/ukzuz/hnojiva-a-puda/evidence-hnojeni','https://mze.gov.cz/public/portal/mze/zivotni-prostredi/nitratova-smernice/navaznost-na-dotace']})
    print('Created annual dataset: 20 fields, 50 locations, 10 crops, 35 workers.')


if __name__=='__main__':
    main()
