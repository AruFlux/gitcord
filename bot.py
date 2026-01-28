import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import json
import sys
import asyncio
from typing import Optional
from github import Github, GithubException
from dotenv import load_dotenv
import re
import asyncpg
from datetime import datetime

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
DEFAULT_REPO = os.getenv("DEFAULT_REPO", "discord-projects")
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway provides this automatically

# Database connection pool
db_pool = None

# Initialize bot
PREFIX = "--"
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
tree = app_commands.CommandTree(bot)

# GitHub client
github_client = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None

# ========== DATABASE FUNCTIONS ==========

async def init_db():
    """Initialize database connection pool"""
    global db_pool
    if DATABASE_URL:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        await create_tables()
        print("Database connected")

async def create_tables():
    """Create database tables if they don't exist"""
    async with db_pool.acquire() as conn:
        # User settings table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                default_repo TEXT,
                preferred_prefix TEXT DEFAULT '--',
                auto_create_repo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Repository history table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS repository_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                repo_name TEXT,
                action TEXT,
                filename TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Command logs table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS command_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                command TEXT,
                arguments TEXT,
                success BOOLEAN,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

async def get_user_settings(user_id: int):
    """Get user settings from database"""
    if not db_pool:
        return None
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM user_settings WHERE user_id = $1',
            user_id
        )
        return dict(row) if row else None

async def update_user_settings(user_id: int, **kwargs):
    """Update user settings in database"""
    if not db_pool:
        return
    
    async with db_pool.acquire() as conn:
        settings = await get_user_settings(user_id)
        
        if settings:
            # Update existing
            set_clause = ', '.join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
            values = [user_id] + list(kwargs.values())
            query = f'''
                UPDATE user_settings 
                SET {set_clause}, updated_at = NOW()
                WHERE user_id = $1
            '''
            await conn.execute(query, *values)
        else:
            # Insert new
            columns = ['user_id'] + list(kwargs.keys())
            placeholders = ', '.join([f'${i+1}' for i in range(len(columns))])
            values = [user_id] + list(kwargs.values())
            
            query = f'''
                INSERT INTO user_settings ({', '.join(columns)})
                VALUES ({placeholders})
            '''
            await conn.execute(query, *values)

async def log_command(user_id: int, command: str, arguments: str, success: bool, error_message: str = None):
    """Log command usage to database"""
    if not db_pool:
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO command_logs (user_id, command, arguments, success, error_message)
            VALUES ($1, $2, $3, $4, $5)
        ''', user_id, command, arguments, success, error_message)

async def log_repo_action(user_id: int, repo_name: str, action: str, filename: str = None):
    """Log repository actions to database"""
    if not db_pool:
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO repository_history (user_id, repo_name, action, filename)
            VALUES ($1, $2, $3, $4)
        ''', user_id, repo_name, action, filename)

