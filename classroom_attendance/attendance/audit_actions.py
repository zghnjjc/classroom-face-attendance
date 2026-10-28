"""稳定的审计入库值与可独立修改的界面名称；兼容已有中文历史记录。"""

CORRECT_ATTENDANCE = '人工更正'
FACE_CHECKIN = '人脸签到'
SAVE_FACE_EVIDENCE = '保存首次人脸证据'
CHANGE_ROSTER = '调整课次名单'
CLOSE_SESSION = '结束签到'
EXPIRE_SESSION = '课次到时关闭'
OPEN_SESSION = '开启签到'
CANCEL_SESSION = '取消课次'

REVIEW_ACTIONS = (CORRECT_ATTENDANCE, FACE_CHECKIN, SAVE_FACE_EVIDENCE,
                  CHANGE_ROSTER, CLOSE_SESSION, EXPIRE_SESSION)
SESSION_ACTIONS = (CANCEL_SESSION, CHANGE_ROSTER, CLOSE_SESSION, OPEN_SESSION, EXPIRE_SESSION)
ACTION_TEXT = {action: action for action in (*REVIEW_ACTIONS, OPEN_SESSION, CANCEL_SESSION)}


def action_text(action):
    """只改变展示名称，不改变历史查询、去重及核对失效使用的入库标识。"""
    return ACTION_TEXT.get(action, action)
