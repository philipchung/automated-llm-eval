import asyncio
import logging
import warnings
from dataclasses import dataclass
from typing import NamedTuple

import httpx
import openai
from openai.types.chat.chat_completion import ChatCompletion

import private_key
from automated_llm_eval.utils import ProgressBar

chat_logger = logging.getLogger(name="ChatLogger")


class MessageBundle(NamedTuple):
    "Input messages & API call settings bundled with response messages and metadata."
    # ID Created by API Call
    id: str | None = None
    # Input Messages
    system_message: str | None = None
    user_message: str | None = None
    # Response Message
    response_message: str | None = None
    # Response Metadata
    created_time: int | None = None
    model: str | None = None
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Additional API Call Arguments
    seed: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


@dataclass(kw_only=True)
class ChatModel:
    """Wrapper around openai.ChatCompletion with concurrency limiting
    and exponential backoff retries."""

    # OpenAI API Config
    sync_client: openai.OpenAI = openai.OpenAI(
        api_key=private_key.key["open-ai"],
        max_retries=10,
        timeout=httpx.Timeout(60.0, read=5.0, write=10.0, connect=10.0),
    )
    async_client: openai.AsyncOpenAI = openai.AsyncOpenAI(
        api_key=private_key.key["open-ai"],
        max_retries=10,
        timeout=httpx.Timeout(60.0, read=5.0, write=10.0, connect=10.0),
    )
    # Model Config
    model: str = "gpt-3.5-turbo"
    temperature: float = 0.9
    top_p: float = 0.9
    max_tokens: int = None
    n: int = 1
    seed: int | None = None

    def create_chat_completion(
        self, system_message: str, user_message: str, **kwargs
    ) -> ChatCompletion | str | MessageBundle | dict | None:
        """Simplified Chat Completion call that packages `system_message` and `user_message`
        for us.

        Args:
            system_message (str): system message prompt
            user_message (str): user message prompt
        """
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]
        parsed_cc = self.chat_completion(messages=messages, **kwargs)
        return parsed_cc

    def parse_chat_completion_response(
        self,
        cc: ChatCompletion,
        output_format: str | None = "message_bundle_dict",
        **kwargs,
    ) -> ChatCompletion | str | MessageBundle | dict:
        """Parse ChatCompletion object.

        Args:
            output_format (str | None, optional): Controls format of output.
                `None`: return raw ChatCompletion object with no modification.
                `simple`: return only response message
                `message_bundle`: return namedtuple with input+output messages and ChatCompletion
                    metadata flattened as a namedtuple
                `message_bundle_dict`: same as `message_bundle`, but returns as a
                    dictionary.

        Returns:
            Either ChatCompletion, string response message, MessageBundle, or dict depending
            on `output_format`.
        """
        # If `messages` key, then split out system_message and user_message to separate fields
        if "messages" in kwargs:
            messages = kwargs.pop("messages")
            system_message = [m for m in messages if m["role"] == "system"][0]
            user_message = [m for m in messages if m["role"] == "user"][0]
            kwargs |= {
                "system_message": system_message["content"],
                "user_message": user_message["content"],
            }

        # Remove from kwargs to avoid duplicate if they are present in ChatCompletion
        for key in ("model", "n"):
            if key in kwargs:
                kwargs.pop(key)

        match output_format:
            case "simple":
                chat_completion_message = cc.choices[0].message.content
                return chat_completion_message
            case "message_bundle" | "message_bundle_dict":
                mb = MessageBundle(
                    id=cc.id,
                    response_message=cc.choices[0].message.content,
                    created_time=cc.created,
                    model=cc.model,
                    total_tokens=cc.usage.total_tokens,
                    prompt_tokens=cc.usage.prompt_tokens,
                    completion_tokens=cc.usage.completion_tokens,
                    **kwargs,
                )
                if output_format == "message_bundle_dict":
                    return mb._asdict()
                else:
                    return mb
            case None:
                return cc
            case _:
                return cc

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        output_format: str | None = None,
        num_retries: int = 5,
        **kwargs,
    ) -> ChatCompletion | str | MessageBundle | dict | None:
        """Calls OpenAI ChatCompletions API.
        https://platform.openai.com/docs/api-reference/chat/create

        This method uses properties declared on class as default arguments.
        Any keyword arguments directly passed in to `kwargs` will override
        the default arguments.

        Args:
            messages (list[dict[str, str]]): List of dict message format.
                ```python
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What is the capital of the USA?"},
                ],
                ```
            output_format (str | None, optional): Controls format of output.
                see method `parse_chat_completion_response`.
            num_retries (int): Number of retries if API call fails.  If still fails,
                then `None` is returned.

        Returns:
            Either ChatCompletion, string response message, MessageBundle, or dict depending
            on `output_format`.  If API call fails, returns `None`.
        """
        default_kwargs = {
            "messages": messages,
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "n": self.n,
            "seed": self.seed,
        }
        updated_kwargs = default_kwargs | kwargs
        try:
            cc = self.sync_client.chat.completions.create(**updated_kwargs)
            # Format API call response
            return self.parse_chat_completion_response(
                cc=cc, output_format=output_format, **updated_kwargs
            )
        except Exception as e:
            warnings.warn(
                f"Failed to create ChatCompletion with arguments: {updated_kwargs.items()}\n"
                f"Exception: {e}\n"
                f"Retries left: {num_retries}"
            )
            if num_retries > 0:
                # Decrement retry counter, recursively call this method
                return self.chat_completion(
                    **updated_kwargs, output_format=output_format, num_retries=num_retries - 1
                )
            else:
                return None

    def chat_completions(
        self, messages_list: list[list[dict[str, str]]], **kwargs
    ) -> list[ChatCompletion | str | MessageBundle | dict | None]:
        "Calls `chat_completion` multiple times and returns a list of ChatCompletion objects."
        cc_list = []
        with ProgressBar() as p:
            for message in p.track(messages_list, description="ChatCompletions"):
                cc = self.chat_completion(message, **kwargs)
                cc_list += [cc]
        return cc_list

    async def async_chat_completion(
        self,
        messages: list[dict[str, str]],
        output_format: str | None = None,
        num_retries: int = 5,
        **kwargs,
    ) -> ChatCompletion | str | MessageBundle | dict | None:
        "Same as `chat_completion` but using asynchronous (non-blocking) client."
        default_kwargs = {
            "messages": messages,
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "n": self.n,
            "seed": self.seed,
        }
        updated_kwargs = default_kwargs | kwargs
        try:
            cc = await self.async_client.chat.completions.create(**updated_kwargs)
            # Format API call response
            return self.parse_chat_completion_response(
                cc=cc, output_format=output_format, **updated_kwargs
            )
        except Exception as e:
            warnings.warn(
                f"Failed to create ChatCompletion with arguments: {updated_kwargs.items()}\n"
                f"Exception: {e}\n"
                f"Retries left: {num_retries}"
            )
            if num_retries > 0:
                # Decrement retry counter, recursively call this method
                return self.async_chat_completion(
                    **updated_kwargs, output_format=output_format, num_retries=num_retries - 1
                )
            else:
                return None

    async def async_chat_completions(
        self,
        messages_list: list[list[dict[str, str]]],
        num_concurrent: int = 5,
        timeout: int | None = None,
        **kwargs,
    ) -> list[ChatCompletion | str | MessageBundle | dict | None]:
        """Calls `async_chat_completion` multiple times and returns a list of
        ChatCompletion objects. Concurrency is controlled using `num_concurrent`."""

        async def generation_task(
            semaphore, messages, **kwargs
        ) -> ChatCompletion | str | MessageBundle | dict | None:
            "Wrap ChatCompletion API call with a blocking semaphore to control concurrency."
            async with semaphore:
                cc = await self.async_chat_completion(messages=messages, **kwargs)
                return cc

        async def generate_concurrent() -> list[ChatCompletion | str | MessageBundle | dict | None]:
            "Main task to schedule on asyncio event loop."
            # Create the shared semaphore
            semaphore = asyncio.BoundedSemaphore(num_concurrent)
            # Create and schedule tasks, limiting concurrent tasks with semaphore
            task_list = []
            for messages in messages_list:
                task = asyncio.create_task(
                    generation_task(semaphore=semaphore, messages=messages, **kwargs)
                )
                task_list += [task]
            # Await each task to complete with progress bar (returns in order of completion)
            with ProgressBar() as p:
                for task in p.track(
                    asyncio.as_completed(task_list),
                    description="ChatCompletions",
                    total=len(task_list),
                ):
                    await task
            # Await to ensure all tasks are done
            await asyncio.wait(task_list)
            # Return results in original order of tasks
            cc_list = [await task for task in task_list]
            return cc_list

        # Start the asyncio program
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 'RuntimeError: There is no current event loop...'
            loop = None

        # Schedule coroutine as a task on event loop if it already exists,
        # otherwise run coroutine on a new event loop
        if loop and loop.is_running():
            tsk = loop.create_task(generate_concurrent())
            await asyncio.wait_for(tsk, timeout=timeout)
            result = tsk.result()
        else:
            result = asyncio.run(generate_concurrent())
        return result