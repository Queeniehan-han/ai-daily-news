# -*- coding: utf-8 -*-
"""
llm_client.py — 多 Provider LLM 客户端（AI每日大事件 Max）

PRD §「用户可选择自己的大模型 api 调用和 api key 来驱动产品」：
  公开三方：OpenRouter / Gemini / OpenAI / Anthropic / Kimi / MiniMax / DeepSeek
  Bytedance：ModelHub（AzureOpenAI 形态，base_url 即完整 crawl 端点，
             需 X-TT-LOGID 头 —— 与 PRD 调用样例一致）

关键踩坑修复（项目历史沉淀，缺一不可）：
  1) Bytedance ModelHub 的 Gemini 模型默认开启 thinking(budget=8192)，会吃光
     token 预算导致正文 JSON 被截断为空。必须显式 extra_body=
     {"thinking": {"budget_tokens": 0}}（仅 azure_openai + gemini 模型时启用）；
     同时 max_tokens 不低于 8000。
  2) gemini-3.1-p-priority 普通账户经常 429「资源不足 code -4302」。必须识别
     429/quota/rate limit/资源不足 等关键字并明确提示切换模型，而非黑盒「0 条」。
  3) openai SDK 默认 timeout=600s + 内部重试，与应用层重试叠乘会挂起数分钟。
     必须显式 timeout=120.0, max_retries=0，重试完全交给应用层。

统一对外接口：LLMClient(provider, api_key, model).chat(messages) -> str
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

import config


# ──────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────
class LLMError(Exception):
    """LLM 调用失败的统一异常，message 面向用户可读。"""


class LLMQuotaError(LLMError):
    """配额耗尽 / 限频，最终面向用户提示切换模型或 Provider。"""


class LLMRateLimitError(LLMQuotaError):
    """短时限频 —— 等待后可重试，最终失败时仍按配额/限频提示用户。"""


_QUOTA_KEYWORDS = (
    "quota", "资源不足", "资源不对外", "-4302", "overloaded",
    "insufficient_quota",
)

_RATE_LIMIT_KEYWORDS = (
    "429", "qpm limit", "rate limit", "rate_limit", "ratelimit",
    "too many requests",
)


def _classify_error(exc: Exception) -> LLMError:
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in _RATE_LIMIT_KEYWORDS):
        return LLMRateLimitError(
            f"配额耗尽 / 限频：{msg}\n"
            f"建议切换到 gemini-3.5-flash（普通账户更稳定）或更换 Provider。")
    if any(k in low for k in _QUOTA_KEYWORDS):
        return LLMQuotaError(
            f"配额耗尽 / 限频：{msg}\n"
            f"建议切换到 gemini-3.5-flash（普通账户更稳定）或更换 Provider。")
    return LLMError(msg)


# ──────────────────────────────────────────────────────────────────────────
# LLM 客户端
# ──────────────────────────────────────────────────────────────────────────
class LLMClient:
    def __init__(self, provider: str, api_key: str, model: Optional[str] = None):
        if provider not in config.LLM_PROVIDERS:
            raise LLMError(f"未知 Provider: {provider}")
        if not api_key or not api_key.strip():
            raise LLMError(f"{provider} 的 API Key 不能为空，请在界面输入。")

        self.provider = provider
        self.api_key = api_key.strip()
        self.conf = config.LLM_PROVIDERS[provider]
        self.client_type = self.conf["client_type"]
        self.model = model or self.conf["default_model"]
        self._client = None
        self._gemini_mode = ""
        self._init_client()

    # ── 初始化底层 SDK 客户端 ─────────────────────────────────────────────
    def _init_client(self):
        ct = self.client_type
        if ct == "openai_compat":
            import openai
            self._client = openai.OpenAI(
                api_key=self.api_key, base_url=self.conf["base_url"],
                timeout=120.0, max_retries=0)
        elif ct == "azure_openai":
            import openai
            self._client = openai.AzureOpenAI(
                api_key=self.api_key,
                azure_endpoint=self.conf["base_url"],
                api_version=self.conf.get("api_version", "2024-03-01-preview"),
                timeout=120.0, max_retries=0)
        elif ct == "gemini":
            try:
                from google import genai  # type: ignore
                self._client = genai.Client(api_key=self.api_key)
                self._gemini_mode = "genai"
            except Exception:
                # google-genai 未安装时退化到 Gemini 的 OpenAI 兼容端点
                import openai
                self._client = openai.OpenAI(
                    api_key=self.api_key,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    timeout=120.0, max_retries=0)
                self._gemini_mode = "openai_compat"
        elif ct == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=120.0, max_retries=0)
        else:
            raise LLMError(f"不支持的 client_type: {ct}")

    # ── 统一对话接口 ──────────────────────────────────────────────────────
    def chat(self, messages: List[Dict], *, max_tokens: int = 8000,
             temperature: float = 0.3, max_retries: int = 2) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                return self._dispatch(messages, max_tokens, temperature)
            except Exception as e:  # noqa: BLE001
                err = _classify_error(e)
                last_exc = err
                if isinstance(err, LLMRateLimitError):
                    if attempt < max_retries:
                        time.sleep(65.0)
                        continue
                    raise err
                if isinstance(err, LLMQuotaError):
                    raise err            # 配额错误重试无意义
                if attempt < max_retries:
                    time.sleep(1.2 * (attempt + 1))
        raise last_exc if last_exc else LLMError("未知 LLM 调用失败")

    def _dispatch(self, messages: List[Dict], max_tokens: int,
                  temperature: float) -> str:
        ct = self.client_type
        if ct == "anthropic":
            return self._chat_anthropic(messages, max_tokens, temperature)
        if ct == "gemini" and self._gemini_mode == "genai":
            return self._chat_gemini_native(messages, max_tokens, temperature)
        # openai_compat / azure_openai / gemini(openai_compat 降级)
        return self._chat_openai_like(messages, max_tokens, temperature)

    def _chat_openai_like(self, messages: List[Dict], max_tokens: int,
                          temperature: float) -> str:
        kwargs = dict(
            model=self.model, messages=messages,
            max_tokens=max_tokens, stream=False)
        if not self._requires_default_temperature():
            kwargs["temperature"] = temperature
        extra_headers: Dict[str, str] = {}
        extra_body: Dict = {}

        if self.client_type == "azure_openai":
            # Bytedance ModelHub 必需头（PRD 调用样例）
            extra_headers["X-TT-LOGID"] = uuid.uuid4().hex
            # Gemini 模型必须显式关闭 thinking，否则正文 JSON 被截断为空
            if "gemini" in self.model.lower():
                extra_body["thinking"] = {"budget_tokens": 0}

        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        if extra_body:
            kwargs["extra_body"] = extra_body

        resp = self._client.chat.completions.create(**kwargs)
        if not resp.choices:
            raise LLMError("LLM 返回空 choices（可能 thinking 吃光预算或被限流）")
        content = resp.choices[0].message.content or ""
        if not content.strip():
            raise LLMError("LLM 返回空正文（Bytedance Gemini 请确认已关闭 thinking）")
        return content

    def _requires_default_temperature(self) -> bool:
        """Some ModelHub GPT deployments reject any non-default temperature."""
        return self.client_type == "azure_openai" and self.model.lower().startswith("gpt-5")

    def _chat_gemini_native(self, messages: List[Dict], max_tokens: int,
                            temperature: float) -> str:
        from google.genai import types  # type: ignore
        sys_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user_text = "\n".join(m["content"] for m in messages if m["role"] != "system")
        prompt = (sys_text + "\n\n" + user_text).strip()
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_tokens, temperature=temperature)
        resp = self._client.models.generate_content(
            model=self.model, contents=prompt, config=cfg)
        text = getattr(resp, "text", "") or ""
        if not text.strip():
            raise LLMError("Gemini 返回空正文")
        return text

    def _chat_anthropic(self, messages: List[Dict], max_tokens: int,
                        temperature: float) -> str:
        sys_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        conv = [m for m in messages if m["role"] != "system"]
        if not conv:
            conv = [{"role": "user", "content": sys_text or ""}]
        resp = self._client.messages.create(
            model=self.model, system=sys_text or None,
            messages=conv, max_tokens=max_tokens, temperature=temperature)
        parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        text = "".join(parts)
        if not text.strip():
            raise LLMError("Anthropic 返回空正文")
        return text

    # ── 连通性自检（UI「测试连接」按钮用）──────────────────────────────────
    def ping(self) -> str:
        """轻量连通性测试，返回模型回答或抛出可读异常。"""
        return self.chat(
            [{"role": "user", "content": "请只回复两个字：可用"}],
            max_tokens=8000 if self.client_type == "azure_openai" else 32,
            temperature=0.0, max_retries=1)


def detect_bytedance_key(api_key: str) -> bool:
    """识别 Bytedance ModelHub Key（含 _GPT_AK 或以 dSx 开头）。"""
    if not api_key:
        return False
    k = api_key.strip()
    return "_GPT_AK" in k or k.startswith("dSx")
