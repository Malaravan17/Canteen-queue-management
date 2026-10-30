from fastapi import APIRouter, HTTPException

from .schemas import ChatInput
from .gemini_service import GeminiUnavailable
from .chatbot_service import chat

router = APIRouter()


@router.post("/api/chat")
def chat_with_assistant(payload: ChatInput):
    try:
        return chat(payload.message, payload.session_id, payload.order_id, payload.tracking_token)
    except GeminiUnavailable as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        # Keep database and vendor errors out of student-facing responses.
        raise HTTPException(status_code=503, detail="The assistant is temporarily unavailable. Please try again shortly.") from exc
