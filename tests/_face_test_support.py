from datetime import datetime

from classroom_attendance.faces.face_lifecycle import FaceLifecycleService


def authorize_faces(store):
    service = FaceLifecycleService(store)
    now = datetime(2000, 1, 1)
    if service.snapshot(now)['notice'] is None:
        service.publish_notice('synthetic-test-v1', '合成测试告知：仅供临时数据库的回归测试，不使用真实人脸或真实学生资料。', confirmed=True, now=now)
    for student in service.snapshot(now)['students']:
        if student['state'] == 'valid':
            continue
        plan = service.authorization_plan(student['student_id'], now=now)
        service.grant(student['student_id'], plan['token'], '2099-12-31', '本人明确同意', 'synthetic-test-record', confirmed=True, now=now)