async def get_user_repo_history(user_id: int, limit: int = 10):
    """Get user's repository history"""
    if not db_pool:
        return []
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT repo_name, action, filename, created_at
            FROM repository_history
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        ''', user_id, limit)
        
        return [dict(row) for row in rows]

# ========== HELPER FUNCTIONS ==========

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be GitHub-safe"""
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    return filename

async def get_current_repo(user_id: int) -> str:
    """Get user's current repository from database or default"""
    if db_pool:
        settings = await get_user_settings(user_id)
        if settings and settings.get('default_repo'):
            return settings['default_repo']
    
    return DEFAULT_REPO

async def set_current_repo(user_id: int, repo_name: str):
    """Set user's current repository in database"""
    if db_pool:
        await update_user_settings(user_id, default_repo=repo_name)

async def get_user_prefix(user_id: int) -> str:
    """Get user's preferred prefix from database"""
    if db_pool:
        settings = await get_user_settings(user_id)
        if settings and settings.get('preferred_prefix'):
            return settings['preferred_prefix']
    
    return PREFIX

async def create_github_repo(repo_name: str, is_private: bool = True):
    """Create a new GitHub repository"""
    try:
        user = github_client.get_user()
        repo = user.create_repo(
            name=repo_name,
            private=is_private,
            auto_init=True,
            description=f"Created via Discord bot"
        )
        return repo, None
    except GithubException as e:
        return None, str(e)

async def file_exists(repo_name: str, filename: str) -> bool:
    """Check if a file exists in the repository"""
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        repo.get_contents(filename)
        return True
    except:
        return False

# ========== BOT EVENTS ==========

@bot.event
async def on_ready():
    """Bot startup handler"""
    await init_db()
    print(f'Bot {bot.user} is online')
    print(f'Prefix: {PREFIX}')
    
    try:
        await tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print(f"Error syncing commands: {e}")

# ========== PREFIX COMMANDS WITH DATABASE ==========

@bot.command(name='repo')
async def prefix_repo(ctx, repo_name: str = None, private: str = "true"):
    """Switch to a different repository"""
    args_str = f"repo_name={repo_name}, private={private}"
    
    try:
        if not repo_name:
            current = await get_current_repo(ctx.author.id)
            await ctx.send(f"Current repository: `{current}`")
            await log_command(ctx.author.id, 'repo', args_str, True)
            return
        
        repo_name = sanitize_filename(repo_name)
        is_private = private.lower() in ['true', 'yes', '1', 'private']
        
        try:
            repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
            await set_current_repo(ctx.author.id, repo_name)
            await log_repo_action(ctx.author.id, repo_name, 'switch')
            
            await ctx.send(f"Switched to repository: `{repo_name}`\nURL: {repo.html_url}")
            await log_command(ctx.author.id, 'repo', args_str, True)
            
        except GithubException:
            # Repository doesn't exist, create it
            repo, error = await create_github_repo(repo_name, is_private)
            if repo:
                await set_current_repo(ctx.author.id, repo_name)
                await log_repo_action(ctx.author.id, repo_name, 'create')
                await ctx.send(f"Created and switched to repository: `{repo_name}`\nURL: {repo.html_url}")
                await log_command(ctx.author.id, 'repo', args_str, True)
            else:
                await ctx.send(f"Error creating repository: {error}")
                await log_command(ctx.author.id, 'repo', args_str, False, error)
                
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'repo', args_str, False, str(e))

@bot.command(name='create')
async def prefix_create(ctx, filename: str, *, content: str):
    """Create a new file"""
    args_str = f"filename={filename}, content_length={len(content)}"
    
    try:
        repo_name = await get_current_repo(ctx.author.id)
        filename = sanitize_filename(filename)
        
        if await file_exists(repo_name, filename):
            await ctx.send(f"File `{filename}` already exists. Use edit command.")
            await log_command(ctx.author.id, 'create', args_str, False, "File exists")
            return
        
        api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        data = {
            "message": f"Create {filename}",
            "content": content.encode("utf-8").hex(),
            "branch": "main"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.put(api_url, headers=headers, json=data) as response:
                if response.status == 201:
                    repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
                    await log_repo_action(ctx.author.id, repo_name, 'create_file', filename)
                    await ctx.send(f"File `{filename}` created in `{repo_name}`\n{repo_url}")
                    await log_command(ctx.author.id, 'create', args_str, True)
                else:
                    error_data = await response.json()
                    error_msg = error_data.get('message', 'Unknown error')
                    await ctx.send(f"Error: {error_msg}")
                    await log_command(ctx.author.id, 'create', args_str, False, error_msg)
                    
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'create', args_str, False, str(e))

@bot.command(name='edit')
async def prefix_edit(ctx, filename: str, *, content: str):
    """Edit an existing file"""
    args_str = f"filename={filename}, content_length={len(content)}"
    
    try:
        repo_name = await get_current_repo(ctx.author.id)
        filename = sanitize_filename(filename)
        
        api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                if response.status != 200:
                    await ctx.send(f"File `{filename}` not found")
                    await log_command(ctx.author.id, 'edit', args_str, False, "File not found")
                    return
                
                file_data = await response.json()
                sha = file_data["sha"]
            
            data = {
                "message": f"Update {filename}",
                "content": content.encode("utf-8").hex(),
                "sha": sha,
                "branch": "main"
            }
            
            async with session.put(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
                    await log_repo_action(ctx.author.id, repo_name, 'edit_file', filename)
                    await ctx.send(f"File `{filename}` updated in `{repo_name}`\n{repo_url}")
                    await log_command(ctx.author.id, 'edit', args_str, True)
                else:
                    error_data = await response.json()
                    error_msg = error_data.get('message', 'Unknown error')
                    await ctx.send(f"Error: {error_msg}")
                    await log_command(ctx.author.id, 'edit', args_str, False, error_msg)
                    
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'edit', args_str, False, str(e))

@bot.command(name='view')
async def prefix_view(ctx, filename: str):
    """View a file's content"""
    args_str = f"filename={filename}"
    
    try:
        repo_name = await get_current_repo(ctx.author.id)
        filename = sanitize_filename(filename)
        
        api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                if response.status != 200:
                    await ctx.send(f"File `{filename}` not found")
                    await log_command(ctx.author.id, 'view', args_str, False, "File not found")
                    return
                
                file_data = await response.json()
                import base64
                content = base64.b64decode(file_data["content"]).decode("utf-8")
                file_url = file_data["html_url"]
                
                if len(content) > 1500:
                    content = content[:1500] + "..."
                
                await ctx.send(f"**{filename}**\n```\n{content}\n```\n{file_url}")
                await log_command(ctx.author.id, 'view', args_str, True)
                
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'view', args_str, False, str(e))

@bot.command(name='history')
async def prefix_history(ctx, limit: int = 10):
    """Show your repository history"""
    try:
        if not db_pool:
            await ctx.send("Database not available")
            return
        
        history = await get_user_repo_history(ctx.author.id, limit)
        
        if not history:
            await ctx.send("No history found")
            return
        
        lines = []
        for item in history:
            time = item['created_at'].strftime("%Y-%m-%d %H:%M")
            action = item['action']
            repo = item['repo_name']
            filename = f" - {item['filename']}" if item['filename'] else ""
            lines.append(f"[{time}] {action} - {repo}{filename}")
        
        await ctx.send(f"**Your Recent Activity (Last {len(history)}):**\n```\n" + "\n".join(lines) + "\n```")
        await log_command(ctx.author.id, 'history', f"limit={limit}", True)
        
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'history', f"limit={limit}", False, str(e))

@bot.command(name='prefix')
async def set_prefix(ctx, new_prefix: str = None):
    """Set your personal prefix"""
    try:
        if not new_prefix:
            current = await get_user_prefix(ctx.author.id)
            await ctx.send(f"Your prefix: `{current}`")
            await log_command(ctx.author.id, 'prefix', "check", True)
            return
        
        if len(new_prefix) > 3:
            await ctx.send("Prefix must be 3 characters or less")
            await log_command(ctx.author.id, 'prefix', new_prefix, False, "Too long")
            return
        
        if db_pool:
            await update_user_settings(ctx.author.id, preferred_prefix=new_prefix)
            await ctx.send(f"Your prefix updated to: `{new_prefix}`")
            await log_command(ctx.author.id, 'prefix', new_prefix, True)
        else:
            await ctx.send("Database not available")
            
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'prefix', new_prefix, False, str(e))

@bot.command(name='stats')
async def prefix_stats(ctx):
    """Show your bot usage statistics"""
    try:
        if not db_pool:
            await ctx.send("Database not available")
            return
        
        async with db_pool.acquire() as conn:
            # Total commands
            total_cmd = await conn.fetchval(
                'SELECT COUNT(*) FROM command_logs WHERE user_id = $1',
                ctx.author.id
            )
            
            # Successful commands
            success_cmd = await conn.fetchval(
                'SELECT COUNT(*) FROM command_logs WHERE user_id = $1 AND success = true',
                ctx.author.id
            )
            
            # Unique repos used
            unique_repos = await conn.fetchval(
                'SELECT COUNT(DISTINCT repo_name) FROM repository_history WHERE user_id = $1',
                ctx.author.id
            )
            
            # Last activity
            last_active = await conn.fetchval(
                'SELECT MAX(created_at) FROM command_logs WHERE user_id = $1',
                ctx.author.id
            )
        
        stats_text = f"""
**Your Statistics:**
- Total commands: {total_cmd}
- Successful: {success_cmd}
- Failed: {total_cmd - success_cmd}
- Unique repositories: {unique_repos}
- Last active: {last_active.strftime('%Y-%m-%d %H:%M') if last_active else 'Never'}
"""
        await ctx.send(stats_text)
        await log_command(ctx.author.id, 'stats', "", True)
        
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'stats', "", False, str(e))

@bot.command(name='restart')
@commands.is_owner()
async def restart_bot(ctx):
    """Restart the bot (owner only)"""
    await ctx.send("Restarting bot...")
    print("Bot restart initiated")
    os.execv(sys.executable, ['python'] + sys.argv)

@bot.command(name='help')
async def prefix_help(ctx):
    """Show all commands"""
    help_text = """
**Prefix Commands:**
`--repo [name] [private]` - Switch to/create repository
`--create [filename] [content]` - Create new file
`--edit [filename] [content]` - Edit existing file
`--view [filename]` - View file content
`--list` - List repository files
`--current` - Show current repository
`--delete [filename]` - Delete a file
`--history [limit]` - Show your activity history
`--stats` - Show your usage statistics
`--prefix [new_prefix]` - Change your prefix
`--restart` - Restart bot (owner only)
`--help` - Show this help

**Database Features:**
- Command logging
- User preferences storage
- Activity history
- Usage statistics
"""
    await ctx.send(help_text)
    await log_command(ctx.author.id, 'help', "", True)

# ========== SLASH COMMANDS ==========

@tree.command(name="repo", description="Switch to a GitHub repository")
@app_commands.describe(
    repo_name="Repository name",
    private="Make repository private"
)
async def slash_repo(interaction: discord.Interaction, repo_name: str, private: bool = True):
    await interaction.response.defer()
    ctx = await bot.get_context(interaction.message) if interaction.message else None
    if ctx:
        await prefix_repo(ctx, repo_name, str(private).lower())

# Add other slash commands similarly...

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Command not found. Use `{PREFIX}help` for available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument. Use `{PREFIX}help` for command usage.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("This command is for bot owners only.")
    else:
        print(f"Error: {error}")
        await ctx.send("An error occurred while executing the command.")

# ========== START BOT ==========

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GITHUB_TOKEN:
        print("Missing required environment variables")
    else:
        bot.run(DISCORD_TOKEN)
