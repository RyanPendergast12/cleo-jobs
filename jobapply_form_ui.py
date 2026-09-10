"""Discord-only review and answer collection for the employer form queue."""
import io
import json
from urllib.parse import urlsplit
import discord


async def allowed(interaction, bot):
    if interaction.channel_id != bot.channel_id or str(interaction.user.id) != str(bot.owner_id):
        await interaction.response.send_message('Only the owner in the approval channel can do this.', ephemeral=True)
        return False
    return True


class AnswerModal(discord.ui.Modal):
    def __init__(self, bot, row):
        super().__init__(title='Answer an application question')
        self.bot, self.row = bot, row
        self.number = discord.ui.TextInput(label='Field number from the attached form packet', max_length=3)
        self.value = discord.ui.TextInput(label='Answer (checkbox: true/false)', style=discord.TextStyle.paragraph, max_length=4000)
        self.add_item(self.number)
        self.add_item(self.value)

    async def on_submit(self, interaction):
        if not await allowed(interaction, self.bot):
            return
        try:
            plan = json.loads(self.row['plan'])
            index = int(str(self.number)) - 1
            if index < 0 or index >= len(plan['snapshot']['fields']):
                raise ValueError('Invalid field number')
            field = plan['snapshot']['fields'][index]
            value = str(self.value).strip()
            if field['type'] == 'checkbox':
                if value.lower() not in ('true','false'):
                    raise ValueError('Use true or false for a checkbox')
                value = value.lower() == 'true'
            self.bot.forms.answer(self.row['id'], self.row['version'], field['id'], value,
                                  interaction.user.id, self.bot.owner_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message('Answer saved. A fresh review card will appear shortly.', ephemeral=True)


class DestinationModal(discord.ui.Modal):
    def __init__(self, bot, row):
        super().__init__(title='Set direct employer application URL')
        self.bot, self.row = bot, row
        self.url = discord.ui.TextInput(label='LinkedIn or direct employer application URL', max_length=1500)
        self.add_item(self.url)

    async def on_submit(self, interaction):
        if not await allowed(interaction, self.bot):
            return
        try:
            self.bot.forms.set_target(self.row['id'], str(self.url).strip(), interaction.user.id, self.bot.owner_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message('Destination saved. Read-only form inspection queued.', ephemeral=True)


class FormReview(discord.ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=None)
        if row['state'] == 'needs_attention':
            button = discord.ui.Button(label='Set employer URL / reinspect', custom_id=f"jf:{row['id']}:destination")
            async def destination(interaction):
                if await allowed(interaction, bot):
                    await interaction.response.send_modal(DestinationModal(bot, row))
            button.callback = destination
            self.add_item(button)
            return
        plan = json.loads(row['plan'])
        edit = discord.ui.Button(label='Edit answer', custom_id=f"jf:{row['id']}:{row['version'][:16]}:edit")
        async def edit_callback(interaction):
            if await allowed(interaction, bot):
                await interaction.response.send_modal(AnswerModal(bot, row))
        edit.callback = edit_callback
        self.add_item(edit)
        final = plan['snapshot']['action'] == 'submit'
        can_transmit = bot.forms.can_transmit(plan['snapshot']['url'])
        approve_label = ('Approve & submit' if final else 'Approve & continue')
        if not can_transmit:
            approve_label = 'Transmission disabled'
        for action, label in [('approve', approve_label), ('skip','Skip')]:
            button = discord.ui.Button(label=label,
                style=discord.ButtonStyle.success if action == 'approve' else discord.ButtonStyle.danger,
                disabled=action == 'approve' and
                (bool(bot.forms.missing(plan)) or not can_transmit),
                custom_id=f"jf:{row['id']}:{row['version'][:16]}:{action}")
            async def callback(interaction, action=action):
                if not await allowed(interaction, bot):
                    return
                try:
                    bot.forms.decide(row['id'], row['version'], action, interaction.user.id, bot.owner_id)
                except ValueError as exc:
                    await interaction.response.send_message(str(exc), ephemeral=True)
                    return
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(view=self)
            button.callback = callback
            self.add_item(button)


async def send_cards(bot):
    states = ('review','needs_attention','uncertain','submitted','approved','skipped')
    rows = bot.forms.list(states, undelivered=True)
    if not rows:
        return
    channel = bot.get_channel(bot.channel_id) or await bot.fetch_channel(bot.channel_id)
    for row in rows:
        app = bot.store.get(row['id'])
        job = json.loads(app['job'])
        embed = discord.Embed(title=('Application: ' + job['title'] + ' | ' + job.get('company',''))[:256])
        kwargs = {'embed': embed}
        if row['state'] == 'review':
            plan = json.loads(row['plan'])
            snapshot = plan['snapshot']
            missing = bot.forms.missing(plan)
            prefilled = sum(1 for answer in plan['answers'].values()
                            if str(answer.get('source', '')).startswith('profile:'))
            can_transmit = bot.forms.can_transmit(snapshot['url'])
            policy = (
                'Approval authorizes sending the attached answers and selected resume files to this employer. '
                'A Continue step may create an applicant record before final submission. '
                'Optional unanswered checkboxes will be unchecked.'
                if can_transmit else
                'INSPECTION-ONLY: answers are resolved locally. No employer fields, files or '
                'button clicks can be transmitted; the approval button is disabled.'
            )
            embed.description = (
                f"Destination: {urlsplit(snapshot['url']).hostname}\n"
                f"Employer button: {snapshot['button']}\n"
                f"{policy}\n"
                f"Prefilled from your profile: {prefilled} of {len(snapshot['fields'])} fields. "
                f"Required answers still missing: {len(missing)}. Use Edit answer and the numbered packet."
            )
            packet = dict(plan)
            packet['numbered_fields'] = [dict(field, number=index + 1) for index, field in enumerate(snapshot['fields'])]
            packet['attachment_choices'] = ['approved_resume.pdf','approved_resume.docx']
            kwargs['files'] = [discord.File(io.BytesIO(json.dumps(packet, indent=2).encode()), filename='form-review.json')]
            kwargs['view'] = FormReview(bot, row)
            embed.set_footer(text='Form version ' + row['version'][:16] + ' | Resume ' + row['resume_digest'][:16])
        elif row['state'] == 'needs_attention':
            embed.description = row['error'] + '\nNo employer action was attempted by this inspection.'
            kwargs['view'] = FormReview(bot, row)
        elif row['state'] == 'uncertain':
            embed.description = row['error'] + '\nA transmission may have occurred. Automatic retries are stopped. Check the employer record before continuing.'
        elif row['state'] == 'submitted':
            embed.description = 'The employer displayed a submission confirmation. Receipt attached.'
            kwargs['files'] = [discord.File(io.BytesIO(row['receipt'].encode()), filename='submission-receipt.json')]
        else:
            embed.description = 'Approved step queued for execution.' if row['state'] == 'approved' else 'Application workflow skipped.'
        message = await channel.send(**kwargs)
        bot.forms.mark_card(row, message.id)

