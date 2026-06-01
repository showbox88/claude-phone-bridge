"""Summarize a week's Primary-inbox emails into a markdown digest using
the locally-bundled `claude` CLI (via claude-agent-sdk).

Auth piggy-backs on whatever the service already uses for chat — Anthropic
OAuth (Max plan) or ANTHROPIC_API_KEY. No new keys, no extra spend.

We run with `permission_mode='dontAsk'` and empty `allowed_tools` so the
model can't accidentally invoke shell/file tools — this is a pure text task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, TextBlock

log = logging.getLogger("bridge.summarizer")

MODEL = "claude-sonnet-4-5"

_PROMPT_TEMPLATE = """\
你正在为用户生成一份周报里的「重要邮件」总结。下面是过去 7 天 Gmail Primary inbox \
的所有邮件 (含已读未读)，共 {n} 封。Promotions / Social / Updates / Forums \
tab 的邮件已被 Gmail 过滤掉。

请你做两件事:
1. **挑出真正需要用户关注的邮件**: 比如账单/付款通知、面试/会议、需要回复的私人或工作邮件、\
账号安全变更、订单/物流异常、政府/法律/医疗通知。
2. **过滤掉低价值邮件**: 自动通知、营销 (虽然 Gmail 已过滤一遍但还会漏)、纯 newsletter、\
重复的服务更新、纯确认类邮件 (例如"密码修改成功")。

输出严格按下面的 Markdown 格式 (用中文)，不要写解释、不要包裹 code block:

### 🔥 需要处理 (N)
- **发件人** · 邮件主题
  一句话说清楚要做什么 / 为什么重要 (≤30 字)

### 📌 信息提醒 (N)
- **发件人** · 邮件主题
  一句话总结内容 (≤30 字)

### 📰 其他 N 封 (已自动归类: newsletter / 通知 / 已完成事项)

规则:
- "需要处理"区块: 最多 8 条，按重要性排序，无内容则写「(本周无需要处理的邮件)」
- "信息提醒"区块: 最多 6 条，无内容则整段省略
- "其他"区块: 只写计数，不要展开
- 发件人显示名字部分即可 (例如 "Amazon"而不是"Amazon <account@amazon.com>")
- 邮件主题超长就截断到 50 字符

邮件列表 (按时间倒序):

{emails}
"""


def _format_email(idx: int, m: dict[str, Any]) -> str:
    flag = "📨" if m.get("unread") else "✉️"
    sender = (m.get("from") or "").strip()
    subject = (m.get("subject") or "(无主题)").strip()
    snippet = (m.get("snippet") or "").strip()
    if len(snippet) > 300:
        snippet = snippet[:297] + "…"
    return (f"#{idx} {flag} {m.get('date','')}\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Snippet: {snippet}\n")


async def _summarize_async(emails: list[dict[str, Any]]) -> str:
    if not emails:
        return "> 本周 Primary inbox 没有邮件。"
    body = "\n".join(_format_email(i + 1, m) for i, m in enumerate(emails))
    prompt = _PROMPT_TEMPLATE.format(n=len(emails), emails=body)

    opts = ClaudeAgentOptions(
        model=MODEL,
        allowed_tools=[],
        permission_mode="dontAsk",
        max_turns=1,
        system_prompt=("You are a concise Chinese-language email triage "
                       "assistant. Reply only with the requested Markdown."),
    )

    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    chunks.append(block.text)
    text = "".join(chunks).strip()
    return text or "> 邮件总结为空 — 模型未返回内容。"


def summarize(emails: list[dict[str, Any]], *, timeout_sec: int = 120) -> str:
    """Sync wrapper. Returns markdown or a degraded placeholder on failure."""
    try:
        return asyncio.run(asyncio.wait_for(
            _summarize_async(emails), timeout=timeout_sec))
    except asyncio.TimeoutError:
        log.warning("summarize timed out after %ds (n=%d)", timeout_sec, len(emails))
        return f"> 邮件总结超时 ({len(emails)} 封)，请稍后重试。"
    except Exception:
        log.exception("summarize failed (n=%d)", len(emails))
        return "> 邮件总结失败,详见服务器日志。"
