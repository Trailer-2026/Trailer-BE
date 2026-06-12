import httpx
from pydantic import BaseModel
from typing import List, Optional

class Embed(BaseModel):
    title: str
    description: str
    color: int = 15158332

class DiscordMessage(BaseModel):
    content: Optional[str] = None
    embeds: List[Embed]

async def send_discord_alarm(webhook_url: str, message: DiscordMessage):
    if not webhook_url: return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(webhook_url, json=message.model_dump())
        except Exception as e:
            print(f"Discord error: {e}")