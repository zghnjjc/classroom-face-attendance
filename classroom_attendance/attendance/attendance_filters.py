"""教师考勤查询的日期边界；结束边界不包含在范围内。"""

from datetime import date, datetime, time, timedelta

PERIODS = ("当天", "当周", "当月", "当前学期")


def default_semester_dates(now=None):
    today = (now or datetime.now()).date()
    if today.month == 1:
        return date(today.year - 1, 9, 1), date(today.year, 1, 31)
    if today.month >= 9:
        return date(today.year, 9, 1), date(today.year + 1, 1, 31)
    return date(today.year, 2, 1), date(today.year, 8, 31)


def validate_semester_dates(starts_on, ends_on):
    try:
        start, end = date.fromisoformat(str(starts_on)), date.fromisoformat(str(ends_on))
    except (TypeError, ValueError):
        raise ValueError("学期日期请使用 年-月-日，例如 2026-09-01。") from None
    if start > end:
        raise ValueError("学期结束日期不能早于开始日期。")
    if end == date.max:
        raise ValueError("学期结束日期须早于 9999-12-31。")
    return start, end


def record_date_range(period, now=None, semester_dates=None):
    today = (now or datetime.now()).date()
    if period == "当天":
        start, end = today, today + timedelta(days=1)
    elif period == "当周":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif period == "当月":
        start = today.replace(day=1)
        end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    elif period == "当前学期":
        start, last = validate_semester_dates(*(semester_dates or default_semester_dates(now)))
        end = last + timedelta(days=1)
    else:
        raise ValueError("请选择当天、当周、当月或当前学期。")
    return datetime.combine(start, time.min), datetime.combine(end, time.min)
