# -*- coding: utf-8 -*-
"""
LLM 模型封装模块
统一封装网络模型（Qwen 等）和本地模型（Ollama / llama.cpp）的调用
基于 LangChain ChatOpenAI，通过 OpenAI 兼容接口实现，代码简洁且功能完整
支持单轮对话、多轮对话、流式输出，统一返回格式并记录耗时
"""

import time
from typing import List, Dict, Any, Optional, Generator, Tuple

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, BaseMessage

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen3.5:4b"
DEFAULT_NETWORK_MODEL = "qwen-turbo"
DEFAULT_NETWORK_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class LLMProviderType:
    NETWORK = "network"
    LOCAL = "local"


class LLMWrapper:
    def __init__(
        self,
        provider: str = LLMProviderType.NETWORK,
        model: str = DEFAULT_NETWORK_MODEL,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.0,
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature

        if base_url:
            self.base_url = base_url.rstrip('/')
        else:
            self.base_url = (
                DEFAULT_OLLAMA_BASE_URL
                if provider == LLMProviderType.LOCAL
                else DEFAULT_NETWORK_BASE_URL
            ).rstrip('/')

        self.api_key = api_key or "sk-no-key-needed"

        self._llm = self._create_llm_client()

    def _create_llm_client(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=4096,
            timeout=120,
        )

    def _convert_messages(
        self,
        message: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        system_message: Optional[str] = None,
    ) -> List[BaseMessage]:
        result: List[BaseMessage] = []
        if system_message:
            result.append(SystemMessage(content=system_message))
        if messages is not None:
            result.extend([
                HumanMessage(content=m["content"]) if m["role"] == "user"
                else AIMessage(content=m["content"])
                for m in messages
            ])
        elif message is not None:
            result.append(HumanMessage(content=message))
        return result

    def single_turn(
        self,
        message: str,
        system_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_time = time.time()

        try:
            messages = self._convert_messages(message=message, system_message=system_message)
            response = self._llm.invoke(messages)

            elapsed = time.time() - start_time
            tokens = 0
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                tokens = response.usage_metadata.get('output_tokens', 0)

            return {
                "text": response.content,
                "meta": {
                    "time": round(elapsed, 2),
                    "tokens": tokens,
                },
            }
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "text": "",
                "meta": {
                    "time": round(elapsed, 2),
                    "tokens": 0,
                    "error": str(e),
                },
            }

    def multi_turn(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        start_time = time.time()

        try:
            msg_list = self._convert_messages(messages=messages)
            response = self._llm.invoke(msg_list)

            elapsed = time.time() - start_time
            tokens = 0
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                tokens = response.usage_metadata.get('output_tokens', 0)

            return {
                "text": response.content,
                "meta": {
                    "time": round(elapsed, 2),
                    "tokens": tokens,
                },
            }
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "text": "",
                "meta": {
                    "time": round(elapsed, 2),
                    "tokens": 0,
                    "error": str(e),
                },
            }

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
    ) -> Generator[Tuple[str, bool, Dict[str, Any]], None, None]:
        start_time = time.time()
        total_tokens = 0
        full_text = ""

        llm_stream = ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=4096,
            timeout=300,
            streaming=True,
        )

        try:
            msg_list = self._convert_messages(messages=messages)

            for chunk in llm_stream.stream(msg_list):
                if chunk.content:
                    full_text += chunk.content
                    total_tokens += 1
                    yield chunk.content, False, {}

            elapsed = time.time() - start_time
            meta = {
                "time": round(elapsed, 2),
                "tokens": total_tokens,
            }
            yield "", True, meta

        except Exception as e:
            elapsed = time.time() - start_time
            yield "", True, {
                "time": round(elapsed, 2),
                "tokens": total_tokens,
                "error": str(e),
            }

    def switch_model(
        self,
        provider: str,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.provider = provider
        self.model = model
        if base_url:
            self.base_url = base_url.rstrip('/')
        else:
            self.base_url = (
                DEFAULT_OLLAMA_BASE_URL
                if provider == LLMProviderType.LOCAL
                else DEFAULT_NETWORK_BASE_URL
            ).rstrip('/')
        if api_key is not None:
            self.api_key = api_key or "sk-no-key-needed"

        self._llm = self._create_llm_client()
