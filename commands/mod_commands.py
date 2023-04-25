from datetime import datetime, timedelta
from discord import app_commands, Interaction, Client, User
from discord.app_commands.errors import AppCommandError, CheckFailure
from controllers.good_morning_controller import (
    GoodMorningController,
    GOOD_MORNING_EXPLANATION,
)
from controllers.prediction_controller import PredictionController
from db import DB, RaffleType
from db.models import PredictionChoice, PredictionOutcome
from views.predictions.create_predictions_modal import CreatePredictionModal
from views.raffle.new_raffle_modal import NewRaffleModal
from views.rewards.add_reward_modal import AddRewardModal
from controllers.raffle_controller import RaffleController
from config import Config
import logging
import random
from threading import Thread
import requests

LOG = logging.getLogger(__name__)
JOEL_DISCORD_ID = 112386674155122688
HOOJ_DISCORD_ID = 82969926125490176
POINTS_AUDIT_CHANNEL = int(Config.CONFIG["Discord"]["PointsAuditChannel"])

AUTH_TOKEN = Config.CONFIG["Server"]["AuthToken"]
PUBLISH_URL = "http://localhost:3000/publish-vod"
PUBLISH_POLL_URL = "http://localhost:3000/publish-poll"
PUBLISH_TIMER_URL = "http://localhost:3000/publish-timer"


