import aiohttp
from io import BytesIO

import discord
from discord import app_commands


PETPET_ORIGIN = "https://ptc.pwn3t.ru"


def register(bot: discord.Client) -> None:
    if bot.tree.get_command("petpet") is not None:
        return

    @bot.tree.command(
        name="petpet",
        description="Hazle petpet al avatar de un usuario.",
    )
    @app_commands.describe(user="Usuario cuyo avatar quieres acariciar.")
    async def petpet(interaction: discord.Interaction, user: discord.User | None = None):
        target = user or interaction.user
        await interaction.response.defer()

        url = f"{PETPET_ORIGIN}/{target.id}.gif"
        timeout = aiohttp.ClientTimeout(total=30)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"PetPet HTTP {response.status}")
                    data = await response.read()

            if not data:
                raise RuntimeError("PetPet devolvio un GIF vacio")

            await interaction.followup.send(
                content=f"Petpet de **{target.display_name}**",
                file=discord.File(BytesIO(data), filename="petpet.gif"),
            )
        except Exception as error:
            print("petpet:", error)
            await interaction.followup.send(
                "No pude generar el petpet ahora. Intenta de nuevo en unos segundos."
            )
