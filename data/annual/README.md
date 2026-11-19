# Roční plán velké vzorové farmy

Spusť z kořene projektu:

```powershell
.\.venv\Scripts\python.exe main.py
```

Stejný běh spustí `data/generate_annual_run.py`. Výsledek vznikne v nové složce
`Schedules/annual_...`; odkaz na poslední běh obsahuje `data/annual_latest.json`.
Generátor dat `create_annual_dataset.py` sloužil k prvotnímu založení. Existující
JSON nepřepisuje, aby nezničil ruční úpravy.

## Přehledné SVG výstupy

Ve složce `weekly/` je každý týden rozdělen na čtyři časové osy: `fields`,
`animals`, `other` a `workers`. Ve složce `daily/` je pro každý den samostatný
seznam úkolů po polích, živočišných provozech, ostatních lokalitách a pracovnících.
Denní rozpis uvádí celý název činnosti, čas a přidělené lidi přímo v řádku.
Soubor `daily/index.csv` obsahuje datum, pohled a název příslušného SVG.
Samostatné stránky každého pracovníka jsou v `daily/workers/<worker_id>/<datum>.svg`.
Jejich přehled `daily/workers/index.csv` uvádí jméno, datum, počet úkolů a soubor;
stránka existuje i pro den bez přidělené práce.
Měsíční rozpis každého pracovníka je v `monthly/workers/<worker_id>/<rok-měsíc>.svg`.
Index `monthly/workers/index.csv` uvádí jméno, měsíc, pokryté dny, počet pracovních
dní, počet úkolů a soubor. První a poslední měsíc zahrnují jen dny uvnitř horizontu.

Kravíny mají v `farm_locations.json` atribut `milking_mode`: `ROBOTIC` vyžaduje
jednoho pracovníka/technika, `MANUAL` dva pracovníky. Režim dojení je uvedený
i v denních a týdenních SVG. V `schedule.csv` a `schedule.json` je tým uvedený
v `worker_ids`; `required_staff_count` a `staffing_mode` umožňují kontrolovat
splnění personálního požadavku.

## Rozsah

Je zde přesně **20 pěstebních polí a 50 lokalit celkem**: navíc 4 sady, 5 luk,
3 pastviny, 2 rybníky, 2 háje, 2 kravíny, 2 vepříny, 2 drůbežárny, dojírna,
ovčín, kozín, stáj koní, vinice, skleník, obilní sklad a dílna. Vzorový personál
má 35 pracovníků včetně ranní a odpolední péče o zvířata. Plodiny mají 10 profilů.
Budovy a kontroly se plánují bez fiktivní výměry a bez povinného traktoru.

Začátek zůstává v `../starting_date.json`. Aktuálně jde o **24. 11. 2026 až
23. 11. 2027 včetně**, tedy 365 dní. Časy jsou Europe/Prague, slot je 15 minut.
Pro kalendářní rok nastav `starting_date` například na `2027-01-01` a horizont
365 dní. Pro jiná léta aktualizuj i přiřazení plodin; osevní postup se nekopíruje
automaticky do neomezené budoucnosti. U přestupného kalendářního roku použij 366 dní.

## Editovatelné vstupy

- `farm_locations.json`: 50 pracovišť. Pěstební pole mají `type: FIELD`, výměru,
  faktor obtížnosti práce a překryvy BPEJ s výměrami. U budov je výměra pro práci
  `null`; `capacity_units` je orientační počet zvířat. Budovy, rybníky, háje nebo
  skleník nemají automaticky přidělenou zemědělskou BPEJ.
  Kravíny, vepříny a ostatní živočišné provozy mají navíc `animal_count`, `animal_groups` a stav počtu.
  `animal_count` nesmí překročit kapacitu ani se lišit od součtu skupin.
- `crops.json`: nároky na vodu, živiny a práci (orientační škála 1–5), půdní nároky,
  doporučený návrat plodiny, sezonní okna a návaznosti operací. Nejde o dávkovací
  ani výnosový model. Pšenice/řepka mají setí v předchozím roce sklizně.
