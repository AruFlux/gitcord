import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import json
import sys
import asyncio
import base64
from typing import Optional, Dict, Any
from github import Github, GithubException
from dotenv import load_dotenv
import re
from datetime import datetime, timedelta
import aiosqlite
import psycopg2
from contextlib import closing
import hashlib

# ========== DISABLE VOICE SUPPORT ==========
import discord.voice_client
discord.voice_client.VoiceClient = None

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
DEFAULT_REPO = os.getenv("DEFAULT_REPO", "discord-projects")
DATABASE_URL = os.getenv("DATABASE_URL")

# Validate required environment variables
if not DISCORD_TOKEN:
    print("❌ ERROR: DISCORD_TOKEN environment variable not set")
    sys.exit(1)
if not GITHUB_TOKEN:
    print("❌ ERROR: GITHUB_TOKEN environment variable not set")
    sys.exit(1)
if not GITHUB_USERNAME:
    print("❌ ERROR: GITHUB_USERNAME environment variable not set")
    sys.exit(1)

# Initialize bot
PREFIX = "--"
intents = discord.Intents.default()
intents.message_content = True

class GitCordBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=None
        )
        self.db = None
        self.logger = None
        self.github_client = None
        self.github_username = GITHUB_USERNAME
    
    async def setup_hook(self):
        await self.add_cog(GitHubCommands(self))
        await self.add_cog(AdminCommands(self))
        await self.tree.sync()
        
    async def on_ready(self):
        print(f'✅ Bot {self.user} is online')
        print(f'📝 Prefix: {PREFIX}')
        print(f'👤 GitHub User: {self.github_username}')
        
        # Initialize systems
        success = await self.initialize_systems()
        if not success:
            print('❌ Some systems failed to initialize')
        
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="GitHub repositories"
        ))

    async def initialize_systems(self):
        """Initialize all systems, return True if all successful"""
        all_success = True
        
        # Database
        self.db = DatabaseManager()
        if await self.db.initialize():
            print('✅ Database connected')
        else:
            print('⚠️  Database not available')
            all_success = False
        
        # Logger
        self.logger = FileLogger()
        print('✅ Logger initialized')
        
        # GitHub client
        try:
            self.github_client = Github(GITHUB_TOKEN)
            # Verify token and get actual username
            user = self.github_client.get_user()
            actual_username = user.login
            print(f'✅ GitHub authenticated as {actual_username}')
            
            # Update username if different from env
            if actual_username.lower() != self.github_username.lower():
                print(f'⚠️  Username mismatch: ENV={self.github_username}, TOKEN={actual_username}')
                print(f'   Using: {actual_username}')
                self.github_username = actual_username
            
            # Check rate limits
            rate_limit = self.github_client.get_rate_limit().core
            print(f'📊 GitHub Rate Limit: {rate_limit.remaining}/{rate_limit.limit}')
            
        except Exception as e:
            print(f'❌ GitHub authentication failed: {e}')
            all_success = False
        
        return all_success

bot = GitCordBot()

# ========== DATABASE MANAGER ==========

