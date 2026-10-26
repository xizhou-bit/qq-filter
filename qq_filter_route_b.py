"""
QQ 群重要消息过滤 - 路线 B (只读, 不在群里发言)

流程:
  SnowLuma (OneBot v11 WebSocket) -> 本地粗筛 (白名单群 + 关键词/发言人/全量AI)
  -> 候选调 DeepSeek / OpenAI 兼容 API 精筛
  -> 重要结果写 today_useful.jsonl, 白名单群消息写 recent_messages.jsonl (供看板点选发言人)
  -> 按配置批量/紧急/夜间静默推送 (WxPusher / QQ 私聊只发自己 / 邮件 / 只看本地)

配置: config.json (gitignored), 密钥只走本地 secrets.json (gitignored), 绝不进代码或配置.
看板: dashboard.py (独立进程), 只跑 127.0.0.1, 不接管本脚本.
"""

import asyncio
import hashlib
import json
import os
import re
import smtplib
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import requests
import websockets

# ---------------- 路径与常量 ----------------
CONFIG_PATH = os.getenv("QQFILTER_CONFIG", "config.json")
OUT_FILE = os.getenv("QQFILTER_OUT", "today_useful.jsonl")
RECENT_FILE = os.getenv("QQFILTER_RECENT", "recent_messages.jsonl")
PUSH_STATE_FILE = os.getenv("QQFILTER_PUSH_STATE", "push_state.json")
SECRETS_FILE = os.getenv("QQFILTER_SECRETS", "secrets.json")

ONEBOT_WS = os.getenv("ONEBOT_WS", "ws://127.0.0.1:3001")

DEDUP_WINDOW = 300                 # 同群同人同文本 5 分钟去重
CROSS_GROUP_DEDUP_WINDOW = 6 * 3600
RECENT_LIMIT = 500                 # recent_messages 每群保留条数
BATCH_TICK_SECONDS = 60            # 批量检查周期
SUMMARY_MAX = 20                   # 单条摘要最多条数
EMERGENCY_HOURS = 6                # deadline_dt 距现在 <=6 小时视为紧急

# 内置默认关键词, 用于新建群时预填. 不含任何密钥或 QQ 号.
DEFAULT_KEYWORDS = (
    "作业", "考试", "截止", "交", "通知", "报名", "点名", "上课", "调课",
    "答辩", "开题", "结课", "补考", "重修", "缓考", "实验", "组会", "课题", "研讨", "讨论班",
    "讲座", "比赛", "活动", "宣讲", "报告", "招聘", "实习", "奖学金",
    "选课", "会议", "集合", "选拔", "集训", "保研", "推免", "夏令营", "四六级", "证书",
    "班会", "团日", "党课", "评优", "评奖", "公示",
    "教务", "缴费", "宿舍", "返校", "放假", "调休",
    "务必", "紧急", "重要", "请注意", "请查收", "周知",
)

DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com/chat/completions"
OPENAI_COMPATIBLE_DEFAULT_URL = "https://api.openai.com/v1/chat/completions"

EMERGENCY_WORDS = ("紧急", "马上", "立即", "尽快", "速")

# 所有可选的密钥/凭据. 统一从 secrets.json(本地, gitignored) 读取.
SECRET_KEYS = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "WXPUSHER_APP_TOKEN",
    "WXPUSHER_UID",
    "SELF_QQ",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "MAIL_TO",
)

# 敏感字段: 页面默认遮罩, 点眼睛才取真值.
SENSITIVE_KEYS = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "WXPUSHER_APP_TOKEN",
    "SMTP_PASS",
)

_secrets_cache = None


def load_secrets():
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: str(data.get(k) or "") for k in SECRET_KEYS}
        except Exception:
            pass
    return {k: "" for k in SECRET_KEYS}


def save_secrets(data):
    """只更新传入的键, 未传入的键保持原值; 空字符串表示清除."""
    existing = load_secrets()
    if isinstance(data, dict):
        for k in SECRET_KEYS:
            if k in data:
                existing[k] = str(data.get(k) or "")
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SECRETS_FILE)
    global _secrets_cache
    _secrets_cache = existing
    return existing


def get_secret(name):
    """只读本地 secrets.json (gitignored), 不看环境变量."""
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = load_secrets()
    return _secrets_cache.get(name, "")


def secret_status():
    """返回每个密钥是否已配置, 不含任何明文."""
    return {k: bool(get_secret(k)) for k in SECRET_KEYS}


