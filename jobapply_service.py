"""Discord bot + authenticated intake. Run one instance on a persistent host."""
import asyncio
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks
from aiohttp import web
from jobapply_skill import prepare_candidate
from jobapply_store import Store
from jobapply_forms import FormQueue, threshold, transmission_enabled
from jobapply_form_ui import FormReview, send_cards


def approval_note(bot, row, unattended):
    locked = 'Resume approved and locked to this version. '
    if not bot.forms_enabled:
        return locked + ('Nothing has been submitted. Employer form inspection will '
                         'queue when its worker is enabled.')
    if not transmission_enabled():
        return locked + (
            'Inspection-only mode is active. The employer form may be inspected and '
            'reviewed, but no values, files or button clicks can be transmitted.')
    if not unattended:
        return locked + ('Nothing has been submitted. The employer form will be inspected '
                         'and prefilled from your profile, then wait for your approval.')
    score = (json.loads(row['job']).get('fit') or {}).get('score')
    ranked = isinstance(score, (int, float)) and not isinstance(score, bool)
    if ranked and score >= threshold():
        return locked + (f'Auto-apply authorized. At {score}% this can run unattended only '
                         'when the employer ATS is explicitly transmission-allowlisted: the form '
                         'is prefilled from your profile and submitted without another click. '
                         'It stops for a login or verification challenge, a changed form, or '
                         'any required answer your profile does not cover.')
    return locked + (f'Auto-apply recorded, but this match ({score if ranked else "unscored"}) '
                     f'is below the {threshold():g}% auto-submit threshold. The prefilled form '
                     'will still wait for your approval.')


async def review_allowed(interaction, bot):
    if interaction.channel_id != bot.channel_id:
        await interaction.response.send_message('Wrong approval channel.', ephemeral=True)
        return False
    if str(interaction.user.id) != str(bot.owner_id):
        await interaction.response.send_message(
            'Only the configured owner can review this application.', ephemeral=True)
        return False
    return True


class RetryPreparation(discord.ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=None)
        retry = discord.ui.Button(
            label='Retry preparation',
            style=discord.ButtonStyle.primary,
            custom_id=f"ja:{row['id']}:prepare:retry",
        )
        skip = discord.ui.Button(
            label='Skip',
            style=discord.ButtonStyle.danger,
            custom_id=f"ja:{row['id']}:prepare:skip",
        )

        async def retry_callback(interaction):
            if not await review_allowed(interaction, bot):
                return
            try:
                bot.store.retry_skill(row['id'], interaction.user.id, bot.owner_id)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            kwargs = {'view': self}
            if interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.description = (
                    'Retry queued. Claude will prepare a new evidence-reviewed draft. '
                    'Nothing has been submitted.'
                )
                kwargs['embed'] = embed
            await interaction.response.edit_message(**kwargs)

        async def skip_callback(interaction):
            if not await review_allowed(interaction, bot):
                return
            try:
                bot.store.skip_pending(row['id'], interaction.user.id, bot.owner_id)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                'Skipped. No resume was approved and nothing was submitted.',
                ephemeral=True,
            )

        retry.callback = retry_callback
        skip.callback = skip_callback
        self.add_item(retry)
        self.add_item(skip)


class ChangeRequestModal(discord.ui.Modal):
    def __init__(self, bot, row):
        super().__init__(
            title='Request resume changes',
            custom_id=f"ja:{row['id']}:{row['digest'][:16]}:change-modal",
        )
        self.bot = bot
        self.row = row
        self.details = discord.ui.TextInput(
            label='What should Claude change?',
            style=discord.TextStyle.paragraph,
            placeholder='Example: emphasize patient scheduling and shorten the summary.',
            min_length=5,
            max_length=1000,
            required=True,
        )
        self.add_item(self.details)

    async def on_submit(self, interaction):
        if not await review_allowed(interaction, self.bot):
            return
        try:
            self.bot.store.decide_draft(
                self.row['id'],
                self.row['digest'],
                'changes',
                interaction.user.id,
                self.bot.owner_id,
                str(self.details),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            'Changes saved. Claude will create a new evidence-reviewed version. '
            'Nothing has been submitted.',
            ephemeral=True,
        )
        try:
            channel = (self.bot.get_channel(self.bot.channel_id) or
                       await self.bot.fetch_channel(self.bot.channel_id))
            message = await channel.fetch_message(int(self.row['message_id']))
            await message.edit(view=None)
        except (discord.HTTPException, OSError, TypeError):
            print('[JOB-APPLY] could not disable the outdated draft card')