class DatabaseManager:
    def __init__(self):
        self.db_type = None
        self.sqlite_conn = None
        self.pg_conn = None
        
    async def initialize(self):
        if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
            try:
                self.pg_conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
                self.db_type = "postgresql"
                await self._create_postgres_tables()
                print('✅ PostgreSQL connected')
                return True
            except Exception as e:
                print(f'❌ PostgreSQL failed: {e}')
        
        # Fallback to SQLite
        try:
            self.sqlite_conn = await aiosqlite.connect("gitcord.db")
            self.db_type = "sqlite"
            await self._create_sqlite_tables()
            print('✅ SQLite connected')
            return True
        except Exception as e:
            print(f'❌ SQLite failed: {e}')
            return False
    
    async def _create_postgres_tables(self):
        with closing(self.pg_conn.cursor()) as cur:
            tables = [
                '''CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    default_repo TEXT,
                    preferred_prefix TEXT DEFAULT '--',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )''',
                
                '''CREATE TABLE IF NOT EXISTS command_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    command TEXT,
                    arguments TEXT,
                    success BOOLEAN,
                    error_message TEXT,
                    execution_time FLOAT,
                    created_at TIMESTAMP DEFAULT NOW()
                )''',
                
                '''CREATE TABLE IF NOT EXISTS file_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    repo_name TEXT,
                    filename TEXT,
                    action TEXT,
                    content_hash TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )'''
            ]
            
            for table_sql in tables:
                cur.execute(table_sql)
            
            self.pg_conn.commit()
    
    async def _create_sqlite_tables(self):
        await self.sqlite_conn.execute("PRAGMA foreign_keys = ON")
        
        tables = [
            '''CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_repo TEXT,
                preferred_prefix TEXT DEFAULT '--',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            
            '''CREATE TABLE IF NOT EXISTS command_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                command TEXT,
                arguments TEXT,
                success BOOLEAN,
                error_message TEXT,
                execution_time REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            
            '''CREATE TABLE IF NOT EXISTS file_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                repo_name TEXT,
                filename TEXT,
                action TEXT,
                content_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        ]
        
        for table_sql in tables:
            await self.sqlite_conn.execute(table_sql)
        
        await self.sqlite_conn.commit()
    
    async def execute(self, query: str, *args):
        if self.db_type == "postgresql":
            with closing(self.pg_conn.cursor()) as cur:
                cur.execute(query, args)
                self.pg_conn.commit()
        elif self.db_type == "sqlite":
            await self.sqlite_conn.execute(query, args)
            await self.sqlite_conn.commit()
    
    async def fetchrow(self, query: str, *args):
        if self.db_type == "postgresql":
            with closing(self.pg_conn.cursor()) as cur:
                cur.execute(query, args)
                row = cur.fetchone()
                return dict(zip([desc[0] for desc in cur.description], row)) if row else None
        elif self.db_type == "sqlite":
            cursor = await self.sqlite_conn.execute(query, args)
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row) if row else None
    
    async def fetch(self, query: str, *args):
        if self.db_type == "postgresql":
            with closing(self.pg_conn.cursor()) as cur:
                cur.execute(query, args)
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in rows]
        elif self.db_type == "sqlite":
            cursor = await self.sqlite_conn.execute(query, args)
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows] if rows else []
    
    async def close(self):
        if self.db_type == "sqlite" and self.sqlite_conn:
            await self.sqlite_conn.close()
        elif self.db_type == "postgresql" and self.pg_conn:
            self.pg_conn.close()

# ========== HELPER FUNCTIONS ==========

def sanitize_filename(filename: str) -> str:
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    return filename

def encode_content(content: str) -> str:
    return base64.b64encode(content.encode('utf-8')).decode('utf-8')

async def get_user_settings(user_id: int, db: DatabaseManager) -> Dict[str, Any]:
    row = await db.fetchrow('SELECT * FROM user_settings WHERE user_id = $1', user_id)
    return row or {}

async def update_user_settings(user_id: int, db: DatabaseManager, **kwargs):
    settings = await get_user_settings(user_id, db)
    
    if settings:
        set_clause = ', '.join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
        values = [user_id] + list(kwargs.values())
        await db.execute(
            f'UPDATE user_settings SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = $1',
            *values
        )
    else:
        columns = ['user_id'] + list(kwargs.keys())
        placeholders = ', '.join([f'${i+1}' for i in range(len(columns))])
        values = [user_id] + list(kwargs.values())
        await db.execute(
            f'INSERT INTO user_settings ({", ".join(columns)}) VALUES ({placeholders})',
            *values
        )

async def get_current_repo(user_id: int, db: DatabaseManager) -> str:
    settings = await get_user_settings(user_id, db)
    return settings.get('default_repo', DEFAULT_REPO)

async def set_current_repo(user_id: int, repo_name: str, db: DatabaseManager):
    await update_user_settings(user_id, db, default_repo=repo_name)

async def log_command(user_id: int, command: str, arguments: str, success: bool, 
                     db: DatabaseManager, error_message: str = None, execution_time: float = 0.0):
    await db.execute(
        'INSERT INTO command_logs (user_id, command, arguments, success, error_message, execution_time) VALUES ($1, $2, $3, $4, $5, $6)',
        user_id, command, arguments, success, error_message, execution_time
    )

async def github_api_request(method: str, endpoint: str, data: dict = None):
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitCord-Bot"
    }
    
    url = f"https://api.github.com{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, headers=headers, json=data, timeout=30) as response:
                if response.status == 204:
                    return response.status, {}
                response_data = await response.json() if response.content_length else {}
                return response.status, response_data
        except asyncio.TimeoutError:
            return 408, {"message": "Request timeout"}
        except Exception as e:
            return 500, {"message": str(e)}

# ========== COMMAND COGS ==========

class GitHubCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='debug')
    async def cmd_debug(self, ctx):
        """Debug GitHub connection"""
        try:
            embed = discord.Embed(
                title="🔧 Debug Information",
                color=discord.Color.blue()
            )
            
            # 1. Basic info
            embed.add_field(
                name="Bot Info",
                value=f"Username: {self.bot.github_username}\n"
                      f"Prefix: {PREFIX}\n"
                      f"GitHub Client: {'✅ Ready' if self.bot.github_client else '❌ Not ready'}",
                inline=False
            )
            
            # 2. GitHub authentication
            if self.bot.github_client:
                try:
                    user = self.bot.github_client.get_user()
                    embed.add_field(
                        name="GitHub Auth",
                        value=f"Authenticated as: {user.login}\n"
                              f"User ID: {user.id}\n"
                              f"Profile URL: {user.html_url}",
                        inline=False
                    )
                    
                    # 3. List some repos
                    try:
                        repos = list(user.get_repos()[:5])
                        repo_list = "\n".join([f"• {repo.name} ({'🔒' if repo.private else '🌐'})" for repo in repos])
                        embed.add_field(
                            name="Your Repositories (first 5)",
                            value=repo_list or "No repositories found",
                            inline=False
                        )
                    except Exception as e:
                        embed.add_field(
                            name="Repository Access",
                            value=f"Error: {str(e)}",
                            inline=False
                        )
                        
                except Exception as e:
                    embed.add_field(
                        name="GitHub Error",
                        value=f"Authentication failed: {str(e)}",
                        inline=False
                    )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Debug error: {str(e)}")
    
    @commands.command(name='repo')
    async def cmd_repo(self, ctx, repo_name: str = None, private: str = "true"):
        """Switch to or create a repository"""
        if not repo_name:
            current = await get_current_repo(ctx.author.id, self.bot.db)
            await ctx.send(f"Current repository: `{current}`")
            return
        
        if not self.bot.github_client:
            await ctx.send("GitHub client not ready. Please wait for bot initialization.")
            return
        
        repo_name = sanitize_filename(repo_name)
        is_private = private.lower() in ['true', 'yes', '1', 'private']
        
        try:
            # Try to access the repository
            repo_full_name = f"{self.bot.github_username}/{repo_name}"
            repo = self.bot.github_client.get_repo(repo_full_name)
            
            # Success - repository exists
            await set_current_repo(ctx.author.id, repo_full_name, self.bot.db)
            
            embed = discord.Embed(
                title="✅ Repository Switched",
                description=f"Now working in **{repo.name}**",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=repo.html_url, inline=False)
            embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
            embed.add_field(name="Owner", value=repo.owner.login, inline=True)
            
            await ctx.send(embed=embed)
            
        except GithubException as e:
            # Repository doesn't exist - offer to create
            error_msg = str(e)
            if "404" in error_msg or "Not Found" in error_msg:
                embed = discord.Embed(
                    title="Repository Not Found",
                    description=f"Repository `{repo_name}` doesn't exist under `{self.bot.github_username}`.",
                    color=discord.Color.orange()
                )
                embed.add_field(
                    name="Create it?",
                    value="Type `create` to create this repository or `cancel` to abort.",
                    inline=False
                )
                
                await ctx.send(embed=embed)
                
                # Wait for user response
                def check(m):
                    return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['create', 'cancel']
                
                try:
                    msg = await self.bot.wait_for('message', timeout=30.0, check=check)
                    
                    if msg.content.lower() == 'create':
                        # Create the repository
                        user = self.bot.github_client.get_user()
                        repo = user.create_repo(
                            name=repo_name,
                            private=is_private,
                            auto_init=False,
                            description="Created via GitCord Bot"
                        )
                        
                        repo_full_name = f"{self.bot.github_username}/{repo_name}"
                        await set_current_repo(ctx.author.id, repo_full_name, self.bot.db)
                        
                        embed = discord.Embed(
                            title="✅ Repository Created",
                            description=f"Created and switched to **{repo.name}**",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="URL", value=repo.html_url, inline=False)
                        embed.add_field(name="Visibility", value="Private" if is_private else "Public", inline=True)
                        
                        await ctx.send(embed=embed)
                    else:
                        await ctx.send("Repository creation cancelled.")
                        
                except asyncio.TimeoutError:
                    await ctx.send("Repository creation timed out.")
            else:
                await ctx.send(f"GitHub Error: {error_msg}")
                
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")
    
    @commands.command(name='create')
    async def cmd_create(self, ctx, filename: str, *, content: str):
        """Create a new file"""
        # Get current repository
        current_repo = await get_current_repo(ctx.author.id, self.bot.db)
        if not current_repo or current_repo == DEFAULT_REPO:
            await ctx.send("Please select a repository first using `--repo <name>`")
            return
        
        filename = sanitize_filename(filename)
        
        if len(content) > 10000:
            await ctx.send("File too large (max 10KB)")
            return
        
        try:
            # Parse repo owner and name from current_repo
            if '/' in current_repo:
                owner, repo_name = current_repo.split('/', 1)
            else:
                owner = self.bot.github_username
                repo_name = current_repo
            
            # Use GitHub API directly
            status, response = await github_api_request(
                "PUT",
                f"/repos/{owner}/{repo_name}/contents/{filename}",
                {
                    "message": f"Create {filename} via GitCord",
                    "content": encode_content(content),
                    "branch": "main"
                }
            )
            
            if status == 201:
                embed = discord.Embed(
                    title="✅ File Created",
                    description=f"Created `{filename}` in `{current_repo}`",
                    color=discord.Color.green()
                )
                embed.add_field(name="Size", value=f"{len(content)} characters", inline=True)
                
                if len(content) <= 500:
                    preview = content[:200] + "..." if len(content) > 200 else content
                    embed.add_field(name="Preview", value=f"```\n{preview}\n```", inline=False)
                
                await ctx.send(embed=embed)
            else:
                error = response.get('message', f'Unknown error (status: {status})')
                await ctx.send(f"Error: {error}")
                
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")
    
    @commands.command(name='list')
    async def cmd_list(self, ctx):
        """List files in current repository"""
        current_repo = await get_current_repo(ctx.author.id, self.bot.db)
        if not current_repo or current_repo == DEFAULT_REPO:
            await ctx.send("Please select a repository first using `--repo <name>`")
            return
        
        try:
            # Parse repo owner and name
            if '/' in current_repo:
                owner, repo_name = current_repo.split('/', 1)
            else:
                owner = self.bot.github_username
                repo_name = current_repo
            
            # Use GitHub API
            status, response = await github_api_request(
                "GET",
                f"/repos/{owner}/{repo_name}/contents"
            )
            
            if status == 200:
                files = []
                directories = []
                
                for item in response:
                    if item.get('type') == 'file':
                        files.append(item.get('name', 'unknown'))
                    elif item.get('type') == 'dir':
                        directories.append(item.get('name', 'unknown'))
                
                embed = discord.Embed(
                    title=f"📁 Contents of {repo_name}",
                    color=discord.Color.purple()
                )
                
                if files:
                    file_list = "\n".join([f"• {f}" for f in files[:15]])
                    if len(files) > 15:
                        file_list += f"\n... and {len(files) - 15} more"
                    embed.add_field(name=f"Files ({len(files)})", value=file_list, inline=False)
                
                if directories:
                    dir_list = "\n".join([f"• {d}" for d in directories[:10]])
                    if len(directories) > 10:
                        dir_list += f"\n... and {len(directories) - 10} more"
                    embed.add_field(name=f"Directories ({len(directories)})", value=dir_list, inline=False)
                
                if not files and not directories:
                    embed.description = "Repository is empty"
                
                await ctx.send(embed=embed)
            else:
                error = response.get('message', f'Repository not found (status: {status})')
                await ctx.send(f"Error: {error}")
                
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")
    
    @commands.command(name='view')
    async def cmd_view(self, ctx, filename: str):
        """View a file"""
        current_repo = await get_current_repo(ctx.author.id, self.bot.db)
        if not current_repo or current_repo == DEFAULT_REPO:
            await ctx.send("Please select a repository first using `--repo <name>`")
            return
        
        filename = sanitize_filename(filename)
        
        try:
            # Parse repo owner and name
            if '/' in current_repo:
                owner, repo_name = current_repo.split('/', 1)
            else:
                owner = self.bot.github_username
                repo_name = current_repo
            
            # Use GitHub API
            status, response = await github_api_request(
                "GET",
                f"/repos/{owner}/{repo_name}/contents/{filename}"
            )
            
            if status == 200:
                content = base64.b64decode(response['content']).decode('utf-8')
                
                if len(content) > 1500:
                    content = content[:1500] + "\n... (truncated)"
                
                # Syntax highlighting
                ext = filename.split('.')[-1].lower() if '.' in filename else 'txt'
                languages = {
                    'py': 'python', 'js': 'javascript', 'ts': 'typescript',
                    'html': 'html', 'css': 'css', 'json': 'json',
                    'md': 'markdown', 'txt': 'text'
                }
                lang = languages.get(ext, 'text')
                
                embed = discord.Embed(
                    title=f"📄 {filename}",
                    description=f"From `{repo_name}`",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Content", value=f"```{lang}\n{content}\n```", inline=False)
                
                await ctx.send(embed=embed)
            else:
                error = response.get('message', 'File not found')
                await ctx.send(f"Error: {error}")
                
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")
    
    @commands.command(name='current')
    async def cmd_current(self, ctx):
        """Show current repository"""
        current_repo = await get_current_repo(ctx.author.id, self.bot.db)
        await ctx.send(f"Current repository: `{current_repo}`")
    
    @commands.command(name='help')
    async def cmd_help(self, ctx):
        """Show help"""
        embed = discord.Embed(
            title="GitCord Help",
            description="GitHub management bot for Discord",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Basic Commands",
            value=f"`{PREFIX}repo [name]` - Switch to repository\n"
                  f"`{PREFIX}create [filename] [content]` - Create file\n"
                  f"`{PREFIX}list` - List repository files\n"
                  f"`{PREFIX}view [filename]` - View file\n"
                  f"`{PREFIX}current` - Show current repo\n"
                  f"`{PREFIX}debug` - Debug information\n"
                  f"`{PREFIX}help` - This help",
            inline=False
        )
        
        embed.add_field(
            name="Usage",
            value=f"1. First, use `{PREFIX}repo your-repo-name`\n"
                  f"2. Then create files with `{PREFIX}create`\n"
                  f"3. Use `{PREFIX}debug` if you have issues",
            inline=False
        )
        
        await ctx.send(embed=embed)

    # ========== SLASH COMMANDS ==========
    
    @app_commands.command(name="repo", description="Switch to a repository")
    @app_commands.describe(
        repo_name="Repository name",
        private="Make private (true/false)"
    )
    async def slash_repo(self, interaction: discord.Interaction, repo_name: str, private: bool = True):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_repo(ctx, repo_name, "true" if private else "false")
    
    @app_commands.command(name="create", description="Create a new file")
    @app_commands.describe(
        filename="File name",
        content="File content"
    )
    async def slash_create(self, interaction: discord.Interaction, filename: str, content: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_create(ctx, filename, content=content)
    
    @app_commands.command(name="debug", description="Debug information")
    async def slash_debug(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_debug(ctx)
    
    @app_commands.command(name="list", description="List repository files")
    async def slash_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_list(ctx)

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='restart')
    @commands.is_owner()
    async def cmd_restart(self, ctx):
        """Restart the bot"""
        await ctx.send("Restarting bot...")
        print("Bot restart initiated")
        os.execv(sys.executable, ['python'] + sys.argv)

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Command not found. Use `{PREFIX}help` for available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument. Use `{PREFIX}help {ctx.command.name}` for usage.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("This command is for bot owners only.")
    else:
        print(f"Command Error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

# ========== START BOT ==========

if __name__ == "__main__":
    print("=" * 50)
    print("Starting GitCord Bot...")
    print("=" * 50)
    
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Bot shutdown complete")