# ---------------- 配置加载 ----------------

def default_config():
    return {
        "groups": [],
        "snow_luma_dir": "",
        "close_snowluma_on_exit": False,
        "global": {"important_senders": [], "muted_senders": []},
        "model": {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "base_url": DEEPSEEK_DEFAULT_URL,
        },
        "push": {
            "channels": ["wxpusher"],
            "batch_interval_minutes": 60,
            "night_quiet": {"enabled": True, "start": "23:00", "end": "08:00"},
        },
        "dashboard": {"refresh_seconds": 30},
    }


def _int_list(value):
    seen = set()
    out = []
    for x in value or []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _int_range(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _time_str(value, default):
    s = str(value or "")
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return s
    return default


def normalize_config(cfg):
    """补全字段、校验类型, 返回规范化后的配置. 不含任何密钥字段."""
    if not isinstance(cfg, dict):
        cfg = {}
    result = {
        "groups": [],
        "snow_luma_dir": str(cfg.get("snow_luma_dir") or ""),
        "close_snowluma_on_exit": bool(cfg.get("close_snowluma_on_exit", False)),
        "global": {"important_senders": [], "muted_senders": []},
        "model": {"provider": "deepseek", "model": "deepseek-flash", "base_url": DEEPSEEK_DEFAULT_URL},
        "push": {"channels": ["wxpusher"], "batch_interval_minutes": 60,
                 "night_quiet": {"enabled": True, "start": "23:00", "end": "08:00"}},
        "dashboard": {"refresh_seconds": 30},
    }

    gbl = cfg.get("global") or {}
    result["global"] = {
        "important_senders": _int_list(gbl.get("important_senders")),
        "muted_senders": _int_list(gbl.get("muted_senders")),
    }

    for g in cfg.get("groups") or []:
        if not isinstance(g, dict) or g.get("id") is None:
            continue
        try:
            gid = int(g["id"])
        except (TypeError, ValueError):
            continue
        mode = g.get("mode")
        if mode not in ("keyword", "full_ai"):
            mode = "keyword"
        group = {
            "id": gid,
            "name": str(g.get("name") or f"群{gid}"),
            "enabled": bool(g.get("enabled", True)),
            "mode": mode,
            "keywords": [str(k) for k in (g.get("keywords") or [])],
            "important_senders": _int_list(g.get("important_senders")),
            "muted_senders": _int_list(g.get("muted_senders")),
        }
        if "keywords" not in g:
            group["keywords"] = list(DEFAULT_KEYWORDS)
        result["groups"].append(group)

    model = cfg.get("model") or {}
    provider = model.get("provider")
    if provider not in ("deepseek", "openai_compatible", "subscription"):
        provider = "deepseek"
    model_name = str(model.get("model") or "deepseek-flash")
    base_url = str(model.get("base_url") or "")
    if not base_url:
        base_url = DEEPSEEK_DEFAULT_URL if provider == "deepseek" else OPENAI_COMPATIBLE_DEFAULT_URL
    result["model"] = {"provider": provider, "model": model_name, "base_url": base_url}

    push = cfg.get("push") or {}
    channels = [c for c in (push.get("channels") or []) if c in ("wxpusher", "qq_private", "email")]
    night = push.get("night_quiet") or {}
    result["push"] = {
        "channels": channels,
        "batch_interval_minutes": _int_range(push.get("batch_interval_minutes"), 60, 1, 1440),
        "night_quiet": {
            "enabled": bool(night.get("enabled", True)),
            "start": _time_str(night.get("start"), "23:00"),
            "end": _time_str(night.get("end"), "08:00"),
        },
    }

    dash = cfg.get("dashboard") or {}
    result["dashboard"] = {"refresh_seconds": _int_range(dash.get("refresh_seconds"), 30, 5, 3600)}
    return result


def load_config(path=CONFIG_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    else:
        raw = {}
    norm = normalize_config(raw)
    if not os.path.exists(path):
        save_config(norm, path)
    return norm


def save_config(cfg, path=CONFIG_PATH):
    norm = normalize_config(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return norm


# ---------------- 运行时热重载 (看板保存后无需重启) ----------------
_cfg = None
_cfg_mtime = None
_secrets_mtime = None


def get_config():
    """返回当前配置; 首次调用加载, 之后由 config_watch_loop 定时刷新."""
    global _cfg, _cfg_mtime
    if _cfg is None:
        _cfg = load_config()
        try:
            _cfg_mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
        except OSError:
            _cfg_mtime = None
    return _cfg


def _reload_if_changed():
    """config.json / secrets.json 的 mtime 变了就重新加载."""
    global _cfg, _cfg_mtime, _secrets_cache, _secrets_mtime
    if _cfg is not None:
        try:
            mt = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
        except OSError:
            mt = None
        if mt != _cfg_mtime:
            _cfg = load_config()
            _cfg_mtime = mt
            print("[config] 检测到配置变化, 已热重载")
    try:
        smt = os.path.getmtime(SECRETS_FILE) if os.path.exists(SECRETS_FILE) else None
    except OSError:
        smt = None
    if smt != _secrets_mtime:
        _secrets_cache = None
        _secrets_mtime = smt


async def config_watch_loop():
    while True:
        await asyncio.sleep(2)
        try:
            _reload_if_changed()
        except Exception as e:
            print(f"[config] 热重载出错: {e}")


# ---------------- 文本与事件 ----------------

def extract_text(message):
    if isinstance(message, str):
        return message
    parts = []
    for seg in message or []:
        t = seg.get("type")
        data = seg.get("data", {}) or {}
        if t == "text":
            parts.append(data.get("text", ""))
        elif t == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif t == "image":
            parts.append("[图片]")
        elif t == "file":
            parts.append("[文件]")
        elif t == "record":
            parts.append("[语音]")
        else:
            parts.append(f"[{t}]")
    return "".join(parts)


def normalize_text(text):
    return re.sub(r"\s+", "", text).strip()


def sender_name_of(event):
    sender = event.get("sender") or {}
    for key in ("nickname", "card"):
        v = sender.get(key)
        if v:
            return str(v)
    return str(event.get("user_id", ""))


def _find_group(cfg, group_id):
    for g in cfg.get("groups", []):
        if g.get("id") == group_id:
            return g
    return None


# 去重状态 (进程内)
_seen = {}
_seen_text = {}
_seen_ids = set()


def coarse_filter(event, cfg):
    """本地粗筛: 返回 (是否候选, 原因). 屏蔽优先于重要, 全量AI跳过关键词."""
    if event.get("post_type") != "message":
        return False, ""
    if event.get("message_type") != "group":
        return False, ""
    try:
        group_id = int(event.get("group_id"))
        sender_id = int(event.get("user_id"))
    except (TypeError, ValueError):
        return False, ""
    group = _find_group(cfg, group_id)
    if group is None or not group.get("enabled", True):
        return False, ""
    text = extract_text(event.get("message"))
    if not text.strip():
        return False, ""
    now = time.time()

    msg_id = event.get("message_id")
    if msg_id is not None:
        if msg_id in _seen_ids:
            return False, ""
        _seen_ids.add(msg_id)
        if len(_seen_ids) > 10000:
            _seen_ids.clear()

    key = hashlib.md5(f"{group_id}|{sender_id}|{text}".encode()).hexdigest()
    if key in _seen and now - _seen[key] < DEDUP_WINDOW:
        return False, ""
    _seen[key] = now

    muted = set(cfg["global"].get("muted_senders", [])) | set(group.get("muted_senders", []))
    if sender_id in muted:
        return False, ""

    important = set(cfg["global"].get("important_senders", [])) | set(group.get("important_senders", []))
    if group.get("mode") == "full_ai":
        reason = "全量AI"
    elif sender_id in important:
        reason = "重要发言人"
    elif "@" in text or "全体" in text:
        reason = "@ / 全体"
    elif any(k in text for k in group.get("keywords", [])):
        reason = "关键词"
    else:
        return False, ""

    norm = normalize_text(text)
    if norm and norm in _seen_text and now - _seen_text[norm] < CROSS_GROUP_DEDUP_WINDOW:
        return False, ""
    if norm:
        _seen_text[norm] = now
    return True, reason


# ---------------- 精筛 ----------------

def _local_fallback(reason, why):
    return {
        "important": True,
        "why": why,
        "deadline": "",
        "deadline_dt": "",
        "urgent": False,
        "location": "",
        "action": "人工看一下原文",
    }


def classify_with_deepseek(cfg, group_id, sender_id, text, reason):
    provider = cfg["model"]["provider"]
    if provider == "subscription":
        return _local_fallback(reason, "订阅模式 v1 未启用, 按本地规则保留")
    if provider == "deepseek":
        api_key = get_secret("DEEPSEEK_API_KEY")
    else:
        api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        return _local_fallback(reason, "未配 API Key, 按本地规则保留")

    prompt = (
        "你是私人通知筛查员, 只判断下面这条 QQ 群消息对学生是否重要.\n"
        "重要 = 作业、考试、截止、交材料、报名、调课、点名、群公告类通知, "
        "以及讲座、比赛、活动、宣讲、实习、奖学金、选课、答辩、班会、党课、教务、缴费、放假等通知.\n"
        "闲聊、表情、转发、灌水 = 不重要.\n"
        "只输出 JSON, 不要多余文字:\n"
        '{"important": true/false, "why": "一句话", "deadline": "有就写, 没有写空", '
        '"deadline_dt": "YYYY-MM-DDTHH:MM 或空", "urgent": true/false, '
        '"location": "地点或空", "action": "要做什么, 没有写空"}\n'
        "说明: deadline_dt 尽量把截止时间归一化成本地 ISO 日期时间, 没有明确时间就留空; "
        "urgent 表示是否紧急(截止快到/带紧急/老师@全体); location 是消息里的地点(如教室、会场), 没有就留空."
    )
    try:
        r = requests.post(
            cfg["model"]["base_url"],
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"]["model"],
                "temperature": 0,
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"群:{group_id} 发言人:{sender_id} 内容:{text}"},
                ],
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        start, end = content.find("{"), content.rfind("}") + 1
        data = json.loads(content[start:end])
        return {
            "important": bool(data.get("important", True)),
            "why": str(data.get("why", "")),
            "deadline": str(data.get("deadline", "") or ""),
            "deadline_dt": str(data.get("deadline_dt", "") or ""),
            "urgent": bool(data.get("urgent", False)),
            "location": str(data.get("location", "") or ""),
            "action": str(data.get("action", "") or ""),
        }
    except Exception as e:
        return _local_fallback(reason, f"精筛调用失败, 按本地规则保留: {e}")


# ---------------- 紧急与摘要 ----------------

def is_emergency(record, cfg):
    if record.get("urgent"):
        return True
    text = str(record.get("text", ""))
    if any(w in text for w in EMERGENCY_WORDS):
        return True
    deadline = str(record.get("deadline", ""))
    if re.search(r"今天|今日|今晚|明天|小时内|分钟内|马上|立即", deadline):
        return True
    group = _find_group(cfg, record.get("group_id"))
    important = set(cfg["global"].get("important_senders", [])) | set((group or {}).get("important_senders", []))
    if record.get("sender_id") in important and ("全体" in text or "@" in text):
        return True
    dt = record.get("deadline_dt")
    if dt:
        try:
            t = datetime.fromisoformat(str(dt))
            delta_h = (t - datetime.now()).total_seconds() / 3600
            if 0 <= delta_h <= EMERGENCY_HOURS:
                return True
        except Exception:
            pass
    return False


def _group_name(cfg, group_id):
    g = _find_group(cfg, group_id)
    return (g or {}).get("name") or f"群{group_id}"


def build_summary(records, cfg):
    now = datetime.now().strftime("%H:%M")
    lines = [f"【今日重要消息 · {now}】共 {len(records)} 条"]
    for i, r in enumerate(records, 1):
        lines.append(f"{i}) [{_group_name(cfg, r.get('group_id'))}] {r.get('text', '')}")
        meta = []
        if r.get("deadline"):
            meta.append(f"截止：{r['deadline']}")
        if r.get("location"):
            meta.append(f"地点：{r['location']}")
        if r.get("action"):
            meta.append(f"要做：{r['action']}")
        if meta:
            lines.append("   " + " ｜ ".join(meta))
    return "\n".join(lines)


def build_emergency_summary(record, cfg):
    now = datetime.now().strftime("%H:%M")
    lines = [f"【紧急 · {now}】[{_group_name(cfg, record.get('group_id'))}] {record.get('text', '')}"]
    meta = []
    if record.get("deadline"):
        meta.append(f"截止：{record['deadline']}")
    if record.get("location"):
        meta.append(f"地点：{record['location']}")
    if record.get("action"):
        meta.append(f"要做：{record['action']}")
    if meta:
        lines.append("   " + " ｜ ".join(meta))
    return "\n".join(lines)


# ---------------- 数据文件 ----------------

def append_record(record):
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_today_records(date=None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    out = []
    if not os.path.exists(OUT_FILE):
        return out
    with open(OUT_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("time", "")).startswith(date):
                out.append(r)
    return out


def append_recent(record):
    try:
        with open(RECENT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim_recent()
    except Exception as e:
        print(f"[recent] 写入失败: {e}")


def _trim_recent():
    if not os.path.exists(RECENT_FILE):
        return
    with open(RECENT_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    if not lines:
        return
    tails = {}
    for i, line in enumerate(lines):
        try:
            gid = json.loads(line).get("group_id")
        except Exception:
            continue
        tails.setdefault(gid, []).append(i)
    keep = set()
    for idxs in tails.values():
        keep.update(idxs[-RECENT_LIMIT:])
    if len(keep) == len(lines):
        return
    new_lines = [l for i, l in enumerate(lines) if i in keep]
    tmp = RECENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.replace(tmp, RECENT_FILE)


def record_hash(record):
    s = f"{record.get('time')}|{record.get('group_id')}|{record.get('sender_id')}|{record.get('text')}"
    return hashlib.sha1(s.encode()).hexdigest()


def load_push_state():
    if os.path.exists(PUSH_STATE_FILE):
        try:
            with open(PUSH_STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("last_batch_time"), set(d.get("pushed") or [])
        except Exception:
            pass
    return None, set()


def save_push_state(last_batch_time, pushed):
    tmp = PUSH_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_batch_time": last_batch_time, "pushed": list(pushed)[-2000:]},
                  f, ensure_ascii=False)
    os.replace(tmp, PUSH_STATE_FILE)


# ---------------- 推送通道 ----------------

def send_wxpusher(content, summary):
    token = get_secret("WXPUSHER_APP_TOKEN")
    uid = get_secret("WXPUSHER_UID")
    if not token or not uid:
        return False, "未配置 WXPUSHER_APP_TOKEN / WXPUSHER_UID"
    try:
        r = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={"appToken": token, "contentType": 3, "content": content,
                  "summary": summary, "uids": [uid]},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == 1000:
            return True, "ok"
        return False, f"WxPusher: {data.get('msg')}"
    except Exception as e:
        return False, f"WxPusher 发送失败: {e}"


def _smtp_from_sender(sender):
    sender = (sender or "").strip()
    domain = sender.split("@")[-1].lower() if "@" in sender else ""
    table = {
        "qq.com": ("smtp.qq.com", 465),
        "foxmail.com": ("smtp.qq.com", 465),
        "163.com": ("smtp.163.com", 465),
        "126.com": ("smtp.126.com", 465),
        "yeah.net": ("smtp.yeah.net", 465),
        "gmail.com": ("smtp.gmail.com", 465),
        "outlook.com": ("smtp-mail.outlook.com", 587),
        "hotmail.com": ("smtp-mail.outlook.com", 587),
        "live.com": ("smtp-mail.outlook.com", 587),
        "icloud.com": ("smtp.mail.me.com", 587),
        "me.com": ("smtp.mail.me.com", 587),
    }
    if domain in table:
        return table[domain]
    if domain:
        return ("smtp." + domain, 465)
    return ("", 465)


def send_email(subject, content):
    user = get_secret("SMTP_USER")
    password = get_secret("SMTP_PASS")
    mail_to = get_secret("MAIL_TO")
    host, port = _smtp_from_sender(user)
    if not (host and user and password and mail_to):
        return False, "请填写发件邮箱、邮箱密码/授权码和收件邮箱"
    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = user
        msg["To"] = mail_to
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        server.login(user, password)
        server.sendmail(user, [mail_to], msg.as_string())
        server.quit()
        return True, "ok"
    except Exception as e:
        return False, f"邮件发送失败: {e}"


_send_lock = asyncio.Lock()


async def send_qq_private(ws, content):
    target = get_secret("SELF_QQ")
    if not target:
        return False, "未配置 SELF_QQ"
    try:
        payload = {"action": "send_private_msg",
                   "params": {"user_id": int(target), "message": content}}
        async with _send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        return True, "ok"
    except Exception as e:
        return False, f"QQ 私聊发送失败: {e}"


async def push_content(ws, cfg, content, title):
    channels = cfg["push"]["channels"]
    results = {}
    if "wxpusher" in channels:
        results["wxpusher"] = send_wxpusher(content, title)
    if "qq_private" in channels:
        results["qq_private"] = await send_qq_private(ws, content)
    if "email" in channels:
        results["email"] = send_email(title, content)
    if not channels:
        results["local"] = (True, "只看本地, 不推送")
    return results


# ---------------- 批量 / 静默 ----------------

def _in_quiet(cfg):
    q = cfg["push"]["night_quiet"]
    if not q.get("enabled"):
        return False
    now = datetime.now().strftime("%H:%M")
    start, end = q["start"], q["end"]
    if start <= end:
        return start <= now < end
    return now >= start or now < end


async def _run_batch(ws, last_batch_time, pushed):
    cfg = get_config()
    records = read_today_records()
    pending = [r for r in records if record_hash(r) not in pushed]
    if not pending:
        now = time.time()
        save_push_state(now, pushed)
        return now, pushed
    if _in_quiet(cfg) and not any(is_emergency(r, cfg) for r in pending):
        return last_batch_time, pushed
    now = time.time()
    if now - last_batch_time < cfg["push"]["batch_interval_minutes"] * 60:
        return last_batch_time, pushed
    to_send = pending[:SUMMARY_MAX]
    content = build_summary(to_send, cfg)
    await push_content(ws, cfg, content, "QQ 重要消息摘要")
    for r in to_send:
        pushed.add(record_hash(r))
    pushed = set(list(pushed)[-2000:])
    save_push_state(now, pushed)
    return now, pushed


async def batch_loop(ws):
    last_batch_time, pushed = load_push_state()
    if not last_batch_time:
        last_batch_time = time.time()
        save_push_state(last_batch_time, pushed)
    while True:
        await asyncio.sleep(BATCH_TICK_SECONDS)
        try:
            last_batch_time, pushed = await _run_batch(ws, last_batch_time, pushed)
        except Exception as e:
            print(f"[batch] 出错: {e}")


# OneBot 连接状态: 由过滤主循环维护, 看板的 /api/snowluma/status 读取.
_onebot_connected = False


def set_onebot_connected(value):
    global _onebot_connected
    _onebot_connected = bool(value)


def get_onebot_connected():
    return _onebot_connected


# ---------------- 主循环 ----------------

async def _handle_event(event, ws):
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return
    cfg = get_config()
    try:
        group_id = int(event.get("group_id"))
        sender_id = int(event.get("user_id"))
    except (TypeError, ValueError):
        return
    group = _find_group(cfg, group_id)
    if group is None or not group.get("enabled", True):
        return
    text = extract_text(event.get("message"))
    if text.strip():
        append_recent({
            "time": datetime.now().isoformat(timespec="seconds"),
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name_of(event),
            "text": text,
        })
    ok, reason = coarse_filter(event, cfg)
    if not ok:
        return
    result = classify_with_deepseek(cfg, group_id, sender_id, text, reason)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "group_id": group_id,
        "sender_id": sender_id,
        "sender_name": sender_name_of(event),
        "text": text,
        "coarse_reason": reason,
        **result,
    }
    print(json.dumps(record, ensure_ascii=False))
    if result.get("important"):
        append_record(record)
        last_batch_time, pushed = load_push_state()
        if is_emergency(record, cfg):
            content = build_emergency_summary(record, cfg)
            await push_content(ws, cfg, content, "QQ 紧急消息")
            pushed.add(record_hash(record))
            save_push_state(last_batch_time, pushed)


async def main():
    cfg = get_config()
    groups_on = [g["id"] for g in cfg["groups"] if g.get("enabled", True)]
    print(f"连接 SnowLuma OneBot: {ONEBOT_WS}")
    print(f"启用中的群: {groups_on or '无 (看板里新建群后自动生效)'}")
    while True:
        try:
            async with websockets.connect(ONEBOT_WS) as ws:
                print("已连接, 只读监听中, 不会发回 QQ 群 (QQ 私聊只发 SELF_QQ 自己)...")
                batch_task = asyncio.create_task(batch_loop(ws))
                watch_task = asyncio.create_task(config_watch_loop())
                try:
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue
                        if event.get("post_type") == "meta_event":
                            me = event.get("meta_event_type")
                            sub = event.get("sub_type")
                            if me == "lifecycle" and sub in ("connect", "enable"):
                                set_onebot_connected(True)
                            elif me == "lifecycle" and sub in ("disable", "disconnect"):
                                set_onebot_connected(False)
                        await _handle_event(event, ws)
                finally:
                    batch_task.cancel()
                    watch_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            set_onebot_connected(False)
            print(f"[filter] OneBot 连接断开或失败: {e}, 5 秒后重连...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
