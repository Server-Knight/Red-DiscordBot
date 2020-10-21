from typing import Optional
from datetime import timezone, timedelta, datetime
from .abc import MixinMeta

import discord
from redbot.core import commands, checks, i18n, modlog
from redbot.core.utils.chat_formatting import (
    humanize_timedelta,
    humanize_list,
    pagify,
    format_perms_list,
)
from redbot.core.utils.mod import get_audit_reason

from .converters import MuteTime

_ = i18n.Translator("Mutes", __file__)


class VoiceMutes(MixinMeta):
    """
    This handles all voice channel related muting
    """

    @staticmethod
    async def _voice_perm_check(
        ctx: commands.Context, user_voice_state: Optional[discord.VoiceState], **perms: bool
    ) -> bool:
        """Check if the bot and user have sufficient permissions for voicebans.

        This also verifies that the user's voice state and connected
        channel are not ``None``.

        Returns
        -------
        bool
            ``True`` if the permissions are sufficient and the user has
            a valid voice state.

        """
        if user_voice_state is None or user_voice_state.channel is None:
            await ctx.send(_("That user is not in a voice channel."))
            return False
        voice_channel: discord.VoiceChannel = user_voice_state.channel
        required_perms = discord.Permissions()
        required_perms.update(**perms)
        if not voice_channel.permissions_for(ctx.me) >= required_perms:
            await ctx.send(
                _("I require the {perms} permission(s) in that user's channel to do that.").format(
                    perms=format_perms_list(required_perms)
                )
            )
            return False
        if (
            ctx.permission_state is commands.PermState.NORMAL
            and not voice_channel.permissions_for(ctx.author) >= required_perms
        ):
            await ctx.send(
                _(
                    "You must have the {perms} permission(s) in that user's channel to use this "
                    "command."
                ).format(perms=format_perms_list(required_perms))
            )
            return False
        return True

    @commands.command(name="voicemute")
    @commands.guild_only()
    async def voice_mute(
        self,
        ctx: commands.Context,
        users: commands.Greedy[discord.Member],
        *,
        time_and_reason: MuteTime = {},
    ):
        """Mute a user in their current voice channel."""
        if not users:
            return await ctx.send_help()
        if ctx.me in users:
            return await ctx.send(_("You cannot mute me."))
        if ctx.author in users:
            return await ctx.send(_("You cannot mute yourself."))
        async with ctx.typing():
            success_list = []
            issue_list = []
            for user in users:
                user_voice_state = user.voice

                if (
                    await self._voice_perm_check(
                        ctx, user_voice_state, mute_members=True, manage_channels=True
                    )
                    is False
                ):
                    continue
                duration = time_and_reason.get("duration", {})
                reason = time_and_reason.get("reason", None)
                until = None
                time = ""
                if duration:
                    until = datetime.now(timezone.utc) + timedelta(**duration)
                    time = _(" for {duration}").format(
                        duration=humanize_timedelta(timedelta=timedelta(**duration))
                    )
                else:
                    default_duration = await self.config.guild(ctx.guild).default_time()
                    if default_duration:
                        until = datetime.now(timezone.utc) + timedelta(**default_duration)
                        time = _(" for {duration}").format(
                            duration=humanize_timedelta(timedelta=timedelta(**default_duration))
                        )
                guild = ctx.guild
                author = ctx.author
                channel = user_voice_state.channel
                audit_reason = get_audit_reason(author, reason)

                success = await self.channel_mute_user(
                    guild, channel, author, user, until, audit_reason
                )

                if success["success"]:
                    if "reason" in success and success["reason"]:
                        issue_list.append((user, success["reason"]))
                    else:
                        success_list.append(user)
                    await modlog.create_case(
                        self.bot,
                        guild,
                        ctx.message.created_at.replace(tzinfo=timezone.utc),
                        "vmute",
                        user,
                        author,
                        reason,
                        until=None,
                        channel=channel,
                    )
                    async with self.config.member(user).perms_cache() as cache:
                        cache[channel.id] = success["old_overs"]
                else:
                    issue_list.append((user, success["reason"]))

            if success_list:
                msg = _("{users} has been muted in this channel{time}.")
                if len(success_list) > 1:
                    msg = _("{users} have been muted in this channel{time}.")
                await ctx.send(
                    msg.format(users=humanize_list([f"{u}" for u in success_list]), time=time)
                )
            if issue_list:
                msg = _("The following users could not be muted\n")
                for user, issue in issue_list:
                    msg += f"{user}: {issue}\n"
                await ctx.send_interactive(pagify(msg))

    @commands.command(name="voiceunmute")
    @commands.guild_only()
    async def unmute_voice(
        self,
        ctx: commands.Context,
        users: commands.Greedy[discord.Member],
        *,
        reason: Optional[str] = None,
    ):
        """Unmute a user in their current voice channel."""
        if not users:
            return await ctx.send_help()
        if ctx.me in users:
            return await ctx.send(_("You cannot unmute me."))
        if ctx.author in users:
            return await ctx.send(_("You cannot unmute yourself."))
        async with ctx.typing():
            issue_list = []
            success_list = []
            for user in users:
                user_voice_state = user.voice
                if (
                    await self._voice_perm_check(
                        ctx, user_voice_state, mute_members=True, manage_channels=True
                    )
                    is False
                ):
                    continue
                guild = ctx.guild
                author = ctx.author
                channel = user_voice_state.channel
                audit_reason = get_audit_reason(author, reason)

                success = await self.channel_unmute_user(
                    guild, channel, author, user, audit_reason
                )

                if success["success"]:
                    if "reason" in success and success["reason"]:
                        issue_list.append((user, success["reason"]))
                    else:
                        success_list.append(user)
                    await modlog.create_case(
                        self.bot,
                        guild,
                        ctx.message.created_at.replace(tzinfo=timezone.utc),
                        "vunmute",
                        user,
                        author,
                        reason,
                        until=None,
                        channel=channel,
                    )
                else:
                    issue_list.append((user, success["reason"]))
            if success_list:
                if channel.id in self._channel_mutes and self._channel_mutes[channel.id]:
                    await self.config.channel(channel).muted_users.set(
                        self._channel_mutes[channel.id]
                    )
                else:
                    await self.config.channel(channel).muted_users.clear()
                await ctx.send(
                    _("{users} unmuted in this channel.").format(
                        users=humanize_list([f"{u}" for u in success_list])
                    )
                )
            if issue_list:
                message = _(
                    "{users} could not be unmuted in this channels. " "Would you like to see why?"
                ).format(users=humanize_list([f"{u}" for u, x in issue_list]))
                await self.handle_issues(ctx, message, humanize_list(x for u, x in issue_list))
