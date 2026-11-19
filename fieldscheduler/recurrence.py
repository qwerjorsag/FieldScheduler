"""Calendar recurrence expansion. Dates are local dates, never UTC 24-hour steps."""
from datetime import date, timedelta


def expand_recurrence(rule, start, end, excluded=()):
    frequency = rule['frequency'].upper()
    supported = {'ONCE','DAILY','WEEKLY','MONTHLY','YEARLY','EVENT'}
    interval = rule.get('interval',1)
    if frequency not in supported or type(interval) is not int or interval < 1:
        raise ValueError('INVALID_RECURRENCE')
    anchor = date.fromisoformat(rule.get('anchor_date','2026-01-01'))
    exclusions = {date.fromisoformat(x) for x in excluded}
    valid_from=date.fromisoformat(rule['valid_from']) if rule.get('valid_from') else start
    until=date.fromisoformat(rule['until']) if rule.get('until') else end
    if any(type(d) is not int or not 0<=d<=6 for d in rule.get('weekdays',[])):
        raise ValueError('INVALID_WEEKDAY')
    if any(type(d) is not int or not 1<=d<=31 for d in rule.get('monthdays',[])):
        raise ValueError('INVALID_MONTHDAY')
    for md in rule.get('month_days',[]):date.fromisoformat('2000-'+md)
    explicit = {date.fromisoformat(x) for x in rule.get('dates',[])}
    if frequency=='ONCE' and not explicit:
        explicit={date.fromisoformat(rule['date'])}
    season = rule.get('season')
    for i in range((end-start).days+1):
        day=start+timedelta(days=i)
        if day in exclusions or not valid_from<=day<=until:
            continue
        md=day.strftime('%m-%d')
        if season:
            lo,hi=season
            inside=lo<=md<=hi if lo<=hi else md>=lo or md<=hi
            if not inside:
                continue
        weeks=((day-timedelta(days=day.weekday()))-(anchor-timedelta(days=anchor.weekday()))).days//7
        months=(day.year-anchor.year)*12+day.month-anchor.month
        applies=(frequency in ('ONCE','EVENT') and day in explicit
                 or frequency=='DAILY' and (day-anchor).days%interval==0
                 or frequency=='WEEKLY' and weeks%interval==0 and day.weekday() in rule.get('weekdays',[anchor.weekday()])
                 or frequency=='MONTHLY' and months%interval==0 and day.day in rule.get('monthdays',[anchor.day])
                 or frequency=='YEARLY' and (day.year-anchor.year)%interval==0 and md in rule.get('month_days',[anchor.strftime('%m-%d')]))
        if applies:
            yield day
