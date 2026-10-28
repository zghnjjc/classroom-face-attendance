"""教师首页快照和课后核对标记；沿用考勤状态及本地操作记录。"""

import hashlib
import json
from collections import Counter
from datetime import datetime

from task_runtime import checkpoint
from audit_actions import REVIEW_ACTIONS
from attendance_fields import ARRIVED_STATUSES, ARRIVED_STATUS_SQL, is_time_unknown

REVIEW_PREFIX = 'session_review_v1:'
BACKUP_KEY = 'last_backup_v1'
REVIEW_LIMIT = 30


def attendance_counts(rows):
    included = [row for row in rows if row['included']]
    counts = Counter(row['status'] for row in included)
    return dict(expected=len(included), arrived=sum(counts[status] for status in ARRIVED_STATUSES), present=counts['present'],
                late=counts['late'], leave=counts['leave'], absent=counts['absent'], pending=counts['pending'],
                unknown_time=sum(is_time_unknown(row) for row in included))


class TeacherDashboardStore:
    @staticmethod
    def _review_entry(raw):
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                    or type(value.get('revision')) is not int or value['revision'] < 0
                    or not isinstance(value.get('token'), str) or len(value['token']) != 64
                    or any(ch not in '0123456789abcdef' for ch in value['token'])
                    or not isinstance(value.get('actor'), str) or len(value['actor']) > 80
                    or not isinstance(value.get('note'), str) or len(value['note']) > 300
                    or not isinstance(value.get('reviewed_at'), str)
                    or datetime.fromisoformat(value['reviewed_at']).tzinfo is not None):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            return {'invalid': True}

    @staticmethod
    def _review_revision(connection, session_id):
        marks = ','.join('?' for _ in REVIEW_ACTIONS)
        return connection.execute(f'SELECT COALESCE(MAX(id),0) FROM audit_log WHERE session_id=? AND action IN ({marks})',
                                  (session_id, *REVIEW_ACTIONS)).fetchone()[0]

    def _session_review_plan(self, connection, session_id, now):
        session = self._session(connection, session_id)
        if session['state'] != 'closed':
            raise ValueError('只有已结束课次可以确认核对完成；请先结束签到。')
        rows = self._session_rows(connection, session_id, now, include_removed=True)
        revision = self._review_revision(connection, session_id)
        context = dict(session=session, rows=rows, revision=revision)
        review_session = dict(session)
        if review_session['opens_at'] == review_session['starts_at']:
            review_session.pop('opens_at')
        review_rows = [{key: value for key, value in row.items() if key not in {'session_opens_at', 'session_late_at'}} for row in rows]
        fingerprint = dict(session=review_session, rows=review_rows, revision=revision)
        token = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = self._review_entry(self._get(connection, REVIEW_PREFIX + str(session_id)))
        valid = bool(previous and not previous.get('invalid') and previous['token'] == token and previous['revision'] == revision)
        return dict(context, token=token, counts=attendance_counts(rows), previous=previous, reviewed=valid)

    def preview_session_review(self, session_id, now=None):
        now = now or datetime.now()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._expire(connection, now)
            return self._session_review_plan(connection, session_id, now)

    def mark_session_reviewed(self, session_id, token, *, actor='', note='', now=None):
        actor, note = actor.strip(), note.strip()
        if len(actor) > 80 or len(note) > 300:
            raise ValueError('操作署名不超过 80 字，核对说明不超过 300 字。')
        now = now or datetime.now()
        with self.connect(write=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._expire(connection, now)
            plan = self._session_review_plan(connection, session_id, now)
            if not token or token != plan['token']:
                raise ValueError('课次、名单或考勤已变化，请刷新核对明细后重新确认。')
            previous = plan['previous']
            if plan['reviewed'] and previous['actor'] == actor and previous['note'] == note:
                return False
            value = dict(version=1, token=token, revision=plan['revision'], actor=actor, note=note,
                         reviewed_at=now.replace(microsecond=0).isoformat())
            self._set(connection, REVIEW_PREFIX + str(session_id), json.dumps(value, ensure_ascii=False, sort_keys=True))
            counts = plan['counts']
            previous_summary = ({'reviewed_at': previous['reviewed_at'], 'actor': previous['actor'], 'note': previous['note']}
                                if previous and not previous.get('invalid') else {'note': '旧标记不可读'} if previous else None)
            self._audit(connection, '确认课次核对完成', session_id,
                        detail=f"应到 {counts['expected']} 人；已到 {counts['arrived']}；请假 {counts['leave']}；缺勤 {counts['absent']}；时间未确认 {counts['unknown_time']}",
                        actor=actor, before=previous_summary,
                        after=dict(reviewed_at=value['reviewed_at'], note=note, **counts))
            return True

    def last_backup(self):
        with self.connect() as connection:
            return self._backup_entry(self._get(connection, BACKUP_KEY))

    @staticmethod
    def _backup_entry(raw):
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or not isinstance(value.get('completed_at'), str)
                    or datetime.fromisoformat(value['completed_at']).tzinfo is not None):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            return {'invalid': True}

    def record_backup_success(self, *, completed_at=None):
        value = dict(completed_at=(completed_at or datetime.now()).replace(microsecond=0).isoformat())
        with self.connect(write=True) as connection:
            self._set(connection, BACKUP_KEY, json.dumps(value, sort_keys=True))
        return value

    def teacher_dashboard(self, now=None):
        now = now or datetime.now()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._expire(connection, now)
            fields = [('present', 'present'), ('late', 'late'), ('leave', 'leave'), ('absent', 'absent'), ('pending', 'pending')]
            counters = ','.join(f"SUM(CASE WHEN a.included=1 AND a.status='{status}' THEN 1 ELSE 0 END) AS {name}" for name, status in fields)
            marks = ','.join(f':action{index}' for index in range(len(REVIEW_ACTIONS)))
            query = f"""WITH attendance_rows AS ({self.attendance_row_sql}), changes AS (
                SELECT session_id,MAX(id) AS revision FROM audit_log WHERE action IN ({marks}) GROUP BY session_id)
                SELECT s.*, COALESCE(changes.revision,0) AS review_revision,
                SUM(CASE WHEN a.included=1 THEN 1 ELSE 0 END) AS roster_count,
                SUM(CASE WHEN a.included=1 AND a.status IN ('present','late','leave','absent','pending') THEN 1 ELSE 0 END) AS expected,
                {counters}, SUM(CASE WHEN a.included=1 AND a.status IN ({ARRIVED_STATUS_SQL}) AND a.signed_at IS NULL THEN 1 ELSE 0 END) AS unknown_time
                FROM sessions s LEFT JOIN attendance_rows a ON s.id=a.session_id
                LEFT JOIN changes ON changes.session_id=s.id GROUP BY s.id ORDER BY s.starts_at,s.id"""
            parameters = {'now': now.replace(microsecond=0).isoformat()}
            parameters.update({f'action{index}': action for index, action in enumerate(REVIEW_ACTIONS)})
            sessions = [dict(row) for row in connection.execute(query, parameters)]
            reviews = {row['key'][len(REVIEW_PREFIX):]: self._review_entry(row['value'])
                       for row in connection.execute('SELECT key,value FROM metadata WHERE key GLOB ?', (REVIEW_PREFIX + '*',))}
            for row in sessions:
                checkpoint()
                row['arrived'] = sum(row[status] for status in ARRIVED_STATUSES)
                review = reviews.get(str(row['id']))
                row['review'] = review
                row['reviewed'] = bool(row['state'] == 'closed' and review and not review.get('invalid') and review['revision'] == row['review_revision'])
                row['review_text'] = ('已核对' if row['reviewed'] else '标记不可读，需核对' if review and review.get('invalid') else
                                      '变更后待复核' if review else '待核对') if row['state'] == 'closed' else '结束后核对'
            today = [row for row in sessions if row['starts_at'][:10] == now.date().isoformat()]
            active = next((row for row in sessions if row['state'] == 'open' and row['opens_at'] <= parameters['now'] < row['ends_at']), None)
            missing = [row for row in self._session_rows(connection, active['id'], now) if row['status'] == 'pending'] if active else []
            pending = sorted((row for row in sessions if row['state'] == 'closed' and not row['reviewed']), key=lambda row: (row['ends_at'], row['id']), reverse=True)
            return dict(date=now.date().isoformat(), today=today, active=active, missing=missing,
                        totals={key: sum(row[key] for row in today) for key in ('expected', 'arrived', 'late', 'leave', 'absent', 'pending')},
                        pending_reviews=pending[:REVIEW_LIMIT], pending_review_count=len(pending), review_limit=REVIEW_LIMIT,
                        last_backup=self._backup_entry(self._get(connection, BACKUP_KEY)))
