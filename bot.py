import os
import json
import sys
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

# Force unbuffered stdout so Render displays logs immediately
sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))
STATE_FILE = "state.json"


def save_channel_id(channel_id):
    with open(STATE_FILE, "w") as f:
        json.dump({"channel_id": channel_id}, f)

def clear_channel_id():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

def load_channel_id():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return data.get("channel_id")
        except Exception as e:
            print(f"[STATE] Error reading state file: {e}")
    return None


class NativeSilenceSource(discord.AudioSource):
    def __init__(self, duration_ms=1000):
        self.bytes_per_frame = 3840
        self.total_frames = int(duration_ms / 20)
        self.sent_frames = 0

    def read(self):
        if self.sent_frames < self.total_frames:
            self.sent_frames += 1
            return b'\x00' * self.bytes_per_frame
        return b''


def create_bot():
    """Factory function to generate a fresh Bot instance with clean sessions."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.voice_states = True
    
    bot = commands.Bot(command_prefix="!", intents=intents)

    @tasks.loop(seconds=5)
    async def keep_audio_active():
        for vc in bot.voice_clients:
            if vc.is_connected() and not vc.is_playing():
                try:
                    vc.play(NativeSilenceSource(duration_ms=1000))
                except Exception as e:
                    print(f"[AUDIO] Stream error: {e}")

    @keep_audio_active.before_loop
    async def before_keep_audio():
        await bot.wait_until_ready()

    @bot.event
    async def on_ready():
        print(f"[DISCORD] SUCCESS! Logged in as {bot.user.name} (ID: {bot.user.id})")
        
        if not keep_audio_active.is_running():
            keep_audio_active.start()

        saved_channel_id = load_channel_id()
        if saved_channel_id and not bot.voice_clients:
            try:
                channel = bot.get_channel(saved_channel_id) or await bot.fetch_channel(saved_channel_id)
                if isinstance(channel, discord.VoiceChannel):
                    await channel.connect(reconnect=True)
                    print(f"[DISCORD] Auto-rejoined channel: {channel.name}")
            except Exception as e:
                print(f"[DISCORD] Auto-rejoin error: {e}")

    @bot.command(name="join")
    @commands.has_permissions(administrator=True)
    async def join(ctx):
        if not ctx.author.voice:
            await ctx.send("You must be in a voice channel for me to join.")
            return

        channel = ctx.author.voice.channel
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect(reconnect=True)

        save_channel_id(channel.id)
        await ctx.send(f"Joined **{channel.name}**! Saved state for auto-reconnect.")

    @bot.command(name="leave")
    @commands.has_permissions(administrator=True)
    async def leave(ctx):
        clear_channel_id()
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("Disconnected and cleared saved state.")
        else:
            await ctx.send("I am not in a voice channel.")
    @bot.command(name="ping")
    async def ping(ctx):
        await ctx.send("Pong!")    

    return bot


async def handle_ping(request):
    return web.Response(text="Bot service active!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Reuse port flag allows clean restarts without Errno 98
    site = web.TCPSite(runner, "0.0.0.0", PORT, reuse_port=True)
    await site.start()
    print(f"[WEB] Web server listening on 0.0.0.0:{PORT}")



async def main():
    if not TOKEN:
        print("[ERROR] DISCORD_TOKEN environment variable is missing on Render!")
        return

    # Start health check server once
    await start_web_server()

    delay = 30
    max_retries = 10

    for attempt in range(1, max_retries + 1):
        print(f"[DISCORD] Connection attempt {attempt}/{max_retries}...")
        bot = create_bot()
        
        try:
            async with bot:
                await bot.start(TOKEN)
            break  # Clean exit if bot stops normally
            
        except discord.errors.HTTPException as e:
            if e.status == 429 or "1015" in str(e):
                print(f"[RATE LIMIT] Cloudflare/Discord IP rate limited. Cool-down waiting {delay}s...")
            else:
                print(f"[HTTP ERROR] {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)  # Exponential backoff up to 5 mins
            
        except discord.errors.LoginFailure:
            print("[FATAL] Invalid Discord Token! Reset token in Developer Portal and update Render.")
            break
            
        except discord.errors.PrivilegedIntentsRequired:
            print("[FATAL] Enable 'Message Content Intent' in Discord Developer Portal > Bot tab!")
            break
            
        except Exception as e:
            print(f"[ERROR] Connection attempt failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

if __name__ == "__main__":
    asyncio.run(main())