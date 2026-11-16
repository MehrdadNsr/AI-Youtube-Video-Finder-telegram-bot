import logging
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError

from services.llm_router import LLMRouter, LLMUnavailable

logger = logging.getLogger(__name__)


class CaptionAnalysisResult(BaseModel):
    """Structured output schema for extracting video search metadata from captions."""

    exact_entities: list[str] = Field(
        default_factory=list,
        description=(
            "List of specific English technical terms, AI models, company names, "
            "people, or YouTube channel names mentioned (e.g., 'Anthropic', 'Claude Fable 5.2', 'Ben AI'). "
            "Translate transliterated Persian names back to accurate English spelling."
        ),
    )
    mentioned_duration_seconds: int | None = Field(
        default=None,
        description=(
            "Estimated full video duration in seconds if explicitly mentioned in the text "
            "(e.g., 'مدت زمان ویدیو: ۱۳ دقیقه' -> 780). Return null if not mentioned."
        ),
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description=(
            "2 to 3 highly optimized English YouTube search queries combining the extracted entities "
            "and translated core topic of the caption to find the original English YouTube video."
        ),
    )


SYSTEM_INSTRUCTION = """
You are an expert AI metadata extractor specialized in finding original English YouTube sources 
from Persian/English Telegram channel captions.

Telegram channels often repost short clips of longer YouTube videos (about AI, Tech, Coding, Vlogs) 
with a Persian caption. Your task is to analyze the caption and extract:
1. `exact_entities`: All proper nouns, model names, versions, tools, creator names, or companies in English.
   If a name is written in Persian (e.g., 'انتروپیک' or 'بن ای‌آی'), convert it to its exact English form ('Anthropic', 'Ben AI').
2. `mentioned_duration_seconds`: Look carefully for phrases indicating the length of the full/original video 
   (e.g., 'مدت زمان: ۱۳ دقیقه', 'ویدیوی کامل ۲۰ دقیقه‌ای', 'Duration: 15 mins'). Convert it strictly to total seconds (integer). 
   If no duration is mentioned, return null.
3. `search_queries`: Generate 2 to 3 concise, high-signal English search queries that a human would type into YouTube 
   to find the exact source video. Do NOT use quotes or boolean operators like AND/OR, as they restrict YouTube search too much.
   Order queries from most specific (including creator/model names) to broader topic + entity combinations.
"""


class GeminiExtractor:
    """Service for caption analysis and entity extraction routed via LLMRouter."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-3.8-flash",
        llm_router: LLMRouter | None = None,
    ) -> None:
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.llm_router = llm_router

    async def extract_from_caption(self, text: str) -> CaptionAnalysisResult | None:
        """Analyze Telegram post caption/text and return a validated CaptionAnalysisResult."""
        cleaned_text = text.strip()
        if not cleaned_text:
            return None

        prompt = f"Analyze the following Telegram post text/caption:\n\n{cleaned_text}"

        if self.llm_router is not None:
            try:
                parsed, _ = await self.llm_router.generate_json(
                    task="caption",
                    system=SYSTEM_INSTRUCTION,
                    prompt=prompt,
                    schema=CaptionAnalysisResult,
                )
                return parsed
            except LLMUnavailable as exc:
                logger.error("All LLM providers unavailable during caption extraction: %s", exc)
                return None
            except Exception as exc:
                logger.exception("Unexpected error in routed caption extraction: %s", exc)
                return None

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=CaptionAnalysisResult,
                    temperature=0.1,
                ),
            )

            if isinstance(response.parsed, CaptionAnalysisResult):
                return response.parsed

            if response.text:
                return CaptionAnalysisResult.model_validate_json(response.text)

            logger.warning("Empty response for caption text.")
            return None

        except APIError as exc:
            logger.error("Gemini API Error during caption extraction: %s", exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected error during Gemini extraction: %s", exc)
            return None
