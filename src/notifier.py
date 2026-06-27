"""
notifier.py - 每日报告推送模块

通过环境变量配置推送通道，支持企业微信、飞书群机器人 Webhook、飞书应用
机器人 API、Telegram、Slack、Discord、自定义 Webhook、ntfy、Gotify、
PushPlus、ServerChan 和 SMTP 邮件。推送内容仍定位为研究性数据分析，
不包含投资建议。
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional
from urllib import parse, request
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

DISCLAIMER_SHORT = "免责声明：本报告由 AI 工作流自动生成，仅用于个人学习与技术探索，不构成任何投资建议。"
DEFAULT_TIMEOUT_SECONDS = 15


def _ssl_context() -> ssl.SSLContext:
    """创建 HTTPS 校验上下文，优先使用 certifi CA 包。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


@dataclass
class PushResult:
    """单个推送通道的结果。"""

    channel: str
    status: str
    detail: str


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """读取环境变量，空字符串按 None 处理。"""
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量。"""
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _truncate(text: str, limit: int) -> str:
    """按字符数截断文本。"""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)].rstrip() + "\n\n...（内容已截断，请查看完整 report.md）"


def _plain_markdown(text: str) -> str:
    """把常见 Markdown 标记转成飞书内更易读的纯文本。"""
    plain = text.strip()
    plain = re.sub(r"`([^`]+)`", r"\1", plain)
    plain = plain.replace("**", "").replace("__", "")
    plain = plain.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    return plain.strip()


def _split_markdown_table_row(line: str) -> list[str]:
    """解析 Markdown 表格行。"""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [_plain_markdown(cell) for cell in stripped.strip("|").split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    """判断是否为 Markdown 表格分隔行。"""
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(cell and set(cell) <= {"-", ":"} for cell in cells)


def _format_feishu_table_row(cells: list[str], headers: list[str]) -> list[str]:
    """把 Markdown 表格行转为飞书适合阅读的多行摘要。"""
    values = {header: cells[idx] for idx, header in enumerate(headers) if idx < len(cells)}
    if {"标的", "综合分", "当前/信号价", "触发区间", "止损", "第一止盈", "仓位", "计划"}.issubset(values):
        tags = values.get("策略标签", "")
        rows = [
            f"【{values['标的']}】",
            f"综合分 {values['综合分']}｜仓位 {values['仓位']}｜当前/信号价 {values['当前/信号价']}",
            f"触发区间 {values['触发区间']}｜止损 {values['止损']}｜第一止盈 {values['第一止盈']}",
        ]
        if tags:
            rows.append(f"标签：{tags}")
        rows.append(f"计划：{values['计划']}")
        return rows

    if {"标的", "信号日", "复盘状态", "收盘偏离", "最大顺向", "最大逆向", "观察"}.issubset(values):
        return [
            f"【复盘 {values['标的']}】",
            f"状态 {values['复盘状态']}｜信号日 {values['信号日']}",
            f"收盘偏离 {values['收盘偏离']}｜最大顺向 {values['最大顺向']}｜最大逆向 {values['最大逆向']}",
            f"观察：{values['观察']}",
        ]

    pairs = [f"{headers[idx]}：{cell}" for idx, cell in enumerate(cells) if idx < len(headers) and cell]
    return ["｜".join(pairs) if pairs else "｜".join(cells)]


def _markdown_to_readable_lines(text: str) -> list[str]:
    """将 Markdown 摘要转为飞书富文本段落行。"""
    lines: list[str] = []
    table_headers: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue

        if _is_markdown_table_separator(stripped):
            continue

        table_cells = _split_markdown_table_row(stripped)
        if table_cells:
            if not table_headers:
                table_headers = table_cells
                continue
            lines.extend(_format_feishu_table_row(table_cells, table_headers))
            lines.append("")
            continue

        table_headers = []
        is_bold_heading = stripped.startswith("**") and stripped.endswith("**") and stripped.count("**") == 2
        line = _plain_markdown(stripped)
        if line.startswith("#"):
            line = line.lstrip("# ").strip()
        elif line.startswith("- "):
            line = f"• {line[2:].strip()}"
        if is_bold_heading:
            line = f"【{line}】"
        lines.append(line)

    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _build_feishu_post(title: str, content: str) -> dict[str, Any]:
    """构造飞书 post 富文本内容。"""
    paragraphs = []
    for line in _markdown_to_readable_lines(content):
        paragraphs.append([{"tag": "text", "text": line or " ", "un_escape": True}])
    return {
        "zh_cn": {
            "title": _plain_markdown(title),
            "content": paragraphs or [[{"tag": "text", "text": "报告内容为空", "un_escape": True}]],
        }
    }


def _load_report(report_path: Path) -> str:
    """读取报告 Markdown。"""
    with open(report_path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_lines(text: str, prefixes: tuple[str, ...]) -> list[str]:
    """从报告中提取指定前缀的行。"""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(prefixes):
            lines.append(line)
    return lines


def _extract_markdown_section(text: str, header: str, max_lines: int = 20) -> list[str]:
    """提取 Markdown 简单章节，直到下一个粗体标题或二级标题。"""
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == header:
            start = idx
            break
    if start is None:
        return []
    section = [lines[start]]
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**") and stripped != header:
            break
        if stripped.startswith("## "):
            break
        section.append(line)
        if len(section) >= max_lines:
            section.append("...（本节已截断）")
            break
    return section


def _build_scan_digest(text: str, report_path: Path, max_chars: int) -> tuple[str, str]:
    """构造收盘扫描报告摘要。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "收盘扫描报告")
    title = "收盘扫描报告"
    if "完成了" in first_line:
        title = first_line.split("完成了", 1)[-1].split("收盘扫描", 1)[0].strip() or title
        title = f"{title} 收盘扫描"

    content_parts: list[str] = [
        DISCLAIMER_SHORT,
        "",
        first_line,
    ]
    for header, max_lines in [
        ("**昨日计划复盘**", 16),
        ("**大盘判断**", 5),
        ("**策略健康监控**", 10),
        ("**Hermes 策略管家**", 12),
        ("**策略学习记忆**", 6),
        ("**市场环境过滤**", 12),
        ("**今日候选，不超过 5 只**", 12),
        ("**规则回测摘要（历史 K 样本）**", 9),
        ("**组合级回测摘要**", 8),
        ("**影子实验评估**", 12),
        ("**Pending 台账**", 6),
        ("**明日复核要点**", 5),
    ]:
        section = _extract_markdown_section(text, header, max_lines=max_lines)
        if section:
            content_parts.extend(["", *section])
    content_parts.extend(["", f"完整报告路径：{report_path}"])
    return title, _truncate("\n".join(content_parts), max_chars)


