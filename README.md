# Roster Bot

This bot keeps the uploaded `/clan recruit` workflow, but changes the final
step:

1. The command is run in the other Discord server where the roster role exists.
2. The person running it must be in the main server and must own, co-lead, or
   hold the registered role for the selected clan.
3. Every matching person who is already in the main server receives a private
   Discord message.
4. The person chooses **Accept invite** or **Decline**.
5. Only after accepting does the bot add the clan role and save them in
   `clan_members`.

People who are not in the main server are skipped. People already in another
registered clan are skipped. The bot never silently moves someone from one
clan to another.

## 1. Create the bot application

In the Discord Developer Portal:

1. Create an application and open its **Bot** page.
2. Copy/reset the bot token.
3. Enable **Server Members Intent** under Privileged Gateway Intents.
4. Invite the bot with the `bot` and `applications.commands` scopes.
5. Give it these permissions in the main server:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Roles

The bot's highest role must be above every clan role it needs to assign.

The bot must be in both the main server and every roster/sub-server where the
command will be used.

## 2. Install and configure

From this folder:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set:

- `DISCORD_TOKEN` to the bot token
- `MAIN_GUILD_ID` to the main server ID
- `SERVER_NAME` to the name users should see in messages

To copy IDs in Discord, enable Developer Mode, right-click the server or role,
and choose **Copy ID**.

## 3. Use an existing clan database

The bot uses SQLite. The uploaded cog expected a `db.py` file and a SQLite
database containing `clans`, `clan_members`, and `clan_server_config`. If you
already have that database, copy it to:

```text
data/roster.db
```

Or set `DATABASE_PATH` to its existing location. The bot creates the invite
tables automatically the first time it starts.

The `clans` table needs these values for each clan:

- `guild_id`: the main server ID
- `server_id`: the clan/server grouping used by the existing bot
- `name` and `clantag`
- `owner_id`
- `role_id`: the main-server clan role
- `channel_id`: optional clan team-chat channel

If your existing `db.py` or schema uses different column names, keep your
existing database and adjust the SQL in `db.py`/`cogs/recruit.py` to match
before starting the bot.

## 4. Start it

```bash
python bot.py
```

Then use:

```text
/clan recruit role:@YourRosterRole
```

If automatic clan detection finds more than one clan, specify:

```text
/clan recruit role:@YourRosterRole clan_name:YourClanName
```

## Important Discord behavior

- Users with DMs disabled for server members will show under **Failed**.
- The role is not assigned when the recruiter runs the command.
- Pending invitations survive a bot restart and their buttons continue working.
- Invites expire after `INVITE_EXPIRY_DAYS` days.
- The bot requires Server Members Intent to read role members and verify main
  server membership.