- `crop_assignments.json`: konkrétní plodina na poli a rok sklizně. Obsahuje
  cykly 2027 a navazující 2028. Nové podzimní založení čeká na předchozí sklizeň.
- `activities.json`: opakované úkoly všech provozů, časy, flexibilita, dovednost,
  pracovní postup, hluk, podmínky a návaznost. Krmení je výchozím nastavením
  každý den v 06:00 a 16:00. Dojení je v 05:00 a 17:00; následuje sanitace.
  U péče o zvířata je `fixed_setup_minutes` pevný čas přípravy a
  `animal_time_minutes_by_location_type` čas na jedno zvíře. Celková délka je
  pevná příprava + počet zvířat × čas na jedno zvíře, zaokrouhleno na slot.
- `workers.json`: povolené lokality a činnosti, směnové okno, denní/týdenní strop,
  odpočinek, přestávka a rezerva při změně místa. Přiřazení vybírá plánovač.
  `hourly_wage_czk` je vzorová hodinová mzda, `employment_type` je `PERMANENT`
  nebo `TEMPORARY`. Položky `unavailable` obsahují `start_date`, `end_date`
  a `reason` pro dovolenou, nemoc či jinou nepřítomnost. Zadaný interval včetně
  obou konců pracovníka z plánování vyloučí; SVG jej označí.
- `machines.json`: dostupné soupravy, operace, výkon v ha/h a příprava.
  `operating_cost_czk_per_hour` je vzorový variabilní provozní náklad v Kč/h.
- `bpej_catalog.json`: skutečné katalogové kódy a zdroje. Přiřazení na naši
  vzorovou farmu je syntetické, ne výsledek zjištění skutečných parcel.
- `fertilization_policy.json`: podmínky schválení výživy a odstup dělených dávek.
- `../planning_policy.json`: denní pracovní okno, klid po 22:00 a celkový strop.
  Roční heuristika preferuje kontinuitu lokace; nepoužívá číselnou CP-SAT penalizaci
  `field_switch_penalty_minutes`. Rezervu přechodu určuje pracovník.

## Opakování

Každá provozní činnost má `recurrence`. Stejný modul se používá u plodinových
fází v jejich sezonním okně. Změna četnosti fáze může zrušit jejího předchůdce;
taková navazující práce se nesmí tiše provést a zůstane nenaplánovaná.

Příklady:

```json
{"frequency":"DAILY","interval":1,"anchor_date":"2026-01-01"}
{"frequency":"WEEKLY","interval":2,"weekdays":[0,3],"anchor_date":"2026-01-05"}
{"frequency":"MONTHLY","interval":1,"monthdays":[1,15]}
{"frequency":"YEARLY","interval":1,"month_days":["05-20","07-15","09-10"]}
{"frequency":"ONCE","date":"2027-04-15"}
{"frequency":"EVENT","dates":["2027-05-03"]}
```

Pondělí je 0, neděle 6. `interval` musí být kladné celé číslo. `anchor_date`
určuje fázi intervalu; skutečnou platnost omezují volitelné `valid_from` a `until`.
`season: ["04-01","10-31"]` omezuje sezonu; povolená je i sezona přes Nový rok.
Neexistující den měsíce se vynechá (31. únor se neposouvá na březen).
`excluded_dates` v činnosti vyloučí jednotlivá data, `enabled: false` ji vypne.
Prázdný seznam událostí nic automaticky nespustí; zůstane v seznamu čekajících
podmíněných činností. Změny kritické péče musí zkontrolovat odpovědná osoba.

`times` nastavuje více opakování za den; `window_minutes` říká, do kdy musí celé
provedení od daného času skončit. Pro přesné dojení trvající 90 minut je tedy
okno také 90 minut. `date_flex_days` dovolí posun sezonní práce o několik dní.
Denní krmení má nulový posun. `depends_on` navazuje na poslední předchozí výskyt
rodičovské činnosti stejné lokality. `minimum_gap_minutes` určuje minimální odstup.