def build_push_content(report_path: Path, mode: str = "digest", max_chars: int = 3500) -> tuple[str, str]:
    """
    构造推送标题和正文。

    Args:
        report_path: report.md 路径
        mode:        digest 仅推摘要，full 推完整报告
        max_chars:   推送正文最大字符数
    Returns:
        (title, content)
    """
    text = _load_report(report_path)
    first_title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")), "每日市场观察报告")

    if mode == "full":
        content = text
    elif "使用市场数据接口完成了" in text and "**今日候选" in text:
        return _build_scan_digest(text, report_path, max_chars=max_chars)
    else:
        overview_lines = _extract_lines(text, ("- **观察标的数量**", "- **数据质量**", "- **数据来源**", "- **报告生成时间**"))
        stock_headers = [line for line in text.splitlines() if line.startswith("### ")]
        quality_start = text.find("## 三、数据质量与异常提示")
        meta_start = text.find("## 四、附录")
        quality_block = ""
        if quality_start != -1:
            quality_block = text[quality_start: meta_start if meta_start != -1 else None].strip()

        content_parts = [
            DISCLAIMER_SHORT,
            "",
            "## 摘要",
            *overview_lines,
            "",
            "## 覆盖标的",
            *[f"- {h.lstrip('# ').strip()}" for h in stock_headers],
        ]
        if quality_block:
            content_parts.extend(["", quality_block])
        content_parts.extend(["", f"完整报告路径：{report_path}"])
        content = "\n".join(content_parts)

    return first_title, _truncate(content, max_chars)


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST JSON 并返回响应摘要。"""
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=req_headers, method="POST")
    with request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return f"HTTP {resp.status}: {body}"


def _post_form(
    url: str,
    payload: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST form 并返回响应摘要。"""
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return f"HTTP {resp.status}: {body}"


def _post_raw(
    url: str,
    content: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST 原始文本并返回响应摘要。"""
    req = request.Request(url, data=content.encode("utf-8"), headers=headers or {}, method="POST")
    with request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return f"HTTP {resp.status}: {body}"


def _post_json_no_read(
    url: str,
    payload: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST JSON，适用于响应体可能较大的通道。"""
    return _post_json(url, payload, headers=headers, timeout=timeout)


def _safe_send(channel: str, send_fn: Any, dry_run: bool) -> PushResult:
    """执行单个通道推送并包装错误。"""
    if dry_run:
        return PushResult(channel, "dry_run", "configured")
    try:
        detail = send_fn()
        return PushResult(channel, "success", detail)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, smtplib.SMTPException, json.JSONDecodeError) as exc:
        return PushResult(channel, "failed", str(exc))


