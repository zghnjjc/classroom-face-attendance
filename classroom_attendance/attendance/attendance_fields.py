"""界面、统计和导出共用的考勤语义；各报表保留自己的布局和日期格式。"""

from datetime import datetime

STATUS_TEXT = {"present": "已签到", "late": "迟到", "leave": "请假", "absent": "缺勤",
               "pending": "未签到", "not_started": "待开启", "cancelled": "已取消", "excluded": "已移出应到"}
ARRIVED_STATUSES = ('present', 'late')
# 只来自上面的程序常量，绝不接收用户输入。
ARRIVED_STATUS_SQL = ','.join("'" + status + "'" for status in ARRIVED_STATUSES)
METHOD_TEXT = {'face': ('人脸识别', '人脸'), 'manual': ('人工更正', '人工'), 'legacy': ('历史迁移', '迁移')}


def method_text(method, *, short=False, default=''):
    labels = METHOD_TEXT.get(method)
    return labels[int(short)] if labels else default


def is_time_unknown(row):
    return row['status'] in ARRIVED_STATUSES and not row['signed_at']


def arrival_time_note(row):
    return '时间未确认' if is_time_unknown(row) else ''


def excel_time(value):
    return datetime.fromisoformat(value) if value else None


def attendance_base_values(row):
    """两种普通考勤导出共有的字段，不包括序号、证据与课次时间。"""
    return [row['student_id'], row['name'], row['group_name'], STATUS_TEXT[row['status']],
            excel_time(row['signed_at']), method_text(row['method']), row['note'] or '']
