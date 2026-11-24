from host.attributes import player_attributes
from host.auth import AuthService, DEFAULT_PASSWORD
from host.management import add_member
from host.storage import campaign_info, open_campaign


ATTRIBUTE_COMMAND = ".st 力量50str50敏捷65意志55体质60外貌75教育75体型50智力70侦查65诗学11"


def add_ready_player(directory, name):
    member_id = add_member(directory, name)
    with open_campaign(directory) as connection:
        campaign_name = campaign_info(connection)["name"]
    token = AuthService(directory).login(campaign_name, name, DEFAULT_PASSWORD)["token"]
    player_attributes(directory, token, text=ATTRIBUTE_COMMAND)
    return member_id