@app_commands.guild_only()
class ModCommands(app_commands.Group, name="mod"):
    def __init__(self, tree: app_commands.CommandTree, client: Client) -> None:
        super().__init__()
        self.tree = tree
        self.client = client

    @staticmethod
    def check_owner(interaction: Interaction) -> bool:
        return interaction.user.id == JOEL_DISCORD_ID

    @staticmethod
    def check_hooj(interaction: Interaction) -> bool:
        return interaction.user.id == HOOJ_DISCORD_ID

    async def on_error(self, interaction: Interaction, error: AppCommandError):
        if isinstance(error, CheckFailure):
            return await interaction.response.send_message(
                "Failed to perform command - please verify permissions.", ephemeral=True
            )
        logging.error(error)
        return await super().on_error(interaction, error)

    @app_commands.command(name="sync")
    @app_commands.checks.has_role("Mod")
    async def sync(self, interaction: Interaction) -> None:
        """Manually sync slash commands to guild"""

        guild = interaction.guild
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        await interaction.response.send_message("Commands synced", ephemeral=True)

    @app_commands.command(name="timer")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(time="Time in seconds")
    async def timer(self, interaction: Interaction, time: int) -> None:
        """Display a timer on stream of the given length"""
        Thread(target=publish_timer, args=(time,)).start()

        await interaction.response.send_message("Timer created!", ephemeral=True)

    @app_commands.command(name="poll")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(title="title")
    @app_commands.describe(option_one="option_one")
    @app_commands.describe(option_two="option_two")
    @app_commands.describe(option_three="option_three")
    @app_commands.describe(option_four="option_four")
    async def poll(self, interaction: Interaction, title: str, option_one: str, option_two: str, option_three: str="", option_four: str="") -> None:
        """Run the given poll, 2-4 options"""
        Thread(target=publish_poll, args=(title, option_one, option_two, option_three, option_four,)).start()

        await interaction.response.send_message("Poll created!", ephemeral=True)

    @app_commands.command(name="vod")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(username="username")
    @app_commands.describe(riotid="riotid")
    @app_commands.describe(rank="rank")
    async def vod(self, interaction: Interaction, username: str, riotid: str, rank: str) -> None:
        """Start a VOD review for the given username"""
        Thread(target=publish_update, args=(username, riotid, rank, False,)).start()

        await interaction.response.send_message("Username event sent!", ephemeral=True)

    @app_commands.command(name="complete")
    @app_commands.checks.has_role("Mod")
    async def complete(self, interaction: Interaction) -> None:
        """Start a VOD review for the given username"""
        Thread(target=publish_update, args=("", "", "", True,)).start()

        await interaction.response.send_message("VOD Complete Event sent!", ephemeral=True)

    @app_commands.command(name="gift")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(num_winners="num_winners")
    @app_commands.describe(oprah="Oprah")
    async def gift(self, interaction: Interaction, oprah: str, num_winners: int):
        Tier1 = int(Config.CONFIG["Discord"]["Tier1RoleID"])
        Tier2 = int(Config.CONFIG["Discord"]["Tier2RoleID"])
        Tier3 = int(Config.CONFIG["Discord"]["Tier3RoleID"])
        BotRole = int(Config.CONFIG["Discord"]["BotRoleID"])
        GiftedTier1 = int(Config.CONFIG["Discord"]["GiftedTier1RoleID"])
        GiftedTier3 = int(Config.CONFIG["Discord"]["GiftedTier3RoleID"])
        Mod = int(Config.CONFIG["Discord"]["ModRoleID"])

        await interaction.response.send_message("Choosing random gifted sub winners...")
        potential_winners = []
        for member in interaction.channel.members:
            can_win = True
            for role in member.roles:
                if role.id in [
                    Tier1,
                    Tier2,
                    Tier3,
                    BotRole,
                    GiftedTier1,
                    GiftedTier3,
                    Mod,
                ]:
                    can_win = False

            if can_win:
                potential_winners.append(member.mention)

        winners = random.choices(potential_winners, k=num_winners)
        for winner in winners:
            await interaction.channel.send(
                f"{oprah} has gifted {winner} a T1 Subscription!"
            )

    @app_commands.command(name="start")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(raffle_type="Raffle Type (default: normal)")
    async def start(
        self, interaction: Interaction, raffle_type: RaffleType = RaffleType.normal
    ):
        """Starts a new raffle"""

        if DB().has_ongoing_raffle(interaction.guild.id):
            await interaction.response.send_message(
                "There is already an ongoing raffle!"
            )
            return

        modal = NewRaffleModal(raffle_type=raffle_type)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="end")
    @app_commands.checks.has_role("Mod")
    async def end(
        self,
        interaction: Interaction,
        num_winners: int = 1,
    ) -> None:
        """Closes an existing raffle and pick the winner(s)"""

        if not DB().has_ongoing_raffle(interaction.guild.id):
            await interaction.response.send_message(
                "There is no ongoing raffle! You need to start a new one."
            )
            return

        raffle_message_id = DB().get_raffle_message_id(interaction.guild.id)
        if raffle_message_id is None:
            await interaction.response.send_message(
                "Oops! That raffle does not exist anymore."
            )
            return

        await RaffleController._end_raffle_impl(
            interaction, raffle_message_id, num_winners
        )
        DB().close_raffle(interaction.guild.id, end_time=datetime.now())

    @app_commands.command(name="add_reward")
    @app_commands.checks.has_role("Mod")
    async def add_reward(self, interaction: Interaction):
        """Creates new channel reward for redemption"""
        modal = AddRewardModal()
        await interaction.response.send_modal(modal)

    @app_commands.command(name="remove_reward")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(name="Name of reward to remove")
    async def remove_reward(self, interaction: Interaction, name: str):
        """Removes channel reward for redemption"""
        DB().remove_channel_reward(name)
        await interaction.response.send_message(
            f"Successfully removed {name}!", ephemeral=True
        )

    @app_commands.command(name="allow_redemptions")
    @app_commands.checks.has_role("Mod")
    async def allow_redemptions(self, interaction: Interaction):
        """Allow rewards to be redeemed"""
        DB().allow_redemptions()
        await interaction.response.send_message(
            "Redemptions are now enabled", ephemeral=True
        )

    @app_commands.command(name="pause_redemptions")
    @app_commands.checks.has_role("Mod")
    async def pause_redemptions(self, interaction: Interaction):
        """Pause rewards from being redeemed"""
        DB().pause_redemptions()
        await interaction.response.send_message(
            "Redemptions are now paused", ephemeral=True
        )

    @app_commands.command(name="check_redemption_status")
    @app_commands.checks.has_role("Mod")
    async def check_redemption_status(self, interaction: Interaction):
        """Check whether or not rewards are eligible to be redeemed"""
        status = DB().check_redemption_status()
        status_message = "allowed" if status else "paused"
        await interaction.response.send_message(
            f"Redemptions are currently {status_message}.", ephemeral=True
        )

    @app_commands.command(name="start_prediction")
    @app_commands.checks.has_role("Mod")
    async def start_prediction(self, interaction: Interaction):
        """Start new prediction"""
        if DB().has_ongoing_prediction(interaction.guild_id):
            return await interaction.response.send_message(
                "There is already an ongoing prediction!", ephemeral=True
            )
        await interaction.response.send_modal(CreatePredictionModal(self.client))

    @app_commands.command(name="refund_prediction")
    @app_commands.checks.has_role("Mod")
    async def refund_prediction(self, interaction: Interaction):
        """Refund ongoing prediction, giving users back the points they wagered"""
        await PredictionController.refund_prediction(interaction, self.client)

    @app_commands.command(name="payout_prediction")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(option="Option to payout")
    async def payout_prediction(
        self, interaction: Interaction, option: PredictionChoice
    ):
        """Payout predicton to option pink or blue"""
        await PredictionController.payout_prediction(option, interaction, self.client)

    @app_commands.command(name="redo_payout")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(option="Option to payout")
    async def redo_payout(self, interaction: Interaction, option: PredictionOutcome):
        """Redo the last prediction's payout"""
        await PredictionController.redo_payout(option, interaction, self.client)

    @app_commands.command(name="give_points")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(user="User ID to award points")
    @app_commands.describe(points="Number of points to award")
    @app_commands.describe(reason="Reason for awarding points")
    async def give_points(
        self, interaction: Interaction, user: User, points: int, reason: str = None
    ):
        """Manually give points to user"""
        audit_output = f"{interaction.user.mention} gave {user.mention} {points}pts"
        if reason is not None:
            audit_output += f': "{reason}"'

        if reason is None and not self.check_hooj(interaction):
            return await interaction.response.send_message(
                "Please provide a reason for awarding points", ephemeral=True
            )

        logging.info(POINTS_AUDIT_CHANNEL)
        await self.client.get_channel(POINTS_AUDIT_CHANNEL).send(audit_output)
        success, _ = DB().deposit_points(user.id, points)
        if not success:
            return await interaction.response.send_message(
                "Failed to award points - please try again.", ephemeral=True
            )
        await interaction.response.send_message(
            "Successfully awarded points!", ephemeral=True
        )

    @app_commands.command(name="good_morning_count")
    @app_commands.checks.has_role("Mod")
    async def good_morning_count(self, interaction: Interaction):
        """Check how many users have said good morning today!"""
        count = DB().get_today_morning_count()
        await interaction.response.send_message(
            f"{count} users have said good morning today! {GOOD_MORNING_EXPLANATION}"
        )

    @app_commands.command(name="good_morning_reward")
    @app_commands.checks.has_role("Mod")
    async def good_morning_reward(self, interaction: Interaction):
        """Reward users who have met the 'Good Morning' threshold"""
        await GoodMorningController.reward_users(interaction)

    @app_commands.command(name="good_morning_reset")
    @app_commands.checks.has_role("Mod")
    async def good_morning_reset(self, interaction: Interaction):
        """Reset all weekly good morning points to 0"""
        await GoodMorningController.reset_all_morning_points(interaction)

    @app_commands.command(name="good_morning_increment")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(points="Number of points to award")
    async def good_morning_increment(self, interaction: Interaction, points: int):
        """Give all users a fixed number of good morning points"""
        await GoodMorningController.good_morning_increment(points, interaction)

    @app_commands.command(name="remove_raffle_winner")
    @app_commands.checks.has_role("Mod")
    @app_commands.describe(user="User ID to remove win from")
    async def remove_raffle_winner(self, interaction: Interaction, user: User):
        one_week_ago = datetime.now().date() - timedelta(days=6)

        if not DB().remove_raffle_winner(interaction.guild_id, user.id, one_week_ago):
            await interaction.response.send_message(
                "This user has not recently won a raffle!"
            )
            return

        await interaction.response.send_message("Winner removed!")


def publish_update(username, riotid, rank, complete):
    payload = {
        "username": username,
        "riotid": riotid,
        "rank": rank,
        "complete": complete,
    }

    response = requests.post(
        url=PUBLISH_URL, json=payload, headers={"x-access-token": AUTH_TOKEN}
    )

    if response.status_code != 200:
        LOG.error(f"Failed to publish updated prediction summary: {response.text}")

def publish_poll(title, option_one, option_two, option_three, option_four):
    payload = {
        "title": title,
        "options": [option_one, option_two, option_three, option_four],
    }

    response = requests.post(
        url=PUBLISH_POLL_URL, json=payload, headers={"x-access-token": AUTH_TOKEN}
    )

    if response.status_code != 200:
        LOG.error(f"Failed to publish poll: {response.text}")

def publish_timer(time):
    payload = {
        "time": time,
    }

    response = requests.post(
        url=PUBLISH_TIMER_URL, json=payload, headers={"x-access-token": AUTH_TOKEN}
    )

    if response.status_code != 200:
        LOG.error(f"Failed to publish timer: {response.text}")