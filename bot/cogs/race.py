"""
Race cog — /upload-race slash command.

Status updates are ephemeral (only visible to the uploader).
The final video + results embed is posted publicly in the channel.
"""
import asyncio
import io
import os
import tempfile
import zipfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.db import get_session_info, get_final_results, get_participants, get_rdfl_timeline


ALLOWED_EXTENSIONS = {".zip", ".db"}
MAX_UPLOAD_BYTES   = 50 * 1024 * 1024   # 50 MB — reject before download
MAX_UNZIP_BYTES    = 200 * 1024 * 1024  # 200 MB — reject oversized zip contents
SQLITE_MAGIC       = b"SQLite format 3\x00"


def _check_role(member: discord.Member, allowed_roles: set[str]) -> bool:
    """Return True if member has at least one allowed role (or no restriction configured)."""
    if not allowed_roles:
        return True
    return bool({r.name for r in member.roles} & allowed_roles)


def _validate_zip(data: bytes) -> str:
    """Validate zip and return the name of the single .db file inside."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("File is not a valid zip archive.")

    infos = zf.infolist()
    for info in infos:
        if ".." in info.filename or info.filename.startswith("/"):
            raise ValueError("Zip contains suspicious paths.")

    db_files = [i for i in infos if i.filename.endswith(".db")]
    non_db   = [i for i in infos if not i.filename.endswith(".db")]
    if non_db:
        raise ValueError(f"Zip contains unexpected files: {[i.filename for i in non_db]}")
    if not db_files:
        raise ValueError("No .db file found inside the zip.")
    if len(db_files) > 1:
        raise ValueError("Zip contains more than one .db file.")

    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_UNZIP_BYTES:
        raise ValueError(
            f"Uncompressed content is too large "
            f"({total_uncompressed // 1024 // 1024} MB > {MAX_UNZIP_BYTES // 1024 // 1024} MB limit)."
        )

    return db_files[0].filename


def _extract_db(attachment_bytes: bytes, dest_dir: str) -> str:
    """Validate, extract, and return path to the .db file."""
    db_name = _validate_zip(attachment_bytes)
    with zipfile.ZipFile(io.BytesIO(attachment_bytes)) as zf:
        zf.extract(db_name, dest_dir)
    db_path = os.path.join(dest_dir, db_name)
    with open(db_path, "rb") as f:
        if f.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise ValueError("Extracted file is not a valid SQLite database.")
    return db_path


def _format_ms(ms: int) -> str:
    if ms <= 0:
        return "--:--.---"
    minutes, rem = divmod(ms, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{int(minutes)}:{int(seconds):02d}.{int(millis):03d}"


def _build_results_embed(session: dict, results: list, participants: list) -> discord.Embed:
    track = session["track_name"]
    embed = discord.Embed(title=f"🏁 {track} — Race Results", color=discord.Color.red())
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for row in results:
        pos    = row["position"]
        name   = row["name"]
        status = row["result_status"]
        best   = _format_ms(row["best_lap_ms"])
        pits   = row["num_pit_stops"]
        grid   = row["grid_position"] if row["grid_position"] else 0
        # Left-justify grid number in 2 chars so P7 and P12 stay aligned
        grid_tag = f"`P{grid:<2}`" if grid > 0 else "`P?`"
        finish   = medals.get(pos, f"P{pos}")
        if status in ("DNF", "Retired", "DSQ", "Not Classified"):
            lines.append(f"{grid_tag} -> {finish} {name} — _{status}_")
        else:
            lines.append(f"{grid_tag} -> {finish} {name} — Best: `{best}` | Pits: {pits}")
    embed.description = "\n".join(lines) if lines else "No classification data."
    return embed


class RaceCog(commands.Cog, name="Race"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.processing_lock = asyncio.Lock()
        self._queue_size = 0

    @app_commands.command(name="help", description="How to record and upload a race")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🏎️ F1 League Bot — Quick Start",
            color=discord.Color.red(),
        )
        embed.add_field(
            name="1️⃣  Download the recorder",
            value="Grab the latest `F1_Recorder.exe` from the [Releases page](https://github.com/zwan2016/f1_league_discord_bot/releases) and run it during the race.",
            inline=False,
        )
        embed.add_field(
            name="2️⃣  After the race",
            value="Press `Ctrl+C` in the recorder window — it will save a `race.zip` file automatically.",
            inline=False,
        )
        embed.add_field(
            name="3️⃣  Upload",
            value="Use `/upload-race` in this channel and attach the `race.zip` file.",
            inline=False,
        )
        embed.add_field(
            name="📖  Full setup guide",
            value="[github.com/zwan2016/f1_league_discord_bot](https://github.com/zwan2016/f1_league_discord_bot)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upload-race", description="Upload a race recording (.zip) to generate the animation")
    @app_commands.describe(file="The race recording zip exported by the recorder")
    async def upload_race(self, interaction: discord.Interaction, file: discord.Attachment):
        # Role restriction
        allowed_roles = getattr(self.bot, "allowed_roles", set())
        if not _check_role(interaction.user, allowed_roles):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
            return

        # File type check
        if Path(file.filename).suffix.lower() not in ALLOWED_EXTENSIONS:
            await interaction.response.send_message(
                "❌ Please upload a `.zip` or `.db` file.", ephemeral=True
            )
            return

        # Acknowledge immediately — all subsequent updates will be ephemeral
        await interaction.response.defer(ephemeral=True)
        asyncio.create_task(self._process_recording(interaction, file))

    async def _process_recording(
        self, interaction: discord.Interaction, attachment: discord.Attachment
    ) -> None:
        max_queue = getattr(self.bot, "max_queue_size", 5)
        if self._queue_size >= max_queue:
            await interaction.followup.send(
                f"⚠️ Too many uploads in progress ({self._queue_size}/{max_queue}). "
                "Please try again in a few minutes.",
                ephemeral=True,
            )
            return

        self._queue_size += 1
        status_msg = None

        try:
            if self._queue_size > 1:
                status_msg = await interaction.followup.send(
                    f"⏳ Queued — {self._queue_size - 1} task(s) ahead of you...",
                    ephemeral=True,
                )

            async with self.processing_lock:
                await self._do_process(interaction, attachment, status_msg)
        finally:
            self._queue_size -= 1

    async def _do_process(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
        status_msg: discord.WebhookMessage | None,
    ) -> None:
        async def _update(text: str) -> None:
            """Edit existing ephemeral status or send a new one."""
            nonlocal status_msg
            if status_msg:
                await status_msg.edit(content=text)
            else:
                status_msg = await interaction.followup.send(text, ephemeral=True)

        if attachment.size > MAX_UPLOAD_BYTES:
            await _update(
                f"❌ File too large ({attachment.size // 1024 // 1024} MB). "
                f"Max allowed: {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
            )
            return

        await _update("📡 Recording received — extracting data...")
        try:
            data = await attachment.read()
        except discord.HTTPException as e:
            await _update(f"❌ Failed to download file: {e}")
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                if attachment.filename.endswith(".zip"):
                    db_path = _extract_db(data, tmpdir)
                else:
                    db_path = os.path.join(tmpdir, attachment.filename)
                    Path(db_path).write_bytes(data)
                    with open(db_path, "rb") as f:
                        if f.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                            await _update("❌ File is not a valid SQLite database.")
                            return
            except ValueError as e:
                await _update(f"❌ {e}")
                return
            except Exception as e:
                await _update(f"❌ Could not read recording: {e}")
                return

            await _update("🔍 Parsing race data...")
            try:
                session = await get_session_info(db_path)
                if not session:
                    await _update("❌ No session data found in recording.")
                    return
                session_uid  = session["session_uid"]
                results      = await get_final_results(db_path, session_uid)
                participants = await get_participants(db_path, session_uid)
            except Exception as e:
                await _update(f"❌ DB read error: {e}")
                return

            embed = _build_results_embed(dict(session), results, participants)

            await _update("🎨 Generating race animation — this may take up to 3 minutes...")
            try:
                mp4_path = await asyncio.get_event_loop().run_in_executor(
                    None, self._generate_animation, db_path, session_uid, tmpdir
                )
            except Exception as e:
                print(f"[race cog] Animation generation failed: {e}")
                mp4_path = None

            # Post result publicly in the channel
            if mp4_path and Path(mp4_path).exists():
                mp4_file = discord.File(mp4_path, filename="race_animation.mp4")
                await interaction.channel.send(embed=embed, file=mp4_file)
            else:
                await interaction.channel.send(embed=embed)

            # Close out the ephemeral status
            await _update("✅ Done! Results posted above.")

    def _generate_animation(self, db_path: str, session_uid: int, out_dir: str) -> str:
        """Blocking call — runs in executor. Returns path to generated mp4."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            from bot.utils.db import get_session_info, get_lap_snapshots, get_sc_timeline, get_final_results, get_ftlp_timeline, get_rdfl_timeline
            session_info  = loop.run_until_complete(get_session_info(db_path, session_uid))
            snapshots     = loop.run_until_complete(get_lap_snapshots(db_path, session_uid))
            sc_timeline   = loop.run_until_complete(get_sc_timeline(db_path, session_uid))
            final_results = loop.run_until_complete(get_final_results(db_path, session_uid))
            ftlp_timeline = loop.run_until_complete(get_ftlp_timeline(db_path, session_uid))
            rdfl_timeline = loop.run_until_complete(get_rdfl_timeline(db_path, session_uid))
        finally:
            loop.close()

        track_id   = session_info["track_id"]   if session_info and "track_id"   in session_info.keys() else -1
        track_name = session_info["track_name"] if session_info and "track_name" in session_info.keys() else ""
        total_laps = session_info["total_laps"] if session_info and "total_laps" in session_info.keys() else 0

        final_positions = {r["car_index"]: r["position"] for r in final_results} if final_results else None

        from visualizer.race_animation import build_mp4
        out_path = os.path.join(out_dir, "race_animation.mp4")
        build_mp4(snapshots, out_path, sc_timeline=sc_timeline,
                  total_laps=total_laps,
                  track_id=track_id, track_name=track_name,
                  final_positions=final_positions,
                  ftlp_timeline=ftlp_timeline,
                  grid_positions=None,
                  rdfl_timeline=rdfl_timeline)
        return out_path


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RaceCog(bot))