def _feishu_sign(secret: str, timestamp: str) -> str:
    """生成飞书机器人签名。"""
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _send_feishu_app_message(title: str, content: str) -> str:
    """通过飞书开放平台应用机器人发送报告消息。"""
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    receive_id = _env("FEISHU_RECEIVE_ID")
    if not app_id or not app_secret or not receive_id:
        raise OSError("FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_RECEIVE_ID are required")

    token_resp = _post_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    token_data = json.loads(token_resp.split(": ", 1)[1])
    if token_data.get("code") != 0 or not token_data.get("tenant_access_token"):
        raise OSError(f"failed to get tenant_access_token: {token_resp}")

    receive_id_type = _env("FEISHU_RECEIVE_ID_TYPE", "chat_id") or "chat_id"
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={parse.quote(receive_id_type)}"
    message_format = (_env("FEISHU_APP_MESSAGE_FORMAT") or _env("FEISHU_MESSAGE_FORMAT", "post") or "post").lower()
    if message_format == "text":
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": _truncate(f"{title}\n\n{content}", 9500)}, ensure_ascii=False),
        }
    else:
        payload = {
            "receive_id": receive_id,
            "msg_type": "post",
            "content": json.dumps(_build_feishu_post(title, content), ensure_ascii=False),
        }
    headers = {"Authorization": f"Bearer {token_data['tenant_access_token']}"}
    message_resp = _post_json(url, payload, headers=headers)
    message_data = json.loads(message_resp.split(": ", 1)[1])
    if message_data.get("code") != 0:
        raise OSError(f"failed to send Feishu message: {message_resp}")
    message_id = (message_data.get("data") or {}).get("message_id", "unknown")
    return f"sent message_id={message_id}"


def _send_email(title: str, content: str) -> str:
    """通过 SMTP 发送邮件。"""
    host = _env("SMTP_HOST")
    if not host:
        raise OSError("SMTP_HOST is required")
    port = int(_env("SMTP_PORT", "465") or "465")
    username = _env("SMTP_USER") or _env("EMAIL_SENDER")
    password = _env("SMTP_PASSWORD") or _env("EMAIL_PASSWORD")
    sender = _env("EMAIL_FROM") or username
    receivers_raw = _env("EMAIL_TO") or _env("EMAIL_RECEIVERS") or username
    if not sender or not receivers_raw:
        raise OSError("EMAIL_FROM/EMAIL_TO or SMTP_USER is required")
    receivers = [item.strip() for item in receivers_raw.split(",") if item.strip()]

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg.set_content(content)

    if port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=DEFAULT_TIMEOUT_SECONDS) as smtp:
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)

    return f"sent to {len(receivers)} receiver(s)"


