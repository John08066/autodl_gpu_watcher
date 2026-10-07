"""可验证的日志提取规则。仅解释字段路径和文本占位符，不执行模型代码。"""
from __future__ import annotations

import ast
import json
import math
import re

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
FIELDS = {"epoch", "total_epochs", "step", "total_steps", "pid", "dataset", "time"}
STAMP = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?")


class RuleError(ValueError):
    pass


def template_pattern(template):
    parts, offset, names = [], 0, set()
    for match in re.finditer(r"\{([A-Za-z_]\w*):(number|word|json)\}", template):
        name, kind = match.groups()
        if name in names or len(names) >= 12:
            raise RuleError("文本占位符重复或过多")
        names.add(name)
        literal = template[offset:match.start()]
        parts.append(re.sub(r"(?:\\ )+", r"\\s+", re.escape(literal)))
        parts.append("(?P<" + name + ">" + {"number": NUMBER, "word": r"[^\s]+", "json": r"\{.*\}"}[kind] + ")")
        offset = match.end()
    if not names or "{" in template[offset:] or "}" in template[offset:]:
        raise RuleError("文本模板须使用 {字段:number/word/json} 占位符")
    parts.append(re.escape(template[offset:]))
    return re.compile("".join(parts))


def dictionary(text):
    try:
        value = json.loads(text)
    except ValueError:
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
    return value if isinstance(value, dict) else None


def field(values, path):
    if path in values:  # 带斜线或点号的原指标名优先保持完整。
        return values[path]
    value = values
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def numbers(values, prefix=""):
    result = {}
    for key, value in (values.items() if isinstance(values, dict) else ()):
        name = prefix + str(key)
        if isinstance(value, dict):
            result.update(numbers(value, name + "/"))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result[name] = value
    return result


def extract(rule, line, pid=None):
    if rule.get("contains") and rule["contains"] not in line:
        return None
    if rule["format"] == "json":
        start = line.find("{")
        values = dictionary(line[start:]) if start >= 0 else None
    else:
        match = template_pattern(rule["template"]).search(line)
        values = match.groupdict() if match else None
        if values:
            for name, value in list(values.items()):
                if value.startswith("{"):
                    values[name] = dictionary(value)
                elif re.fullmatch(NUMBER, value):
                    values[name] = float(value) if "." in value or "e" in value.lower() else int(value)
    if not values or any(field(values, name) != value for name, value in rule.get("where", {}).items()):
        return None
    actual_pid = field(values, rule.get("fields", {}).get("pid", "pid"))
    if pid is not None and actual_pid is not None and actual_pid != pid:
        return None
    event = {"event": "progress", "phase": rule["phase"]}
    for name, path in rule.get("fields", {}).items():
        value = field(values, path)
        if name in {"dataset", "time"}:
            if isinstance(value, str):
                event[name] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            event[name] = value + rule.get("epoch_offset", 0) if name == "epoch" else value
    if actual_pid is not None:
        event["pid"] = actual_pid
    if "time" not in event and (stamp := STAMP.search(line)):
        event["time"] = stamp[0]
    if "dataset" not in event:
        event["dataset"] = rule.get("dataset", "validation")
    metrics = numbers(field(values, rule["metrics_path"])) if rule.get("metrics_path") else {}
    for name, path in rule.get("metrics", {}).items():
        value = field(values, path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[name] = value
    event["metrics"] = metrics
    return event if metrics or any(name in event for name in ("epoch", "step")) else None


def validate_profile(value, source, pid=None):
    if not isinstance(value, dict) or set(value) - {"name", "rules"} or not isinstance(value.get("name"), str):
        raise RuleError("规则须包含名称和 rules，不能包含代码或命令")
    rules = value.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
        raise RuleError("规则数量须为1到8条")
    for rule in rules:
        allowed = {"format", "contains", "where", "template", "phase", "fields", "metrics", "metrics_path", "dataset", "epoch_offset", "example"}
        if not isinstance(rule, dict) or set(rule) - allowed or rule.get("format") not in {"json", "text"} or rule.get("phase") not in {"train", "test", "unknown"}:
            raise RuleError("规则类型、阶段或字段无效")
        for key in ("contains", "template", "metrics_path", "dataset", "example"):
            if key in rule and (not isinstance(rule[key], str) or len(rule[key]) > 4000):
                raise RuleError("规则文本字段无效或过长")
        for key in ("fields", "metrics", "where"):
            if not isinstance(rule.get(key, {}), dict) or len(rule.get(key, {})) > 30:
                raise RuleError("规则字段映射无效")
        if set(rule.get("fields", {})) - FIELDS:
            raise RuleError("进度字段名称无效")
        if not all(isinstance(k, str) and isinstance(v, str) and 0 < len(v) <= 200
                   for key in ("fields", "metrics") for k, v in rule.get(key, {}).items()):
            raise RuleError("字段必须引用日志路径，不能写死数值")
        if not all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in rule.get("where", {}).items()):
            raise RuleError("行筛选条件无效")
        if rule.get("epoch_offset", 0) not in (0, 1):
            raise RuleError("仅支持原Epoch或零起点加1")
        if rule["format"] == "text":
            template_pattern(rule.get("template", ""))
        example = rule.get("example", "")
        if not example or example not in source.splitlines() or not extract(rule, example, pid):
            raise RuleError("规则无法从所附真实日志样例提取字段")
    return value


def parse_rules(profile, text, pid, *, with_lines=False):
    events = []
    context_pid = None
    for index, line in enumerate(text.replace("\r", "\n").splitlines()):
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        if len(line) > 16384:
            continue
        start = line.find("{")
        raw = dictionary(line[start:]) if start >= 0 else None
        if raw and "pid" in raw:
            context_pid = raw["pid"]
        for rule in profile["rules"]:
            event = extract(rule, line, pid)
            if event:
                if "pid" in event:
                    context_pid = event["pid"]
                elif context_pid is not None and context_pid != pid:
                    break
                events.append((index, event) if with_lines else event)
                break
    return events


def task_identity(server, task):
    return json.dumps([server.id, server.ssh_alias, server.project, server.python, server.scripts, task["key"]], ensure_ascii=False)


class RuleStore:
    def __init__(self, path):
        self.path = path
        self.error = ""
        try:
            self.entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(self.entries, dict):
                raise ValueError()
        except (ValueError, OSError):
            self.entries = {}
            self.error = "规则缓存无法读取；默认监控，暂停自动生成以免重复请求"

    def put(self, identity, entry):
        entries = {**self.entries, identity: entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
        self.entries = entries
