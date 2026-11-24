"""在迁移测试中按旧版表结构建立场景，不调用新版创建接口。"""

from uuid import uuid4

from host.storage import campaign_info, open_campaign


def seed_legacy_scene(directory, name, members):
    channel_id = str(uuid4())
    with open_campaign(directory) as connection, connection:
        campaign = campaign_info(connection)
        connection.execute(
            """INSERT INTO channels(id, session_id, name, kind, status, created_at)
               VALUES (?, ?, ?, 'scene', 'active', '2026-11-13')""",
            (channel_id, campaign["session_id"], name),
        )
        connection.executemany(
            "INSERT INTO channel_memberships(member_id, channel_id, start_sequence) VALUES (?, ?, 1)",
            [(member, channel_id) for member in members],
        )
    return channel_id