class DraftReview(discord.ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=None)
        auto = discord.ui.Button(
            label='Approve & auto-apply',
            style=discord.ButtonStyle.success,
            custom_id=f"ja:{row['id']}:{row['digest'][:16]}:approve-auto",
        )
        approve = discord.ui.Button(
            label='Approve resume only',
            style=discord.ButtonStyle.secondary,
            custom_id=f"ja:{row['id']}:{row['digest'][:16]}:approve-resume",
        )
        changes = discord.ui.Button(
            label='Request changes',
            style=discord.ButtonStyle.secondary,
            custom_id=f"ja:{row['id']}:{row['digest'][:16]}:draft-changes",
        )
        skip = discord.ui.Button(
            label='Skip',
            style=discord.ButtonStyle.danger,
            custom_id=f"ja:{row['id']}:{row['digest'][:16]}:draft-skip",
        )

        def approval(unattended):
            async def callback(interaction):
                if not await review_allowed(interaction, bot):
                    return
                try:
                    bot.store.decide_draft(
                        row['id'], row['digest'], 'approve',
                        interaction.user.id, bot.owner_id, auto=unattended,
                    )
                except ValueError as exc:
                    await interaction.response.send_message(str(exc), ephemeral=True)
                    return
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(
                    approval_note(bot, row, unattended), ephemeral=True)
            return callback

        async def changes_callback(interaction):
            if not await review_allowed(interaction, bot):
                return
            await interaction.response.send_modal(ChangeRequestModal(bot, row))

        async def skip_callback(interaction):
            if not await review_allowed(interaction, bot):
                return
            try:
                bot.store.decide_draft(
                    row['id'], row['digest'], 'skip',
                    interaction.user.id, bot.owner_id,
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                'Skipped. Nothing has been submitted.', ephemeral=True)

        auto.callback = approval(True)
        approve.callback = approval(False)
        changes.callback = changes_callback
        skip.callback = skip_callback
        self.add_item(auto)
        self.add_item(approve)
        self.add_item(changes)
        self.add_item(skip)


