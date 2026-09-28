import json
from datetime import datetime
from dataclasses import asdict


SESSION_PREFIX = "WATCHER_SESSION "
GPU_PREFIX = "WATCHER_GPUS "
REPORT_PREFIX = "WATCHER_REPORT "


def emit_session(status, message):  # 只传会话状态与核验时间，不传令牌或浏览器资料。
    payload = {"status": status, "message": message,
               "checked_at": datetime.now().isoformat(timespec="seconds")}
    print(SESSION_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def emit_gpu_samples(samples, hosts, stale_after_seconds, error=""):  # 图表只展示当前账号可见主机；复用本轮数据，不额外轮询。
    visible = {host.host for host in hosts}
    rows = [dict(asdict(sample), observed_at=sample.observed_at.isoformat())
            for sample in samples if sample.host in visible]
    print(GPU_PREFIX + json.dumps({"samples": rows, "stale_after_seconds": stale_after_seconds,
                                  "error": error}, ensure_ascii=False), flush=True)
