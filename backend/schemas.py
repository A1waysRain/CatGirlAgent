
from typing import Optional

from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    chatmassage: str = Field(description="用户的消息")
    session_id: Optional[str] = Field(default=None, description="目标会话 id；不传则用当前会话")

class SessionBrief(BaseModel):
    id: str
    title: str


class MessageIds(BaseModel):
    user: str
    assistant: str | None = None


class ChatResponse(BaseModel):
    """chat_response 端点返回契约（2026-08-16 扩全并接 response_model）。

    必须和 main.py 里 /api/chat_response 的真实返回保持一致——
    缺了 session/message_ids，前端下拉标题同步和删除/重生成按钮会坏。
    """
    answer: str
    source: str = "llm"
    session: SessionBrief
    message_ids: MessageIds