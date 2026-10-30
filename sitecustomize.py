"""Load the petpet command without changing the existing bot entrypoint."""

try:
    from discord.ext import commands
    import petpet

    _original_init = commands.Bot.__init__

    def _init_with_petpet(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        petpet.register(self)

    commands.Bot.__init__ = _init_with_petpet
except Exception as error:
    print("petpet startup hook:", error)
