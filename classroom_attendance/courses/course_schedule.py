"""课程模板与周期课次计划；模板存入已有 metadata，兼容现有备份格式。"""

import hashlib
import json
import re
import secrets
from datetime import date, datetime, time, timedelta

from classroom_attendance.core.task_runtime import checkpoint
from classroom_attendance.courses.session_times import resolve_session_times

TEMPLATE_PREFIX = 'course_template_v1:'
WEEKDAY_TEXT = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def normalize_course(definition):
    if not isinstance(definition, dict):
        raise ValueError('课程模板格式无效。')
    if any(not isinstance(definition.get(key, ''), str) for key in ('name', 'course_name', 'group_name')):
        raise ValueError('课程、班级和模板名称必须是文本。')
    name = definition.get('name', '').strip()
    course = definition.get('course_name', '').strip()
    group = definition.get('group_name', '').strip()
    if len(name) > 80 or not course or len(course) > 80 or not group or len(group) > 60:
        raise ValueError('请填写课程和班级；模板名、课程名不超过 80 字，班级不超过 60 字。')
    clocks = []
    for key in ('start_time', 'end_time'):
        value = definition.get(key, '')
        if not isinstance(value, str) or not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', value):
            raise ValueError('签到时间请使用 24 小时制 时:分，例如 08:00。')
        try:
            clocks.append(time.fromisoformat(value))
        except ValueError:
            raise ValueError('签到时间请使用有效的 24 小时制 时:分。') from None
    next_day = definition.get('next_day', False)
    if type(next_day) is not bool:
        raise ValueError('请选择是否次日结束。')
    start = datetime.combine(date(2000, 1, 1), clocks[0])
    end = datetime.combine(date(2000, 1, 1), clocks[1]) + timedelta(days=int(next_day))
    if not timedelta(0) < end - start <= timedelta(days=1):
        raise ValueError('签到结束须晚于开始，单次签到时长不超过 24 小时；跨日请勾选次日结束。')
    grace, interval = definition.get('late_minutes', 10), definition.get('interval_weeks', 1)
    if type(grace) is not int or not 0 <= grace <= 180:
        raise ValueError('迟到宽限应为 0 至 180 分钟。')
    if type(interval) is not int or not 1 <= interval <= 8:
        raise ValueError('周间隔应为 1 至 8 周。')
    days = definition.get('weekdays', [])
    if not isinstance(days, (list, tuple)) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError('请至少选择一个星期，范围为周一至周日。')
    ids = definition.get('student_ids')
    if ids is not None:
        if (not isinstance(ids, (list, tuple)) or not ids or
                any(not isinstance(sid, str) or not sid.isascii() or not sid.isdigit() or not 1 <= len(sid) <= 24 for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('固定选课名单须包含不重复的有效学号。')
        ids = sorted(ids)
    normalized = dict(name=name, course_name=course, group_name=group, start_time=definition['start_time'],
                end_time=definition['end_time'], next_day=next_day, late_minutes=grace,
                weekdays=sorted(set(days)), interval_weeks=interval, student_ids=ids)
    if any(key in definition for key in ('opens_time', 'late_time', 'opens_previous_day', 'late_next_day')):
        for key in ('opens_time', 'late_time'):
            value = definition.get(key)
            if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
                raise ValueError('开放签到和迟到起点请使用有效的 24 小时制 时:分。')
        for key in ('opens_previous_day', 'late_next_day'):
            if type(definition.get(key, False)) is not bool:
                raise ValueError('请选择开放签到是否在前日、迟到起点是否在次日。')
        normalized.update(opens_time=definition['opens_time'], late_time=definition['late_time'],
                          opens_previous_day=definition.get('opens_previous_day', False),
                          late_next_day=definition.get('late_next_day', False))
        course_times(normalized, date(2000, 1, 2))
    return normalized


def course_times(definition, day):
    try:
        start = datetime.combine(day, time.fromisoformat(definition['start_time']))
        end = datetime.combine(day, time.fromisoformat(definition['end_time'])) + timedelta(days=int(definition['next_day']))
        opens = (datetime.combine(day, time.fromisoformat(definition['opens_time']))
                 - timedelta(days=int(definition['opens_previous_day']))) if 'opens_time' in definition else start
        late = (datetime.combine(day, time.fromisoformat(definition['late_time']))
                + timedelta(days=int(definition['late_next_day']))) if 'late_time' in definition else None
    except OverflowError:
        raise ValueError('前日开放或次日时间超出有效日期范围。') from None
    return resolve_session_times(start, end, definition['late_minutes'], opens_at=opens, late_at=late)


def schedule_date(value):
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError('日期请使用有效的 年-月-日。')


def expand_course(definition, first_date, last_date, excluded_dates=()):
    definition = normalize_course(definition)
    first, last = schedule_date(first_date), schedule_date(last_date)
    if not 0 <= (last-first).days <= 365:
        raise ValueError('生成日期须按先后排列，含首尾不超过 366 天。')
    excluded = {schedule_date(value).isoformat() for value in excluded_dates}
    anchor = first - timedelta(days=first.weekday())
    rows = []
    for offset in range((last-first).days + 1):
        day = first + timedelta(days=offset)
        if day.weekday() not in definition['weekdays'] or ((day-anchor).days // 7) % definition['interval_weeks']:
            continue
        times = course_times(definition, day)
        rows.append(dict(date=day.isoformat(), weekday=day.weekday(), **{key: value.isoformat() for key, value in times.items()},
                         included=day.isoformat() not in excluded))
    if not rows:
        raise ValueError('该范围内没有符合星期和周间隔的课次，请调整日期。')
    if excluded - {row['date'] for row in rows}:
        raise ValueError('跳过日期必须是当前周期计划中的日期，请重新预览。')
    return rows


class CourseScheduleStore:
    """由 AttendanceStore 复用连接、名单与课次写入，无额外数据库文件。"""

    @staticmethod
    def _template_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
            raise ValueError('课程模板编号无效。')
        return value

    def _course_templates(self, connection):
        result = []
        for row in connection.execute('SELECT key,value FROM metadata WHERE key GLOB ?', (TEMPLATE_PREFIX + '*',)):
            try:
                item = json.loads(row['value'])
                definition = normalize_course(item['definition'])
                if (type(item['version']) is not int or item['version'] not in (1, 2) or type(item['revision']) is not int or item['revision'] < 1
                        or item['id'] != row['key'][len(TEMPLATE_PREFIX):] or not definition['name']):
                    raise ValueError()
                if item['version'] == 2 and 'opens_time' not in definition:
                    raise ValueError()
                self._template_id(item['id'])
            except (KeyError, TypeError, ValueError):
                raise ValueError('课程模板信息损坏，请从已校验的备份恢复。') from None
            result.append(dict(item, definition=definition))
        return sorted(result, key=lambda item: (item['definition']['name'], item['id']))

    def course_templates(self):
        with self.connect() as connection:
            return self._course_templates(connection)

    def save_course_template(self, definition, *, template_id=None, revision=None, actor=''):
        definition = normalize_course(definition)
        if not definition['name']:
            raise ValueError('保存模板前请填写模板名称。')
        with self.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            templates = self._course_templates(connection)
            previous = next((item for item in templates if item['id'] == template_id), None)
            if template_id is not None:
                self._template_id(template_id)
                if not previous or type(revision) is not int or revision != previous['revision']:
                    raise ValueError('课程模板已变化，请重新加载后保存。')
            if any(item['definition']['name'].casefold() == definition['name'].casefold() and item['id'] != template_id for item in templates):
                raise ValueError('该模板名称已存在，请加载原模板或使用不同名称。')
            # 固定名单保存时也复核；按班级模式允许先建立尚无学生的模板。
            if definition['student_ids'] is not None:
                self._session_members(connection, definition['group_name'], definition['student_ids'])
            if previous and previous['definition'] == definition:
                return previous
            item = dict(version=2 if 'opens_time' in definition else 1, id=template_id or secrets.token_hex(8), revision=previous['revision'] + 1 if previous else 1,
                        definition=definition, updated_at=datetime.now().isoformat(timespec='seconds'))
            self._set(connection, TEMPLATE_PREFIX + item['id'], json.dumps(item, ensure_ascii=False, sort_keys=True))
            self._audit(connection, '更新课程模板' if previous else '创建课程模板', detail=definition['name'],
                        actor=actor, before=previous, after=item)
            return item

    def _course_plan(self, connection, definition, first_date, last_date, excluded_dates):
        definition = normalize_course(definition)
        rows = expand_course(definition, first_date, last_date, excluded_dates)
        members = self._session_members(connection, definition['group_name'], definition['student_ids'])
        existing = [dict(row) for row in connection.execute("""SELECT id,course_name,group_name,opens_at,starts_at,late_at,ends_at,state
            FROM sessions WHERE state<>'cancelled' AND opens_at<? AND ends_at>? ORDER BY opens_at,id""",
            (max(row['ends_at'] for row in rows), min(row['opens_at'] for row in rows)))]
        for row in rows:
            checkpoint()
            row['conflicts'] = [item for item in existing if row['opens_at'] < item['ends_at'] and row['ends_at'] > item['opens_at']]
        context = dict(definition=definition, rows=rows, members=members)
        token = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        included = [row for row in rows if row['included']]
        return dict(context, token=token, total=len(rows), included=len(included),
                    conflict_count=sum(bool(row['conflicts']) for row in included))

    def preview_course_schedule(self, definition, first_date, last_date, *, excluded_dates=()):
        with self.connect() as connection:
            connection.execute('BEGIN')
            return self._course_plan(connection, definition, first_date, last_date, excluded_dates)

    def create_course_schedule(self, definition, first_date, last_date, token, *, excluded_dates=(), actor=''):
        with self.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            plan = self._course_plan(connection, definition, first_date, last_date, excluded_dates)
            if not token or token != plan['token']:
                raise ValueError('周期计划、名单或时段占用已变化，请重新预览后生成。')
            if not plan['included']:
                raise ValueError('至少保留一个要生成的日期。')
            if plan['conflict_count']:
                raise ValueError('仍有日期与已有课次冲突，请先跳过或调整后重新预览。')
            definition = plan['definition']
            ids = []
            for row in plan['rows']:
                checkpoint()
                if row['included']:
                    ids.append(self._insert_session(connection, definition['course_name'], definition['group_name'],
                                                    datetime.fromisoformat(row['starts_at']), datetime.fromisoformat(row['ends_at']),
                                                    definition['late_minutes'], plan['members'], actor=actor,
                                                    opens_at=datetime.fromisoformat(row['opens_at']), late_at=datetime.fromisoformat(row['late_at'])))
            self._audit(connection, '批量创建周期课次', detail=f"{definition['course_name']}；创建 {len(ids)} 次，跳过 {plan['total']-len(ids)} 次",
                        actor=actor, after=dict(session_ids=ids, dates=[r['date'] for r in plan['rows'] if r['included']],
                                               template_name=definition['name']))
            return ids
