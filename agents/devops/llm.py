import json
import re
from typing import Any, Callable, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

try:
    from agents.devops.config import DevOpsConfig
except ImportError:
    from config import DevOpsConfig

T = TypeVar("T", bound=BaseModel)

PROVIDER_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}


class LLMError(Exception):
    """Lỗi phát sinh trong quá trình giao tiếp hoặc parse phản hồi từ LLM."""
    pass


def extract_json_string(text: str) -> str:
    """Trích xuất chuỗi JSON từ phản hồi LLM kể cả khi bị bọc markdown ```json."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1].strip()

    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        return text[first_bracket : last_bracket + 1].strip()

    return text


class LLMClient:
    """Client giao tiếp LLM tương thích chuẩn OpenAI, hỗ trợ preset provider, retry và parse JSON."""

    def __init__(
        self,
        config: DevOpsConfig,
        mock: bool = False,
        mock_responder: Callable[[list[dict[str, str]]], str] | None = None,
    ) -> None:
        self.config = config
        self.mock = mock
        self.mock_responder = mock_responder

        provider = config.llm_provider.lower().strip()
        if config.llm_base_url and config.llm_base_url.strip():
            base_url = config.llm_base_url.strip()
        else:
            base_url = PROVIDER_BASE_URLS.get(provider, PROVIDER_BASE_URLS["openai"])

        api_key = config.llm_api_key.strip()
        if not api_key:
            api_key = "dummy-key-for-local-or-mock"

        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=float(config.llm_timeout_seconds),
        )

    def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """Thực hiện một lần gọi LLM thông qua OpenAI client hoặc mock responder."""
        if self.mock:
            if self.mock_responder:
                return self.mock_responder(messages)
            return "{}"

        try:
            response = self.client.chat.completions.create(
                model=self.config.llm_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=self.config.llm_temperature,
                timeout=float(self.config.llm_timeout_seconds),
            )
            choice = response.choices[0]
            content = choice.message.content
            if content is None:
                raise LLMError("LLM trả về phản hồi rỗng (content=None).")
            return content
        except Exception as e:
            if isinstance(e, LLMError):
                raise
            raise LLMError(f"Lỗi kết nối hoặc thực thi API LLM: {e}") from e

    def complete_json(
        self,
        messages: list[dict[str, str]],
        output_model: type[T],
    ) -> T:
        """Gọi LLM, trích xuất và validate JSON theo output_model với cơ chế retry tự sửa lỗi."""
        conversation = list(messages)
        retries = 0
        last_error: Exception | None = None

        while retries <= self.config.llm_max_retries:
            raw_response = ""
            try:
                raw_response = self._call_llm(conversation)
                json_text = extract_json_string(raw_response)
                validated_data = output_model.model_validate_json(json_text)
                return validated_data
            except (json.JSONDecodeError, ValidationError, ValueError) as err:
                last_error = err
                retries += 1
                if retries > self.config.llm_max_retries:
                    break

                conversation.append({"role": "assistant", "content": raw_response})
                conversation.append({
                    "role": "user",
                    "content": (
                        f"Phản hồi trước đó không hợp lệ theo Schema. "
                        f"Chi tiết lỗi: {err}. "
                        f"Vui lòng chỉ trả về DUY NHẤT một chuỗi JSON hợp lệ tuân thủ Schema, "
                        f"không giải thích thêm."
                    ),
                })
            except Exception as e:
                raise LLMError(f"Lỗi trong quá trình gọi LLM: {e}") from e

        raise LLMError(
            f"Không thể lấy được JSON hợp lệ từ LLM sau {self.config.llm_max_retries} lần thử lại. "
            f"Lỗi cuối cùng: {last_error}"
        )
