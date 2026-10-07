"""独立 AI 用量账本；只保存用量和计价快照，不保存密钥或日志。"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
from urllib.parse import urlsplit

PRICE_FIELDS = ("input", "cached", "write", "output", "flex_multiplier", "usd_cny")
LUNA_PRICES = dict(input=.10, cached=.01, write=.125, output=.50, flex_multiplier=.5, usd_cny=7.0)


def pricing_settings(config):
    pricing = config.get("pricing")
    if pricing is None:
        pricing = ({**LUNA_PRICES, "model": config["model"], "base_url": config["base_url"]}
                   if config["model"] == "gpt-6-luna" and config["base_url"] == "https://api.openai.com/v1" else {})
    result = {name: str(pricing.get(name, "")) for name in ("model", "base_url")}
    for name in PRICE_FIELDS:
        value = pricing.get(name)
        if value is None or value == "":
            result[name] = None
            continue
        value = float(value)
        if not math.isfinite(value) or value < 0 or (name in {"flex_multiplier", "usd_cny"} and value == 0):
            raise ValueError("计价数值须为有限非负数；汇率和 Flex 系数须大于零")
        result[name] = value
    return result


def tokens(usage):
    usage = usage or {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return {"input": count(usage.get("input_tokens", usage.get("prompt_tokens"))),
            "output": count(usage.get("output_tokens", usage.get("completion_tokens"))),
            "cached": count(details.get("cached_tokens")), "write": count(details.get("cache_write_tokens"))}


def estimate(call, config):
    pricing = dict(config["pricing"])
    result = {"pricing": pricing, "usd": None, "cny": None, "cost_note": ""}
    usage = call["tokens"]
    if usage["input"] is None or usage["output"] is None:
        return dict(result, cost_note="用量未返回，费用未知")
    if pricing["model"] != config["model"] or pricing["base_url"] != config["base_url"]:
        return dict(result, cost_note="此模型/地址未配置单价")
    actual_model = call.get("actual_model")
    if actual_model and actual_model != config["model"] and not actual_model.startswith(config["model"] + "-"):
        return dict(result, cost_note="返回模型与计价模型不同，费用未知")
    if any(pricing[name] is None for name in PRICE_FIELDS):
        return dict(result, cost_note="单价或汇率未配置完整")
    tier = call.get("actual_tier") or config["service_tier"]
    if tier not in {"default", "flex"}:
        return dict(result, cost_note="返回等级无对应计价，费用未知")
    notes = []
    if not call.get("actual_tier"):
        notes.append("等级未返回，按请求等级估算")
    if usage["cached"] is None or usage["write"] is None:
        notes.append("缓存明细缺项暂按0估算")
    hit, written = usage["cached"] or 0, usage["write"] or 0
    if hit + written > usage["input"]:
        return dict(result, cost_note="缓存用量超过总输入，费用未知")
    tier_factor = pricing["flex_multiplier"] if tier == "flex" else 1
    # 官方 Luna 超长上下文加价；其它模型按用户所填单价估算。
    long = (urlsplit(config["base_url"]).hostname == "api.openai.com" and
            config["model"] == "gpt-6-luna" and usage["input"] > 272000)
    cost = (((usage["input"] - hit - written) * pricing["input"] + hit * pricing["cached"] + written * pricing["write"])
            * (2 if long else 1) + usage["output"] * pricing["output"] * (1.5 if long else 1)) * tier_factor / 1_000_000
    return dict(result, usd=cost, cny=cost * pricing["usd_cny"], cost_note="；".join(notes) or "按返回用量估算")


def call_text(call):
    if not call:
        return "本次用量：未记录；旧版本调用无法补算。"
    usage = call["tokens"]
    number = lambda name: f"{usage[name]:,}" if usage[name] is not None else "未知"
    cost = f"${call['usd']:.6f} / ¥{call['cny']:.6f}" if call.get("usd") is not None else "未知"
    return (f"Token 输入 {number('input')} · 输出 {number('output')} · 缓存命中 {number('cached')} · 写入 {number('write')}\n"
            f"估计消费 {cost}；{call.get('cost_note', '')}")


class UsageStore:
    def __init__(self, path):
        self.path = path

    def record(self, call):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (time REAL NOT NULL, record TEXT NOT NULL)")
            db.execute("INSERT INTO calls VALUES (?, ?)", (call["time"], json.dumps(call, ensure_ascii=False)))

    def records(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path)) as db, db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM calls ORDER BY time DESC")]

    def summary(self):
        records = self.records()
        today = datetime.now(timezone(timedelta(hours=8))).date()
        lines = ["Token 与估计消费 · 所有服务器共用", "历史未记录调用无法补算；金额仅为估计，以服务商账单为准。"]
        for name, selected in (("最近一次", records[:1]), ("今日（UTC+8）", [r for r in records if datetime.fromtimestamp(r["time"], timezone(timedelta(hours=8))).date() == today]), ("累计", records)):
            missing = sum(r.get("cny") is None for r in selected)
            token_totals = []
            for field, label in (("input", "输入"), ("output", "输出"), ("cached", "缓存命中"), ("write", "写入")):
                values = [r["tokens"][field] for r in selected]
                unknown = sum(v is None for v in values)
                token_totals.append(f"{label} {sum(v for v in values if v is not None):,}" + (f"（{unknown}笔未知）" if unknown else ""))
            lines.append(f"\n{name}：{len(selected)}次请求 · " + " · ".join(token_totals))
            lines.append(f"已知费用估计 ¥{sum(r.get('cny') or 0 for r in selected):.6f}" + (f"；另有{missing}笔费用未知" if missing else ""))
        lines.append("\n最近调用（最多50条）：")
        for call in records[:50]:
            stamp = datetime.fromtimestamp(call["time"]).strftime("%m-%d %H:%M:%S")
            lines.append(f"\n{stamp} · {call['model']} · 实际模型 {call.get('actual_model') or '未返回'} · 请求 {call['requested_tier']} / 实际 {call.get('actual_tier') or '未返回'} · {call['status']}")
            lines.append(call_text(call))
        return "\n".join(lines)
