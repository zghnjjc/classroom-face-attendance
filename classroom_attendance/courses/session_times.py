"""课次的开放、正式上课、迟到和关闭边界。"""

from datetime import datetime, timedelta


SCHEMA_VERSION = 5


def resolve_session_times(starts_at, ends_at, late_minutes=10, *, opens_at=None, late_at=None):
    opens_at = starts_at if opens_at is None else opens_at
    for value in (opens_at, starts_at, ends_at):
        if not isinstance(value, datetime) or value.tzinfo is not None:
            raise ValueError('课次时间须为有效的本地日期时间，不含时区。')
    if late_at is not None and (not isinstance(late_at, datetime) or late_at.tzinfo is not None):
        raise ValueError('课次时间须为有效的本地日期时间，不含时区。')
    if late_at is None:
        try:
            minutes = int(late_minutes)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('迟到宽限应为 0 至 180 分钟。') from None
        if not 0 <= minutes <= 180:
            raise ValueError('迟到宽限应为 0 至 180 分钟。')
        late_at = starts_at + min(timedelta(minutes=minutes), ends_at-starts_at)
    times = {key: value.replace(microsecond=0) for key, value in
             (('opens_at', opens_at), ('starts_at', starts_at), ('late_at', late_at), ('ends_at', ends_at))}
    if not (times['opens_at'] <= times['starts_at'] <= times['late_at'] <= times['ends_at']
            and times['starts_at'] < times['ends_at']):
        raise ValueError('时间须满足：开放签到 ≤ 正式上课 ≤ 迟到起点 ≤ 关闭签到，正式上课须早于关闭。')
    if times['ends_at'] - times['opens_at'] > timedelta(days=1):
        raise ValueError('从开放签到到关闭签到不能超过 24 小时。')
    return times


def session_time_text(session):
    return '\n'.join(f"{title}：{session[key].replace('T', ' ')}" for key, title in
                     (('opens_at', '开放签到'), ('starts_at', '正式上课'), ('late_at', '迟到起点'), ('ends_at', '关闭签到')))
