"""Run: python examples/async_chat.py"""

import asyncio

from duckai import AsyncDuckAI


async def main():
    async with AsyncDuckAI() as ai:
        conversation = ai.conversation()
        print((await conversation.chat("Remember this project name: Later.")).text)
        async with conversation.stream("What project name did I give you?") as stream:
            async for chunk in stream:
                print(chunk, end="", flush=True)
        print()


if __name__ == "__main__":
    asyncio.run(main())
