# Data a generování rozpisu

Pro aktuální velkou farmu a celý rok spusť `python main.py` z kořene projektu.
Data s 20 poli, 50 lokalitami, plodinami a opakováním jsou v `data/annual/`;
podrobnosti jsou v [roční dokumentaci](annual/README.md). Níže je starší malá ukázka.

Spuštění z kořene projektu:

```powershell
.\.venv\Scripts\python.exe data\generate_sample_run.py
```

Každý běh vytvoří novou složku `Schedules/run_...`: JSON/CSV rozpis, týdenní
obrázky pracovníků (`schedule_week_*.svg`), časové tabulky polí
(`field_schedule_week_*.svg`), karty cyklu (`field_schedule.svg`), denní zatížení
(`worker_daily_load.csv`), snapshot vstupů a diagnostiku.
V `data/sample_field_timeline.svg` je náhled prvního týdne posledního běhu.

## Co upravovat

- `starting_date.json`: `starting_date` (YYYY-MM-DD), zóna, počet kalendářních
  dní a slot 15, 30 nebo 60 minut. Jediný zdroj začátku plánování.
- `planning_policy.json`: okno 04:00–24:00, po 22:00 jen nerušivé činnosti,
  strop 720 minut/den, penalizace změny pole a rezerva na přesun.
  V této verzi musí být `day_end_hour` rovno 24.
- `workers.json`: jména, aktivita, limity a `work_permissions`, které pro každé
  pole určují `operation_types` a `machine_ids`. Pouhá kvalifikace nenahradí
  konkrétní oprávnění. Výchozí týdenní limit je 2400 minut, odpočinek 660 minut.
  `preferred_field_ids` jsou metadata pro další rozvoj přiřazování.
- `fields.json`: `area_ha`, typ lokace a `productivity_factor` (0 < hodnota <= 1).
  Všech 12 ukázkových lokalit je orná půda i při názvu Sad nebo Louka.
- `resources.json`: `productivity_ha_per_hour` zvlášť pro operaci, příprava
  stroje v minutách, kompatibility a `is_non_disruptive`. Výkony jsou syntetické.
- `sample_schedule.json`: operace, pevně přidělený pracovník a souprava,
  návaznosti a boolean `is_non_disruptive`. Plocha se bere z `fields.json`,
  trvání práce počítá generátor.
- `operation_rules.json`: vysvětlující přehled. Konkrétní vazby čte solver
  z `sample_schedule.json`, pracovní politiku z `planning_policy.json` a osobní
  limity z `workers.json`; přehledový soubor je sám nepřepisuje.

JSON je zdroj pravdy. `sample_schedule.csv` se při spuštění obnoví jako export
vypočtených vstupů. `workers.csv` je pomocný přehled; solver načítá JSON.

Efektivní ha/h = minimum(výkon stroje, výkony nářadí) × faktor pole.
Minuty = zaokrouhlení nahoru(60 × plocha / efektivní ha/h + příprava).
Rezervace v solveru se zaokrouhlí nahoru na celé sloty včetně limitů lidí.
Nulový výkon, nekompatibilní souprava nebo neoprávněný pracovník jsou chyby vstupu.

Po 22:00 musí mít operace i všechny stroje a nástroje `is_non_disruptive: true`.
Hlučná operace také nesmí začít před 22:00 a pokračovat přes tuto hranici.
Omezení změn polí je měkký cíl; dostupnost a bezpečnostní omezení jsou tvrdá.

## Rozsah a omezení

48 úkolů pro 15 lidí je technická ukázka. Orba, setí, postřik a sklizeň během
21 dnů nejsou reálný pěstební cyklus. Postupy popisuje
`../ZEMEDELSKE_CINNOSTI_A_POSTUPY.txt`; navazující vývoj a mezery shrnuje
`../AUDIT_PRO_REALNY_ZEMEDELSKY_PODNIK.txt`.

Denní/týdenní limit, mezidenní odpočinek, oprávnění a hluk runner vynucuje.
Přestávky, úplný zákonný režim směn, skutečné přejezdy a jejich započtení do práce
zatím nejsou plně propojeny. Rezerva na změnu pole není cestovní certifikát.
Výstup proto stále uvádí `validated: false`.
