import json
from datetime import datetime


SESSION_PREFIX = "WATCHER_SESSION "


def emit_session(status, message):  # 只传会话状态与核验时间，不传令牌或浏览器资料。
    payload = {"status": status, "message": message,
               "checked_at": datetime.now().isoformat(timespec="seconds")}
    print(SESSION_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
