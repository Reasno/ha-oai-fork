"""AI Task integration for OpenAI."""

import base64
from json import JSONDecodeError
import logging
from mimetypes import guess_file_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from openai.types.responses.response_output_item import ImageGenerationCall

from homeassistant.components import ai_task, conversation
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util.json import json_loads

from .const import (
    CONF_BASE_URL,
    CONF_CHAT_MODEL,
    CONF_IMAGE_MODEL,
    DEFAULT_BASE_URL,
    RECOMMENDED_CHAT_MODEL,
    RECOMMENDED_IMAGE_MODEL,
    UNSUPPORTED_IMAGE_MODELS,
)
from .entity import OpenAIBaseLLMEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry

    from . import OpenAIConfigEntry

_LOGGER = logging.getLogger(__name__)


def _is_custom_base_url(base_url: str) -> bool:
    """Return if the configured base URL is not the official OpenAI endpoint."""
    return base_url.rstrip("/") != DEFAULT_BASE_URL.rstrip("/")


def _normalize_image_mime_type(output_format: str | None) -> str:
    """Normalize image output format into a MIME type."""
    if not output_format:
        return "image/png"
    if output_format == "jpg":
        output_format = "jpeg"
    return f"image/{output_format}"


def _parse_image_size(size: str | None) -> tuple[int | None, int | None]:
    """Parse a <width>x<height> size string."""
    if not size or "x" not in size:
        return None, None
    width_str, height_str = size.split("x", 1)
    try:
        return int(width_str), int(height_str)
    except ValueError:
        return None, None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenAIConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AI Task entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "ai_task_data":
            continue

        async_add_entities(
            [OpenAITaskEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class OpenAITaskEntity(
    ai_task.AITaskEntity,
    OpenAIBaseLLMEntity,
):
    """OpenAI AI Task entity."""

    def __init__(self, entry: OpenAIConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        self._attr_supported_features = (
            ai_task.AITaskEntityFeature.GENERATE_DATA
            | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        )
        model = self.subentry.data.get(CONF_CHAT_MODEL, RECOMMENDED_CHAT_MODEL)
        if not model.startswith(tuple(UNSUPPORTED_IMAGE_MODELS)):
            self._attr_supported_features |= ai_task.AITaskEntityFeature.GENERATE_IMAGE

    async def _async_prepare_image_inputs(
        self,
        attachments: list[conversation.Attachment] | None,
    ) -> list[str]:
        """Prepare image attachments as data URLs for direct image APIs."""

        def _prepare() -> list[str]:
            image_inputs: list[str] = []
            for attachment in attachments or []:
                path = Path(attachment.path)
                mime_type = attachment.mime_type or guess_file_type(path)[0]
                if not mime_type or not mime_type.startswith("image/"):
                    raise HomeAssistantError(
                        f"Unsupported image attachment type: {attachment.mime_type or 'unknown'}"
                    )
                encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
                image_inputs.append(f"data:{mime_type};base64,{encoded}")
            return image_inputs

        return await self.hass.async_add_executor_job(_prepare)

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Handle a generate data task."""
        await self._async_handle_chat_log(
            chat_log, task.name, task.structure, max_iterations=1000
        )

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError(
                "Last content in chat log is not an AssistantContent"
            )

        text = chat_log.content[-1].content or ""

        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id,
                data=text,
            )
        try:
            data = json_loads(text)
        except JSONDecodeError as err:
            _LOGGER.error(
                "Failed to parse JSON response: %s. Response: %s",
                err,
                text,
            )
            raise HomeAssistantError("Error with OpenAI structured response") from err

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=data,
        )

    async def _async_generate_image_via_direct_api(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Generate an image via the direct images API instead of tool calls."""
        base_url = (self.entry.data.get(CONF_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        model = self.subentry.data.get(CONF_IMAGE_MODEL, RECOMMENDED_IMAGE_MODEL)
        payload: dict[str, Any] = {
            "model": model,
            "prompt": task.instructions,
            "response_format": "b64_json",
        }

        image_inputs = await self._async_prepare_image_inputs(task.attachments)
        if image_inputs:
            payload["image"] = image_inputs[0] if len(image_inputs) == 1 else image_inputs

        if _is_custom_base_url(base_url):
            payload["output_format"] = "png"
            payload["watermark"] = False

        client = get_async_client(self.hass)
        try:
            response = await client.post(
                f"{base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.entry.data[CONF_API_KEY]}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=180.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            detail = err.response.text
            _LOGGER.error("Direct image API request failed: %s", detail)
            raise HomeAssistantError("Error generating image") from err
        except httpx.HTTPError as err:
            raise HomeAssistantError("Error generating image") from err

        data = response.json()
        if not data.get("data"):
            raise HomeAssistantError("No image returned")

        image_item = data["data"][0]
        mime_type = _normalize_image_mime_type(image_item.get("output_format"))
        width, height = _parse_image_size(image_item.get("size"))

        if b64_json := image_item.get("b64_json"):
            image_data = base64.b64decode(b64_json)
        elif image_url := image_item.get("url"):
            try:
                image_response = await client.get(image_url, timeout=60.0)
                image_response.raise_for_status()
            except httpx.HTTPError as err:
                raise HomeAssistantError("Error downloading generated image") from err
            image_data = image_response.content
            mime_type = image_response.headers.get("Content-Type", mime_type)
        else:
            raise HomeAssistantError("No image returned")

        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=mime_type,
            width=width,
            height=height,
            model=model,
            revised_prompt=image_item.get("revised_prompt"),
        )

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Handle a generate image task."""
        base_url = self.entry.data.get(CONF_BASE_URL) or DEFAULT_BASE_URL
        if _is_custom_base_url(base_url):
            return await self._async_generate_image_via_direct_api(task, chat_log)

        await self._async_handle_chat_log(chat_log, task.name, force_image=True)

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError(
                "Last content in chat log is not an AssistantContent"
            )

        image_call: ImageGenerationCall | None = None
        for content in reversed(chat_log.content):
            if not isinstance(content, conversation.AssistantContent):
                break
            if isinstance(content.native, ImageGenerationCall):
                if image_call is None or image_call.result is None:
                    image_call = content.native
                else:  # Remove image data from chat log to save memory
                    content.native.result = None

        if image_call is None or image_call.result is None:
            raise HomeAssistantError("No image returned")

        image_data = base64.b64decode(image_call.result)
        image_call.result = None

        if hasattr(image_call, "output_format") and (
            output_format := image_call.output_format
        ):
            mime_type = f"image/{output_format}"
        else:
            mime_type = "image/png"

        if hasattr(image_call, "size") and (size := image_call.size):
            width, height = tuple(size.split("x"))
        else:
            width, height = None, None

        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=mime_type,
            width=int(width) if width else None,
            height=int(height) if height else None,
            model=self.subentry.data.get(CONF_IMAGE_MODEL, RECOMMENDED_IMAGE_MODEL),
            revised_prompt=image_call.revised_prompt
            if hasattr(image_call, "revised_prompt")
            else None,
        )