class Review(discord.ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=None)
        for action, label, style in [('apply', 'Apply', discord.ButtonStyle.success),
                                     ('changes', 'Request changes', discord.ButtonStyle.secondary),
                                     ('skip', 'Skip', discord.ButtonStyle.danger)]:
            button = discord.ui.Button(label=label, style=style,
                custom_id=f"ja:{row['id']}:{row['digest'][:16]}:{action}")
            async def callback(interaction, action=action):
                if interaction.channel_id != bot.channel_id:
                    await interaction.response.send_message('Wrong approval channel.', ephemeral=True)
                    return
                try:
                    state = bot.store.decide(row['id'], row['digest'], action,
                                             interaction.user.id, bot.owner_id)
                except ValueError as exc:
                    await interaction.response.send_message(str(exc), ephemeral=True)
                    return
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(view=self)
                note = {'approved_waiting_adapter': 'Approval saved. Not submitted: the employer submission adapter is not connected yet.',
                        'changes_requested': 'Changes requested. A revised packet will require fresh approval.',
                        'skipped': 'Skipped. No application submitted.'}[state]
                await interaction.followup.send(note, ephemeral=True)
            button.callback = callback
            self.add_item(button)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.store = Store(os.environ['JOB_APPLY_DB'])
        self.forms = FormQueue(self.store)
        self.forms_enabled = os.environ.get('JOB_APPLY_FORMS_ENABLED', '').lower() == 'true'
        self.channel_id = int(os.environ['JOB_APPLY_CHANNEL_ID'])
        self.owner_id = int(os.environ['JOB_APPLY_OWNER_ID'])
        self.ingest_token = os.environ['JOB_APPLY_INGEST_TOKEN']
        self.skill_enabled = os.environ.get('JOB_APPLY_PREPARE_ENABLED', '').lower() == 'true'
        self.skill_path = os.environ.get(
            'JOB_APPLY_SKILL_PATH',
            'skills/healthcare-career-strategist/SKILL.md',
        )
        if len(self.ingest_token) < 32:
            raise ValueError('Use an intake token of at least 32 characters')
        if self.skill_enabled and not (os.environ.get('ANTHROPIC_API_KEY') and
                                       os.environ.get('ANTHROPIC_MODEL')):
            raise ValueError('Claude preparation requires ANTHROPIC_API_KEY and ANTHROPIC_MODEL')

    async def setup_hook(self):
        self.forms.recover()
        if self.forms_enabled:
            for row in self.forms.list(('review','needs_attention')):
                if row['message_id']:
                    self.add_view(FormReview(self, row), message_id=int(row['message_id']))
        with self.store.connect() as db:
            for row in db.execute(
                    "SELECT * FROM applications WHERE state='ready' "
                    "AND message_id IS NOT NULL"):
                self.add_view(Review(self, dict(row)), message_id=int(row['message_id']))
            for row in db.execute(
                    "SELECT * FROM applications WHERE state='draft_ready' "
                    "AND message_id IS NOT NULL"):
                self.add_view(DraftReview(self, dict(row)),
                              message_id=int(row['message_id']))
            for row in db.execute(
                    "SELECT * FROM applications WHERE state IN "
                    "('awaiting_skill','changes_requested') "
                    "AND prepare_error IS NOT NULL AND message_id IS NOT NULL"):
                self.add_view(RetryPreparation(self, dict(row)),
                              message_id=int(row['message_id']))
        app = web.Application(client_max_size=512 * 1024)
        app.router.add_post('/candidates', self.intake)
        app.router.add_get('/healthz', self.health)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', int(os.environ.get('PORT', '8080'))).start()
        self.cards.start()
        if self.skill_enabled:
            self.prepares.start()
        if self.forms_enabled:
            self.form_worker.start()
            self.form_cards.start()

    async def health(self, request):
        worker_ready = not self.skill_enabled or self.prepares.is_running()
        forms_ready = not self.forms_enabled or (self.form_worker.is_running() and self.form_cards.is_running())
        ready = self.is_ready() and self.cards.is_running() and worker_ready and forms_ready
        return web.json_response({'ready': ready}, status=200 if ready else 503)

    async def intake(self, request):
        expected = 'Bearer ' + self.ingest_token
        if not hmac.compare_digest(request.headers.get('Authorization', ''), expected):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
            key = self.store.enqueue(payload['id'], payload['job'])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPBadRequest(text='Invalid candidate')
        return web.json_response({'id': key, 'queued': True})

    @tasks.loop(seconds=30)
    async def cards(self):
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            for row in self.store.pending_cards():
                job = json.loads(row['job'])
                embed = discord.Embed(title=f"{job['title']} — {job.get('company', '')}"[:256],
                                      url=job['url'], color=0x58A6FF)
                embed.add_field(name='Match', value=f"{job['fit']['score']}%")
                kwargs = {'embed': embed}
                if row['state'] == 'awaiting_skill':
                    if row.get('prepare_error'):
                        embed.description = (
                            'Resume preparation failed: ' + row['prepare_error'] +
                            '\nUse Retry preparation after fixing the issue. '
                            'Nothing has been submitted.'
                        )
                        kwargs['view'] = RetryPreparation(self, row)
                    else:
                        embed.description = (
                            'Queued. Waiting for the Claude career skill and '
                            'evidence review. Nothing has been submitted.'
                        )
                else:
                    if row['state'] == 'draft_ready':
                        embed.description = (
                            'Evidence-reviewed resume draft is attached. Review the '
                            'PDF, DOCX, and evidence packet. Approve & auto-apply '
                            'locks this version, fills the employer form from your '
                            'profile and submits it unattended at or above the match '
                            'threshold. Approve resume only locks the version and '
                            'waits for your approval at the form.'
                        )
                        kwargs['view'] = DraftReview(self, row)
                    else:
                        embed.description = (
                            'Review the attached resume and complete answer/evidence '
                            'packet. Apply records approval of this exact version. '
                            'Employer submission is not connected yet.'
                        )
                    embed.set_footer(text='Version ' + row['digest'][:16])
                    kwargs['files'] = [
                        discord.File(io.BytesIO(row['resume']),
                                     filename='tailored-resume.pdf')
                    ]
                    if row.get('resume_docx'):
                        kwargs['files'].append(
                            discord.File(io.BytesIO(row['resume_docx']),
                                         filename='tailored-resume.docx')
                        )
                    kwargs['files'].append(
                        discord.File(io.BytesIO(row['packet'].encode()),
                                     filename='application-answers-and-evidence.json')
                    )
                    if row['state'] == 'ready':
                        kwargs['view'] = Review(self, row)
                message = await channel.send(**kwargs)
                self.store.mark_card(row['id'], row['state'], row['digest'], message.id)
        except (discord.HTTPException, OSError):
            print('[JOB-APPLY] card delivery failed; retrying next poll')

    @cards.before_loop
    async def before_cards(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=60)
    async def prepares(self):
        for row in self.store.pending_skill(limit=1):
            now = datetime.now(timezone.utc)
            lease_until = (now + timedelta(minutes=5)).isoformat()
            if not self.store.lease_skill(row['id'], lease_until):
                continue
            try:
                await asyncio.to_thread(prepare_candidate, self.store, row['id'], self.skill_path)
                print(f"[JOB-APPLY] prepared evidence-bound draft for {row['id']}")
            except Exception as exc:
                retry_after = (now + timedelta(minutes=30)).isoformat()
                safe_error = self.store.skill_failed(row['id'], exc, retry_after)
                print(f"[JOB-APPLY] draft preparation failed for {row['id']}: {safe_error}")
                await self.show_skill_failure(row['id'])

    async def show_skill_failure(self, key):
        row = self.store.get(key)
        if not row.get('message_id'):
            return
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            message = await channel.fetch_message(int(row['message_id']))
            if message.embeds:
                embed = message.embeds[0]
            else:
                job = json.loads(row['job'])
                embed = discord.Embed(
                    title=f"{job['title']} — {job.get('company', '')}"[:256],
                    url=job['url'],
                    color=0xF85149,
                )
            embed.color = discord.Color(0xF85149)
            embed.description = (
                'Resume preparation failed: ' + row['prepare_error'] +
                '\nUse Retry preparation after fixing the issue. '
                'Nothing has been submitted.'
            )
            await message.edit(embed=embed, view=RetryPreparation(self, row))
        except (discord.HTTPException, OSError, TypeError):
            print('[JOB-APPLY] could not update the failed preparation card')

    @prepares.before_loop
    async def before_prepares(self):
        await self.wait_until_ready()

    async def close(self):
        self.cards.cancel()
        self.form_worker.cancel()
        self.form_cards.cancel()
        if self.prepares.is_running():
            self.prepares.cancel()
        if hasattr(self, 'runner'):
            await self.runner.cleanup()
        await super().close()

    @tasks.loop(seconds=30)
    async def form_worker(self):
        from jobapply_browser import run_one
        try:
            await asyncio.to_thread(run_one, self.forms)
        except Exception:
            print('[JOB-APPLY] form worker unavailable; check browser installation')

    @form_worker.before_loop
    async def before_form_worker(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def form_cards(self):
        try:
            await send_cards(self)
        except (discord.HTTPException, OSError):
            print('[JOB-APPLY] form card delivery failed; retrying next poll')

    @form_cards.before_loop
    async def before_form_cards(self):
        await self.wait_until_ready()


if __name__ == '__main__':
    Bot().run(os.environ['DISCORD_BOT_TOKEN'])
