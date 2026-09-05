import os
import json
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

# Load environmental variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8080))

STATE_FILE = "state.json"

# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# Helper functions to persist channel state
def save_channel_id(channel_id):
    """Saves the active voice channel ID to local state."""
    with open(STATE_FILE, "w") as f:
        json.dump({"channel_id": channel_id}, f)

def clear_channel_id():
    """Clears the stored channel ID when leaving intentionally."""
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

def load_channel_id():
    """Loads the stored channel ID if available."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return data.get("channel_id")
        except Exception as e:
            print(f"Error reading state file: {e}")
    return None


class NativeSilenceSource(discord.AudioSource):
    """Generates pure inaudible silence directly in Python without FFmpeg.
    
    Discord voice expects 20ms frames at 48kHz, 16-bit stereo PCM.
    2 channels * 48000 samples/sec * 2 bytes/sample * 0.02 sec = 3840 bytes/frame.
    """
    def __init__(self, duration_ms=1000):
        self.bytes_per_frame = 3840
        self.total_frames = int(duration_ms / 20)
        self.sent_frames = 0

    def read(self):
        if self.sent_frames < self.total_frames:
            self.sent_frames += 1
            return b'\x00' * self.bytes_per_frame  # Pure zero-byte frame
        return b''


# Keep-Alive Audio Stream Task
@tasks.loop(seconds=5)
async def keep_audio_active():
    """Loops silent audio across all active voice clients to prevent AFK timeout."""
    for vc in bot.voice_clients:
        if vc.is_connected() and not vc.is_playing():
            try:
                vc.play(NativeSilenceSource(duration_ms=1000))
            except Exception as e:
                print(f"Audio stream error: {e}")

@keep_audio_active.before_loop
async def before_keep_audio():
    await bot.wait_until_ready()


async def handle_ping(request):
    return web.Response(text="Bot is active!", status=200)

async def start_web_server():
    """Starts a lightweight HTTP server on 0.0.0.0 using the exact port Render demands."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    
    # Configure runner
    runner = web.AppRunner(app)
    await runner.setup()
    
    # 0.0.0.0 is MANDATORY for Render to route external HTTP traffic
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server successfully listening on 0.0.0.0:{PORT}")


# Bot Events
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    
    # Start the audio loop task
    if not keep_audio_active.is_running():
        keep_audio_active.start()

    # Attempt to rejoin saved voice channel upon restart
    saved_channel_id = load_channel_id()
    if saved_channel_id and not bot.voice_clients:
        try:
            channel = bot.get_channel(saved_channel_id) or await bot.fetch_channel(saved_channel_id)
            if isinstance(channel, discord.VoiceChannel):
                await channel.connect(reconnect=True)
                print(f"Auto-rejoined voice channel: {channel.name} ({channel.id})")
        except Exception as e:
            print(f"Failed to auto-rejoin channel {saved_channel_id}: {e}")


# Admin Commands
@bot.command(name="join")
@commands.has_permissions(administrator=True)
async def join(ctx):
    """Joins the voice channel the caller is currently in and saves state."""
    if not ctx.author.voice:
        await ctx.send("You must be in a voice channel for me to join.")
        return

    channel = ctx.author.voice.channel
    
    if ctx.voice_client:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect(reconnect=True)

    # Save state to local file
    save_channel_id(channel.id)
    await ctx.send(f"Joined **{channel.name}**! Saved state for auto-reconnect.")

@bot.command(name="leave")
@commands.has_permissions(administrator=True)
async def leave(ctx):
    """Disconnects the bot from the voice channel and clears saved state."""
    clear_channel_id()
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Disconnected from the voice channel and cleared saved state.")
    else:
        await ctx.send("I am not in a voice channel.")


# Permission Error Handler
@join.error
@leave.error
async def admin_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Administrator** permissions to use this command.")


async def main():
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN is missing!")
        return

    # Start web server first so Render health checks pass
    await start_web_server()

    delay = 15
    max_retries = 10

    for attempt in range(1, max_retries + 1):
        try:
            print(f"Connecting to Discord Gateway (Attempt {attempt}/{max_retries})...")
            # Creating the bot context inside the loop ensures a fresh HTTP session per attempt
            async with discord.Client(intents=intents) as client:
                # Use bot.start directly inside its own managed session
                await bot.start(TOKEN)
            break
            
        except discord.errors.HTTPException as e:
            if e.status == 429 or "1015" in str(e):
                print(f"Cloudflare/Discord IP rate limited. Waiting {delay}s before retry...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)  # Exponential backoff capped at 2 minutes
            else:
                print(f"HTTP Exception during login: {e}")
                await asyncio.sleep(10)
                
        except discord.errors.LoginFailure:
            print("FATAL ERROR: Invalid Discord Token! Reset your token in Developer Portal and update Render.")
            break
            
        except discord.errors.PrivilegedIntentsRequired:
            print("FATAL ERROR: Enable 'Message Content Intent' in Discord Developer Portal > Bot tab!")
            break
            
        except Exception as e:
            print(f"Startup error: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())