## BPEJ a výživa

Kód ukládat jako řetězec o pěti číslicích, aby se neztratily úvodní nuly.
Číslice vyjadřují klimatický region, hlavní půdní jednotku, sklonitost/expozici
a skeletovitost/hloubku. U pole lze mít více BPEJ; součet jejich výměr musí
odpovídat výměře pole. Vážená bodová výnosnost je informativní, není předpisem
hnojení ani automatickým koeficientem rychlosti stroje.

Použity jsou ověřené katalogové příklady `51400` (70 bodů, ochrana I), `51502`
(60 bodů, ochrana II), `52901` (51 bodů, ochrana II). Třída ochrany ZPF a bodová
výnosnost jsou různé údaje. Skutečnou BPEJ je potřeba zjistit z geometrie parcely.

Hnojení se rezervuje podle fáze plodiny, s odstupem dělených aplikací. Automaticky
se nepředepisuje minerální dusík luskovinám. U všech polí jsou naplánovaná
vyhodnocení výživy; P/K a vápnění se rozhodují z rozboru. Dávka zůstává `null`:
chybí skutečný rozbor, bilance, výnosový cíl, příspěvek předplodiny a statkových
hnojiv, ověření právních podmínek a schválení agronoma. Neplánovat automatický
postřik jen proto, že se opakuje kalendář; je to činnost spouštěná událostí.

Zdroje ověřené 19. 11. 2026:

- https://bpej.vumop.cz/51400
- https://bpej.vumop.cz/51502
- https://bpej.vumop.cz/52901
- https://ukzuz.gov.cz/public/portal/ukzuz/hnojiva-a-puda/evidence-hnojeni
- https://mze.gov.cz/public/portal/mze/zivotni-prostredi/nitratova-smernice/navaznost-na-dotace

## Výstupy a význam výsledku

`farm.sqlite` obsahuje tabulky plodin, lokalit, přiřazení, BPEJ a pracovních úkolů.
`cost_summary.csv` sčítá přímé mzdy a provoz strojů podle naplánovaných hodin.
Rozpad podle člověka a stroje je v `worker_costs.csv` a `machine_costs.csv`.
`Unused/workers.csv` a `Unused/machines.csv` uvádějí zdroje bez přidělené práce
v konkrétním běhu. `AUDIT_OPTIMALIZACE.txt` vysvětluje ekonomické limity.
Je to databázový export běhu; změny prováděj v JSON a spusť nový běh.
`annual_overview.svg` má řádky lokalit a sloupce měsíců. Měsíční SVG/CSV jsou
v `monthly/`; přesné časové rozpisy všech 53 týdnů po lokalitách i pracovnících
jsou ve `weekly/`. Barevná buňka přehledu ukazuje počet úkolů, nikoli jejich
trvání; úplné časy a činnosti ukáže popisek a CSV.

`PLANNED` znamená plánovanou práci, nikoli vykonání. `CONDITIONAL` je rezervace
kapacity pro zásah, jehož podmínky je nutné ještě ověřit. `UNSCHEDULED` zůstává
viditelný s důvodem, nevymaže se z potřeb farmy. Nenaplánovaný rodič blokuje potomka.
Kontrola krytí opakování je v `recurrence_coverage.json`; hnojení s prázdnou dávkou
v `fertilization_plan.csv`; předpokládané práce před začátkem v `external_dependencies.json`.

Použitý algoritmus je deterministické postupné přidělování kapacity s prioritami,
nikoli důkaz globální optimálnosti. Hlídá pracovníky, stroje, kolize lokací,
časová okna, denní/týdenní práci, odpočinek, přestávky, hluk a návaznosti.
Kapacita dílčích polních úkolů se počítá z hektarů a soupravy; automaticky se dělí
na menší části. Doby péče v budovách jsou ukázkové odhady, nikoli kalibrace podle
počtu zvířat. Počasí, dozrání, skutečné přesuny a právní shoda zatím nejsou
plně provozně ověřeny. `scheduled_constraints_valid` není `production_ready`.