def send_report(report_path: Path, mode: str = "digest", dry_run: bool = False) -> list[PushResult]:
    """
    按环境变量配置推送报告。

    Args:
        report_path: report.md 路径
        mode:        digest 或 full
        dry_run:     True 时只检查配置，不发送
    Returns:
        每个已配置通道的结果
    """
    title, content = build_push_content(report_path, mode=mode)
    results: list[PushResult] = []

    wechat_url = _env("WECHAT_WEBHOOK_URL")
    if wechat_url:
        payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{content}"}}
        results.append(_safe_send("wechat", lambda: _post_json(wechat_url, payload), dry_run))

    feishu_url = _env("FEISHU_WEBHOOK_URL")
    if feishu_url:
        webhook_format = (_env("FEISHU_WEBHOOK_MESSAGE_FORMAT") or _env("FEISHU_MESSAGE_FORMAT", "post") or "post").lower()
        if webhook_format == "text":
            payload: dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
        else:
            payload = {"msg_type": "post", "content": {"post": _build_feishu_post(title, content)}}
        secret = _env("FEISHU_WEBHOOK_SECRET")
        if secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _feishu_sign(secret, timestamp)
        results.append(_safe_send("feishu", lambda: _post_json(feishu_url, payload), dry_run))

    if _env("FEISHU_APP_ID") or _env("FEISHU_APP_SECRET") or _env("FEISHU_RECEIVE_ID"):
        results.append(_safe_send("feishu_app", lambda: _send_feishu_app_message(title, content), dry_run))

    telegram_token = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _env("TELEGRAM_CHAT_ID")
    if telegram_token or telegram_chat_id:
        if not telegram_token or not telegram_chat_id:
            results.append(PushResult("telegram", "failed", "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured together"))
        else:
            telegram_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            payload: dict[str, Any] = {
                "chat_id": telegram_chat_id,
                "text": _truncate(f"{title}\n\n{content}", 3900),
                "disable_web_page_preview": True,
            }
            thread_id = _env("TELEGRAM_MESSAGE_THREAD_ID")
            if thread_id:
                payload["message_thread_id"] = int(thread_id)
            results.append(_safe_send("telegram", lambda: _post_json_no_read(telegram_url, payload), dry_run))

    slack_url = _env("SLACK_WEBHOOK_URL")
    if slack_url:
        payload = {"text": f"*{title}*\n\n{content}"}
        results.append(_safe_send("slack", lambda: _post_json(slack_url, payload), dry_run))

    discord_url = _env("DISCORD_WEBHOOK_URL")
    if discord_url:
        payload = {"content": _truncate(f"**{title}**\n\n{content}", 1900)}
        results.append(_safe_send("discord", lambda: _post_json(discord_url, payload), dry_run))

    custom_url = _env("CUSTOM_WEBHOOK_URL")
    if custom_url:
        headers = {}
        bearer = _env("CUSTOM_WEBHOOK_BEARER_TOKEN")
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        def send_custom_webhook() -> str:
            template = _env("CUSTOM_WEBHOOK_BODY_TEMPLATE")
            if template:
                rendered = template.replace("{title}", title).replace("{content}", content).replace("{report_path}", str(report_path))
                payload = json.loads(rendered)
            else:
                payload = {"title": title, "content": content, "report_path": str(report_path)}
            return _post_json(custom_url, payload, headers=headers)

        results.append(_safe_send("custom_webhook", send_custom_webhook, dry_run))

    ntfy_url = _env("NTFY_URL")
    if ntfy_url:
        headers = {"Title": title, "Markdown": "yes"}
        token = _env("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        results.append(_safe_send("ntfy", lambda: _post_raw(ntfy_url, content, headers=headers), dry_run))

    gotify_url = _env("GOTIFY_URL")
    gotify_token = _env("GOTIFY_TOKEN")
    if gotify_url or gotify_token:
        if not gotify_url or not gotify_token:
            results.append(PushResult("gotify", "failed", "GOTIFY_URL and GOTIFY_TOKEN must be configured together"))
        else:
            url = f"{gotify_url.rstrip('/')}/message"
            payload = {"title": title, "message": content, "priority": int(_env("GOTIFY_PRIORITY", "5") or "5")}
            headers = {"X-Gotify-Key": gotify_token}
            results.append(_safe_send("gotify", lambda: _post_json(url, payload, headers=headers), dry_run))

    pushplus_token = _env("PUSHPLUS_TOKEN")
    if pushplus_token:
        payload: dict[str, Any] = {
            "token": pushplus_token,
            "title": title,
            "content": content,
            "template": "markdown",
        }
        topic = _env("PUSHPLUS_TOPIC")
        if topic:
            payload["topic"] = topic
        results.append(_safe_send("pushplus", lambda: _post_json("https://www.pushplus.plus/send", payload), dry_run))

    sendkey = _env("SERVERCHAN3_SENDKEY") or _env("SERVERCHAN_SENDKEY")
    if sendkey:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        payload = {"title": title, "desp": content}
        results.append(_safe_send("serverchan", lambda: _post_form(url, payload), dry_run))

    if _env("SMTP_HOST") or _env_bool("EMAIL_ENABLED"):
        results.append(_safe_send("email", lambda: _send_email(title, content), dry_run))

    if not results:
        results.append(PushResult("none", "skipped", "no push channel configured"))

    for result in results:
        logger.info("push %s: %s (%s)", result.channel, result.status, result.detail)
    return results


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="推送每日市场观察报告")
    parser.add_argument("--report", required=True, help="report.md 路径")
    parser.add_argument("--mode", choices=["digest", "full"], default=_env("PUSH_REPORT_MODE", "digest"))
    parser.add_argument("--dry-run", action="store_true", help="只检查通道配置，不发送")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    results = send_report(Path(args.report), mode=args.mode, dry_run=args.dry_run)
    failed = [item for item in results if item.status == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
