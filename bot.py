import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import json
import sys
import asyncio
import base64
from typing import Optional, Dict, Any, List
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
    
    async def setup_hook(self):
        await self.add_cog(GitHubCommands(self))
        await self.add_cog(AdminCommands(self))
        await self.tree.sync()
        
    async def on_ready(self):
        print(f'Bot {self.user} is online')
        print(f'Prefix: {PREFIX}')
        print(f'GitHub User: {GITHUB_USERNAME}')
        
        await self.initialize_systems()
        
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="GitHub repositories"
        ))

    async def initialize_systems(self):
        self.db = DatabaseManager()
        if await self.db.initialize():
            print('Database connected')
        else:
            print('Database not available')
        
        self.logger = FileLogger()
        
        if GITHUB_TOKEN:
            self.github_client = Github(GITHUB_TOKEN)
            try:
                user = self.github_client.get_user()
                print(f'GitHub authenticated as {user.login}')
            except Exception as e:
                print(f'GitHub authentication failed: {e}')

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
                print('Connected to PostgreSQL database')
                return True
            except Exception as e:
                print(f'PostgreSQL failed: {e}. Using SQLite fallback...')
        
        try:
            self.sqlite_conn = await aiosqlite.connect("gitcord.db")
            self.db_type = "sqlite"
            await self._create_sqlite_tables()
            print('Connected to SQLite database')
            return True
        except Exception as e:
            print(f'SQLite failed: {e}')
            return False
    
    async def _create_postgres_tables(self):
        with closing(self.pg_conn.cursor()) as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    default_repo TEXT,
                    preferred_prefix TEXT DEFAULT '--',
                    auto_create_repo BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS command_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    command TEXT,
                    arguments TEXT,
                    success BOOLEAN,
                    error_message TEXT,
                    execution_time FLOAT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS file_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    repo_name TEXT,
                    filename TEXT,
                    action TEXT,
                    content_hash TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id BIGINT PRIMARY KEY,
                    total_commands INTEGER DEFAULT 0,
                    successful_commands INTEGER DEFAULT 0,
                    files_created INTEGER DEFAULT 0,
                    files_edited INTEGER DEFAULT 0,
                    repos_created INTEGER DEFAULT 0,
                    last_active TIMESTAMP
                )
            ''')
            
            cur.execute('''
                CREATE TABLE IF NOT EXISTS file_templates (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    template_name TEXT,
                    filename_pattern TEXT,
                    content_template TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id, template_name)
                )
            ''')
            
            self.pg_conn.commit()
    
    async def _create_sqlite_tables(self):
        await self.sqlite_conn.execute("PRAGMA foreign_keys = ON")
        
        tables = [
            '''CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_repo TEXT,
                preferred_prefix TEXT DEFAULT '--',
                auto_create_repo BOOLEAN DEFAULT 1,
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
            )''',
            
            '''CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                total_commands INTEGER DEFAULT 0,
                successful_commands INTEGER DEFAULT 0,
                files_created INTEGER DEFAULT 0,
                files_edited INTEGER DEFAULT 0,
                repos_created INTEGER DEFAULT 0,
                last_active TIMESTAMP
            )''',
            
            '''CREATE TABLE IF NOT EXISTS file_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                template_name TEXT,
                filename_pattern TEXT,
                content_template TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, template_name)
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
    
    async def fetchval(self, query: str, *args):
        row = await self.fetchrow(query, *args)
        return list(row.values())[0] if row else None
    
    async def close(self):
        if self.db_type == "sqlite" and self.sqlite_conn:
            await self.sqlite_conn.close()
        elif self.db_type == "postgresql" and self.pg_conn:
            self.pg_conn.close()

# ========== FILE LOGGER SYSTEM ==========

class FileLogger:
    def __init__(self):
        self.log_dir = "logs"
        os.makedirs(self.log_dir, exist_ok=True)
    
    def log_failure(self, user_id: int, operation: str, filename: str, error: str, details: Dict = None):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "operation": operation,
            "filename": filename,
            "error": error,
            "details": details or {}
        }
        
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(self.log_dir, f"failures_{date_str}.json")
        
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    logs = json.load(f)
            else:
                logs = []
            
            logs.append(log_entry)
            
            with open(log_file, 'w') as f:
                json.dump(logs, f, indent=2)
            
            error_file = os.path.join(self.log_dir, "errors.log")
            with open(error_file, 'a') as f:
                f.write(f"[{log_entry['timestamp']}] USER:{user_id} OP:{operation} FILE:{filename} ERROR:{error}\n")
                
        except Exception as e:
            print(f"Failed to write log: {e}")
    
    def get_recent_failures(self, hours: int = 24):
        failures = []
        now = datetime.now()
        
        for i in range(hours // 24 + 1):
            date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            date_str = (date - timedelta(days=i)).strftime("%Y-%m-%d")
            log_file = os.path.join(self.log_dir, f"failures_{date_str}.json")
            
            if os.path.exists(log_file):
                with open(log_file, 'r') as f:
                    daily_failures = json.load(f)
                    failures.extend(daily_failures)
        
        cutoff = now - timedelta(hours=hours)
        recent = [f for f in failures if datetime.fromisoformat(f['timestamp']) > cutoff]
        
        return recent
    
    def log_success(self, user_id: int, operation: str, filename: str, details: Dict = None):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "operation": operation,
            "filename": filename,
            "success": True,
            "details": details or {}
        }
        
        date_str = datetime.now().strftime("%Y-%m-%d")
        success_file = os.path.join(self.log_dir, f"success_{date_str}.json")
        
        try:
            if os.path.exists(success_file):
                with open(success_file, 'r') as f:
                    logs = json.load(f)
            else:
                logs = []
            
            logs.append(log_entry)
            
            with open(success_file, 'w') as f:
                json.dump(logs, f, indent=2)
                
        except Exception as e:
            print(f"Failed to write success log: {e}")

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
    
    await db.execute('''
        INSERT INTO user_stats (user_id, total_commands, successful_commands, last_active)
        VALUES ($1, 1, $2, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id) DO UPDATE SET
            total_commands = user_stats.total_commands + 1,
            successful_commands = user_stats.successful_commands + $2,
            last_active = CURRENT_TIMESTAMP
    ''', user_id, 1 if success else 0)

async def log_file_history(user_id: int, repo_name: str, filename: str, action: str, 
                          db: DatabaseManager, content: str = None):
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:32] if content else None
    await db.execute(
        'INSERT INTO file_history (user_id, repo_name, filename, action, content_hash) VALUES ($1, $2, $3, $4, $5)',
        user_id, repo_name, filename, action, content_hash
    )
    
    if action == 'create':
        await db.execute('''
            INSERT INTO user_stats (user_id, files_created, last_active)
            VALUES ($1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                files_created = user_stats.files_created + 1,
                last_active = CURRENT_TIMESTAMP
        ''', user_id)
    elif action == 'edit':
        await db.execute('''
            INSERT INTO user_stats (user_id, files_edited, last_active)
            VALUES ($1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                files_edited = user_stats.files_edited + 1,
                last_active = CURRENT_TIMESTAMP
        ''', user_id)

async def get_user_stats(user_id: int, db: DatabaseManager) -> Dict[str, Any]:
    row = await db.fetchrow('SELECT * FROM user_stats WHERE user_id = $1', user_id)
    return row or {
        'user_id': user_id,
        'total_commands': 0,
        'successful_commands': 0,
        'files_created': 0,
        'files_edited': 0,
        'repos_created': 0,
        'last_active': None
    }

async def create_github_repo(github_client, repo_name: str, is_private: bool = True):
    try:
        user = github_client.get_user()
        repo = user.create_repo(
            name=repo_name,
            private=is_private,
            auto_init=True,
            description="Created via GitCord Bot"
        )
        return repo, None
    except GithubException as e:
        return None, str(e)

async def github_api_request(method: str, endpoint: str, data: dict = None, token: str = GITHUB_TOKEN):
    headers = {
        "Authorization": f"token {token}",
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
    
    @commands.command(name='repo')
    async def cmd_repo(self, ctx, repo_name: str = None, private: str = "true"):
        start_time = datetime.now()
        
        try:
            if not repo_name:
                current = await get_current_repo(ctx.author.id, self.bot.db)
                embed = discord.Embed(
                    title="Current Repository",
                    description=f"**{current}**",
                    color=discord.Color.blue()
                )
                
                try:
                    if self.bot.github_client:
                        repo = self.bot.github_client.get_repo(f"{GITHUB_USERNAME}/{current}")
                        embed.add_field(name="URL", value=repo.html_url, inline=False)
                        embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
                        try:
                            contents = repo.get_contents("")
                            file_count = len([item for item in contents if item.type == "file"])
                            embed.add_field(name="Files", value=str(file_count), inline=True)
                        except:
                            embed.add_field(name="Files", value="0", inline=True)
                except Exception as e:
                    embed.add_field(name="Status", value="Repository will be created on first use", inline=False)
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'repo', "check", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            repo_name = sanitize_filename(repo_name)
            is_private = private.lower() in ['true', 'yes', '1', 'private']
            
            if not self.bot.github_client:
                await ctx.send("GitHub client not initialized")
                return
            
            try:
                # Try to access existing repo
                repo = self.bot.github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
                await set_current_repo(ctx.author.id, repo_name, self.bot.db)
                
                embed = discord.Embed(
                    title="Repository Switched",
                    description=f"Now working in **{repo_name}**",
                    color=discord.Color.green()
                )
                embed.add_field(name="URL", value=repo.html_url, inline=False)
                embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
                
                try:
                    contents = repo.get_contents("")
                    file_count = len([item for item in contents if item.type == "file"])
                    embed.add_field(name="Files", value=str(file_count), inline=True)
                except:
                    embed.add_field(name="Files", value="0", inline=True)
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'repo', f"name={repo_name}", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                
            except GithubException as e:
                # Check if it's a "not found" error or "already exists" error
                error_str = str(e)
                if "Not Found" in error_str or "404" in error_str:
                    # Repository doesn't exist - offer to create
                    embed = discord.Embed(
                        title="Repository Not Found",
                        description=f"Repository `{repo_name}` doesn't exist. Create it?",
                        color=discord.Color.orange()
                    )
                    
                    msg = await ctx.send(embed=embed)
                    await msg.add_reaction("✅")
                    await msg.add_reaction("❌")
                    
                    def check(reaction, user):
                        return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == msg.id
                    
                    try:
                        reaction, user = await self.bot.wait_for('reaction_add', timeout=30.0, check=check)
                        
                        if str(reaction.emoji) == "✅":
                            repo, error = await create_github_repo(self.bot.github_client, repo_name, is_private)
                            if repo:
                                await set_current_repo(ctx.author.id, repo_name, self.bot.db)
                                
                                embed = discord.Embed(
                                    title="Repository Created",
                                    description=f"Created and switched to **{repo_name}**",
                                    color=discord.Color.green()
                                )
                                embed.add_field(name="URL", value=repo.html_url, inline=False)
                                embed.add_field(name="Visibility", value="Private" if is_private else "Public", inline=True)
                                
                                await msg.edit(embed=embed)
                                await msg.clear_reactions()
                                await log_command(ctx.author.id, 'repo', f"name={repo_name}, create=true", True, self.bot.db, 
                                                execution_time=(datetime.now() - start_time).total_seconds())
                            else:
                                await ctx.send(f"Failed to create repository: {error}")
                                self.bot.logger.log_failure(ctx.author.id, "create_repo", repo_name, error)
                                await log_command(ctx.author.id, 'repo', f"name={repo_name}", False, self.bot.db, error, 
                                                execution_time=(datetime.now() - start_time).total_seconds())
                        else:
                            await msg.edit(content="Repository creation cancelled.", embed=None)
                            await msg.clear_reactions()
                            
                    except asyncio.TimeoutError:
                        await msg.edit(content="Repository creation timed out.", embed=None)
                        await msg.clear_reactions()
                else:
                    # Some other GitHub error
                    await ctx.send(f"GitHub Error: {error_str}")
                    self.bot.logger.log_failure(ctx.author.id, "repo_access", repo_name, error_str)
                    await log_command(ctx.author.id, 'repo', f"name={repo_name}", False, self.bot.db, error_str, 
                                    execution_time=(datetime.now() - start_time).total_seconds())
                    
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            self.bot.logger.log_failure(ctx.author.id, "repo", repo_name or "unknown", error_msg)
            await log_command(ctx.author.id, 'repo', f"name={repo_name or 'none'}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='create')
    async def cmd_create(self, ctx, filename: str, *, content: str):
        start_time = datetime.now()
        
        try:
            repo_name = await get_current_repo(ctx.author.id, self.bot.db)
            filename = sanitize_filename(filename)
            
            if len(content) > 10000:
                error = "File too large (max 10KB)"
                await ctx.send(f"{error}")
                self.bot.logger.log_failure(ctx.author.id, "create", filename, error, {"size": len(content)})
                await log_command(ctx.author.id, 'create', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            # Check if file exists
            status, existing = await github_api_request(
                "GET",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
            )
            
            if status == 200:
                error = "File already exists"
                await ctx.send(f"{error}. Use `--edit` instead.")
                self.bot.logger.log_failure(ctx.author.id, "create", filename, error, {"repo": repo_name})
                await log_command(ctx.author.id, 'create', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            # Create file
            status, response = await github_api_request(
                "PUT",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
                {
                    "message": f"Create {filename} via GitCord",
                    "content": encode_content(content),
                    "branch": "main"
                }
            )
            
            if status == 201:
                await log_file_history(ctx.author.id, repo_name, filename, 'create', self.bot.db, content)
                self.bot.logger.log_success(ctx.author.id, "create", filename, {"repo": repo_name, "size": len(content)})
                
                embed = discord.Embed(
                    title="File Created",
                    description=f"Created `{filename}` in `{repo_name}`",
                    color=discord.Color.green()
                )
                embed.add_field(name="Size", value=f"{len(content)} characters", inline=True)
                
                if len(content) <= 500:
                    embed.add_field(name="Preview", value=f"```\n{content[:200]}...\n```", inline=False)
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'create', f"filename={filename}", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
            else:
                error = response.get('message', 'Unknown error')
                await ctx.send(f"GitHub API Error: {error}")
                self.bot.logger.log_failure(ctx.author.id, "create", filename, error, {"repo": repo_name, "status": status})
                await log_command(ctx.author.id, 'create', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            self.bot.logger.log_failure(ctx.author.id, "create", filename, error_msg)
            await log_command(ctx.author.id, 'create', f"filename={filename}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='edit')
    async def cmd_edit(self, ctx, filename: str, *, content: str):
        start_time = datetime.now()
        
        try:
            repo_name = await get_current_repo(ctx.author.id, self.bot.db)
            filename = sanitize_filename(filename)
            
            if len(content) > 10000:
                error = "File too large (max 10KB)"
                await ctx.send(f"{error}")
                self.bot.logger.log_failure(ctx.author.id, "edit", filename, error, {"size": len(content)})
                await log_command(ctx.author.id, 'edit', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            # Get existing file
            status, file_data = await github_api_request(
                "GET",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
            )
            
            if status != 200:
                error = "File not found"
                await ctx.send(f"{error}")
                self.bot.logger.log_failure(ctx.author.id, "edit", filename, error, {"repo": repo_name})
                await log_command(ctx.author.id, 'edit', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            sha = file_data['sha']
            
            # Update file
            status, response = await github_api_request(
                "PUT",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
                {
                    "message": f"Update {filename} via GitCord",
                    "content": encode_content(content),
                    "sha": sha,
                    "branch": "main"
                }
            )
            
            if status == 200:
                await log_file_history(ctx.author.id, repo_name, filename, 'edit', self.bot.db, content)
                self.bot.logger.log_success(ctx.author.id, "edit", filename, {"repo": repo_name, "size": len(content)})
                
                embed = discord.Embed(
                    title="File Updated",
                    description=f"Updated `{filename}` in `{repo_name}`",
                    color=discord.Color.green()
                )
                
                old_content = base64.b64decode(file_data['content']).decode('utf-8')
                embed.add_field(name="Previous size", value=f"{len(old_content)} characters", inline=True)
                embed.add_field(name="New size", value=f"{len(content)} characters", inline=True)
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'edit', f"filename={filename}", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
            else:
                error = response.get('message', 'Unknown error')
                await ctx.send(f"GitHub API Error: {error}")
                self.bot.logger.log_failure(ctx.author.id, "edit", filename, error, {"repo": repo_name, "status": status})
                await log_command(ctx.author.id, 'edit', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            self.bot.logger.log_failure(ctx.author.id, "edit", filename, error_msg)
            await log_command(ctx.author.id, 'edit', f"filename={filename}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='view')
    async def cmd_view(self, ctx, filename: str):
        start_time = datetime.now()
        
        try:
            repo_name = await get_current_repo(ctx.author.id, self.bot.db)
            filename = sanitize_filename(filename)
            
            status, response = await github_api_request(
                "GET",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
            )
            
            if status == 200:
                content = base64.b64decode(response['content']).decode('utf-8')
                
                if len(content) > 1500:
                    content = content[:1500] + "\n... (truncated)"
                
                ext = filename.split('.')[-1].lower() if '.' in filename else 'txt'
                languages = {
                    'py': 'python', 'js': 'javascript', 'ts': 'typescript',
                    'html': 'html', 'css': 'css', 'json': 'json',
                    'md': 'markdown', 'txt': 'text', 'yml': 'yaml', 'yaml': 'yaml'
                }
                lang = languages.get(ext, 'text')
                
                embed = discord.Embed(
                    title=f"{filename}",
                    description=f"From `{repo_name}`",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Content", value=f"```{lang}\n{content}\n```", inline=False)
                embed.add_field(name="Size", value=f"{len(content)} characters", inline=True)
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'view', f"filename={filename}", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
            else:
                error = response.get('message', 'File not found')
                await ctx.send(f"Error: {error}")
                await log_command(ctx.author.id, 'view', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            await log_command(ctx.author.id, 'view', f"filename={filename}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='list')
    async def cmd_list(self, ctx):
        start_time = datetime.now()
        
        try:
            repo_name = await get_current_repo(ctx.author.id, self.bot.db)
            
            if not repo_name:
                await ctx.send("No repository selected. Use `--repo <name>` first.")
                return
            
            # Try GitHub API first
            status, response = await github_api_request(
                "GET",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents"
            )
            
            if status == 200:
                files = []
                directories = []
                
                for item in response:
                    if isinstance(item, dict):
                        if item.get('type') == 'file':
                            files.append(item.get('name', 'unknown'))
                        elif item.get('type') == 'dir':
                            directories.append(item.get('name', 'unknown'))
                    else:
                        # Fallback for list structure
                        if 'type' in item and item['type'] == 'file':
                            files.append(item.get('name', 'unknown'))
                        elif 'type' in item and item['type'] == 'dir':
                            directories.append(item.get('name', 'unknown'))
                
                embed = discord.Embed(
                    title=f"{repo_name}",
                    color=discord.Color.purple()
                )
                
                if files:
                    file_list = "\n".join([f"{f}" for f in files[:20]])
                    if len(files) > 20:
                        file_list += f"\n... and {len(files) - 20} more"
                    embed.add_field(name=f"Files ({len(files)})", value=file_list, inline=False)
                
                if directories:
                    dir_list = "\n".join([f"{d}" for d in directories[:10]])
                    if len(directories) > 10:
                        dir_list += f"\n... and {len(directories) - 10} more"
                    embed.add_field(name=f"Directories ({len(directories)})", value=dir_list, inline=False)
                
                if not files and not directories:
                    embed.description = "Repository is empty or doesn't exist"
                
                await ctx.send(embed=embed)
                await log_command(ctx.author.id, 'list', "", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
            else:
                # Try PyGithub as fallback
                try:
                    if self.bot.github_client:
                        repo = self.bot.github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
                        contents = repo.get_contents("")
                        
                        files = []
                        directories = []
                        
                        for content in contents:
                            if content.type == "file":
                                files.append(content.name)
                            elif content.type == "dir":
                                directories.append(content.name)
                        
                        embed = discord.Embed(
                            title=f"{repo_name}",
                            color=discord.Color.purple()
                        )
                        
                        if files:
                            file_list = "\n".join([f"{f}" for f in files[:20]])
                            if len(files) > 20:
                                file_list += f"\n... and {len(files) - 20} more"
                            embed.add_field(name=f"Files ({len(files)})", value=file_list, inline=False)
                        
                        if directories:
                            dir_list = "\n".join([f"{d}" for d in directories[:10]])
                            if len(directories) > 10:
                                dir_list += f"\n... and {len(directories) - 10} more"
                            embed.add_field(name=f"Directories ({len(directories)})", value=dir_list, inline=False)
                        
                        await ctx.send(embed=embed)
                        await log_command(ctx.author.id, 'list', "", True, self.bot.db, 
                                        execution_time=(datetime.now() - start_time).total_seconds())
                    else:
                        error = "GitHub client not available"
                        await ctx.send(f"Error: {error}")
                        await log_command(ctx.author.id, 'list', "", False, self.bot.db, error, 
                                        execution_time=(datetime.now() - start_time).total_seconds())
                except Exception as e:
                    error = f"Repository not found or empty: {str(e)}"
                    await ctx.send(f"Error: {error}")
                    await log_command(ctx.author.id, 'list', "", False, self.bot.db, error, 
                                    execution_time=(datetime.now() - start_time).total_seconds())
                
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            await log_command(ctx.author.id, 'list', "", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='delete')
    async def cmd_delete(self, ctx, filename: str):
        start_time = datetime.now()
        
        try:
            repo_name = await get_current_repo(ctx.author.id, self.bot.db)
            filename = sanitize_filename(filename)
            
            # Get file SHA first
            status, file_data = await github_api_request(
                "GET",
                f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
            )
            
            if status != 200:
                error = "File not found"
                await ctx.send(f"{error}")
                self.bot.logger.log_failure(ctx.author.id, "delete", filename, error, {"repo": repo_name})
                await log_command(ctx.author.id, 'delete', f"filename={filename}", False, self.bot.db, error, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            sha = file_data['sha']
            
            # Confirm deletion
            embed = discord.Embed(
                title="Confirm Deletion",
                description=f"Delete `{filename}` from `{repo_name}`?",
                color=discord.Color.red()
            )
            embed.add_field(name="This action cannot be undone", value="React with checkmark to confirm", inline=False)
            
            msg = await ctx.send(embed=embed)
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            
            def check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == msg.id
            
            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=30.0, check=check)
                
                if str(reaction.emoji) == "✅":
                    # Delete file
                    status, response = await github_api_request(
                        "DELETE",
                        f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
                        {
                            "message": f"Delete {filename} via GitCord",
                            "sha": sha,
                            "branch": "main"
                        }
                    )
                    
                    if status == 200:
                        await log_file_history(ctx.author.id, repo_name, filename, 'delete', self.bot.db)
                        self.bot.logger.log_success(ctx.author.id, "delete", filename, {"repo": repo_name})
                        
                        await ctx.send(f"Deleted `{filename}` from `{repo_name}`")
                        await log_command(ctx.author.id, 'delete', f"filename={filename}", True, self.bot.db, 
                                        execution_time=(datetime.now() - start_time).total_seconds())
                    else:
                        error = response.get('message', 'Unknown error')
                        await ctx.send(f"Delete failed: {error}")
                        self.bot.logger.log_failure(ctx.author.id, "delete", filename, error, {"repo": repo_name, "status": status})
                        await log_command(ctx.author.id, 'delete', f"filename={filename}", False, self.bot.db, error, 
                                        execution_time=(datetime.now() - start_time).total_seconds())
                else:
                    await ctx.send("Deletion cancelled")
                    await log_command(ctx.author.id, 'delete', f"filename={filename}", False, self.bot.db, "Cancelled by user", 
                                    execution_time=(datetime.now() - start_time).total_seconds())
                    
            except asyncio.TimeoutError:
                await ctx.send("Deletion timed out")
                await log_command(ctx.author.id, 'delete', f"filename={filename}", False, self.bot.db, "Timeout", 
                                execution_time=(datetime.now() - start_time).total_seconds())
                
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            self.bot.logger.log_failure(ctx.author.id, "delete", filename, error_msg)
            await log_command(ctx.author.id, 'delete', f"filename={filename}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='current')
    async def cmd_current(self, ctx):
        repo_name = await get_current_repo(ctx.author.id, self.bot.db)
        await ctx.send(f"Current repository: `{repo_name}`")
    
    @commands.command(name='stats')
    async def cmd_stats(self, ctx, user: discord.User = None):
        start_time = datetime.now()
        
        try:
            target = user or ctx.author
            stats = await get_user_stats(target.id, self.bot.db)
            
            embed = discord.Embed(
                title=f"Statistics for {target.display_name}",
                color=discord.Color.gold()
            )
            
            total = stats['total_commands']
            success = stats['successful_commands']
            rate = (success / total * 100) if total > 0 else 0
            
            embed.add_field(name="Total Commands", value=total, inline=True)
            embed.add_field(name="Success Rate", value=f"{rate:.1f}%", inline=True)
            embed.add_field(name=" ", value=" ", inline=True)
            
            embed.add_field(name="Files Created", value=stats['files_created'], inline=True)
            embed.add_field(name="Files Edited", value=stats['files_edited'], inline=True)
            embed.add_field(name="Repos Created", value=stats['repos_created'], inline=True)
            
            if stats['last_active']:
                last_active = stats['last_active'].strftime("%Y-%m-%d %H:%M")
                embed.add_field(name="Last Active", value=last_active, inline=False)
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'stats', f"target={target.id}", True, self.bot.db, 
                            execution_time=(datetime.now() - start_time).total_seconds())
            
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            await log_command(ctx.author.id, 'stats', "", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='history')
    async def cmd_history(self, ctx, limit: int = 10):
        start_time = datetime.now()
        
        try:
            rows = await self.bot.db.fetch(
                'SELECT filename, action, repo_name, created_at FROM file_history WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2',
                ctx.author.id, limit
            )
            
            if not rows:
                await ctx.send("No file history found")
                await log_command(ctx.author.id, 'history', f"limit={limit}", True, self.bot.db, 
                                execution_time=(datetime.now() - start_time).total_seconds())
                return
            
            history_text = ""
            for i, row in enumerate(rows, 1):
                time = row['created_at'].strftime("%m/%d %H:%M")
                action_text = row['action']
                history_text += f"{i}. {time} {action_text} - {row['filename']} ({row['repo_name']})\n"
            
            embed = discord.Embed(
                title="Recent File Activity",
                description=history_text,
                color=discord.Color.blue()
            )
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'history', f"limit={limit}", True, self.bot.db, 
                            execution_time=(datetime.now() - start_time).total_seconds())
            
        except Exception as e:
            error_msg = str(e)
            await ctx.send(f"Error: {error_msg}")
            await log_command(ctx.author.id, 'history', f"limit={limit}", False, self.bot.db, error_msg, 
                            execution_time=(datetime.now() - start_time).total_seconds())
    
    @commands.command(name='template')
    async def cmd_template(self, ctx, action: str = None, template_name: str = None, *, content: str = None):
        if action == "create" and template_name and content:
            await self.bot.db.execute(
                'INSERT INTO file_templates (user_id, template_name, content_template) VALUES ($1, $2, $3) ON CONFLICT (user_id, template_name) DO UPDATE SET content_template = $3',
                ctx.author.id, template_name, content
            )
            await ctx.send(f"Template `{template_name}` saved")
            
        elif action == "list":
            templates = await self.bot.db.fetch('SELECT template_name FROM file_templates WHERE user_id = $1', ctx.author.id)
            if templates:
                template_list = "\n".join([f"- {t[0]}" for t in templates])
                await ctx.send(f"Your templates:\n{template_list}")
            else:
                await ctx.send("No templates saved")
                
        elif action == "use" and template_name:
            row = await self.bot.db.fetchrow('SELECT content_template FROM file_templates WHERE user_id = $1 AND template_name = $2', ctx.author.id, template_name)
            if row:
                await ctx.send(f"Template `{template_name}`:\n```\n{row[0]}\n```")
            else:
                await ctx.send(f"Template `{template_name}` not found")
                
        elif action == "delete" and template_name:
            await self.bot.db.execute('DELETE FROM file_templates WHERE user_id = $1 AND template_name = $2', ctx.author.id, template_name)
            await ctx.send(f"Template `{template_name}` deleted")
            
        else:
            await ctx.send("Usage: `--template create <name> <content>` | `--template list` | `--template use <name>` | `--template delete <name>`")
    
    @commands.command(name='prefix')
    async def cmd_prefix(self, ctx, new_prefix: str = None):
        if not new_prefix:
            settings = await get_user_settings(ctx.author.id, self.bot.db)
            current = settings.get('preferred_prefix', PREFIX)
            await ctx.send(f"Your prefix: `{current}`")
            return
        
        if len(new_prefix) > 3:
            await ctx.send("Prefix must be 3 characters or less")
            return
        
        await update_user_settings(ctx.author.id, self.bot.db, preferred_prefix=new_prefix)
        await ctx.send(f"Your prefix updated to: `{new_prefix}`")

    # ========== SLASH COMMANDS ==========
    
    @app_commands.command(name="create", description="Create a new file")
    @app_commands.describe(
        filename="File name",
        content="File content"
    )
    async def slash_create(self, interaction: discord.Interaction, filename: str, content: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_create(ctx, filename, content=content)
    
    @app_commands.command(name="edit", description="Edit a file")
    @app_commands.describe(
        filename="File name",
        content="New content"
    )
    async def slash_edit(self, interaction: discord.Interaction, filename: str, content: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_edit(ctx, filename, content=content)
    
    @app_commands.command(name="repo", description="Switch to a repository")
    @app_commands.describe(
        repo_name="Repository name",
        private="Make private"
    )
    async def slash_repo(self, interaction: discord.Interaction, repo_name: str, private: bool = True):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_repo(ctx, repo_name, "true" if private else "false")
    
    @app_commands.command(name="view", description="View a file")
    @app_commands.describe(
        filename="File name"
    )
    async def slash_view(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_view(ctx, filename)
    
    @app_commands.command(name="list", description="List repository files")
    async def slash_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_list(ctx)
    
    @app_commands.command(name="stats", description="Show user statistics")
    @app_commands.describe(
        user="User to check (optional)"
    )
    async def slash_stats(self, interaction: discord.Interaction, user: discord.User = None):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_stats(ctx, user)

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='restart')
    @commands.is_owner()
    async def cmd_restart(self, ctx):
        await ctx.send("Restarting bot...")
        print("Bot restart initiated")
        os.execv(sys.executable, ['python'] + sys.argv)
    
    @commands.command(name='logs')
    @commands.is_owner()
    async def cmd_logs(self, ctx, hours: int = 24):
        if hours > 168:
            hours = 168
        
        failures = self.bot.logger.get_recent_failures(hours)
        
        if not failures:
            await ctx.send(f"No failures in the last {hours} hours")
            return
        
        embed = discord.Embed(
            title=f"Recent Failures ({hours}h)",
            color=discord.Color.red()
        )
        
        for i, failure in enumerate(failures[:10], 1):
            time = datetime.fromisoformat(failure['timestamp']).strftime("%H:%M")
            embed.add_field(
                name=f"{i}. {time} - {failure['operation']}",
                value=f"User: {failure['user_id']}\nFile: {failure['filename']}\nError: {failure['error'][:100]}",
                inline=False
            )
        
        if len(failures) > 10:
            embed.set_footer(text=f"And {len(failures) - 10} more failures...")
        
        await ctx.send(embed=embed)
    
    @commands.command(name='status')
    @commands.is_owner()
    async def cmd_status(self, ctx):
        embed = discord.Embed(
            title="Bot Status",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Uptime", value="Online", inline=True)
        embed.add_field(name="Servers", value=len(self.bot.guilds), inline=True)
        embed.add_field(name="Users", value=len(self.bot.users), inline=True)
        
        if self.bot.db and self.bot.db.db_type:
            embed.add_field(name="Database", value=self.bot.db.db_type.capitalize(), inline=True)
        else:
            embed.add_field(name="Database", value="Not connected", inline=True)
        
        if self.bot.github_client:
            try:
                user = self.bot.github_client.get_user()
                embed.add_field(name="GitHub", value=f"Connected as {user.login}", inline=True)
            except:
                embed.add_field(name="GitHub", value="Disconnected", inline=True)
        else:
            embed.add_field(name="GitHub", value="Not initialized", inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='cleanup')
    @commands.is_owner()
    async def cmd_cleanup(self, ctx, days: int = 30):
        deleted = await self.bot.db.fetchval(
            'DELETE FROM command_logs WHERE created_at < NOW() - INTERVAL \'$1 days\' RETURNING COUNT(*)',
            days
        )
        await ctx.send(f"Cleaned up {deleted} old log entries (older than {days} days)")

# ========== HELP COMMAND ==========

@bot.command(name='help')
async def cmd_help(ctx, command: str = None):
    if command:
        commands_info = {
            'repo': "Switch to/create repository\nUsage: `--repo [name] [private=true/false]`",
            'create': "Create new file\nUsage: `--create <filename> <content>`",
            'edit': "Edit existing file\nUsage: `--edit <filename> <content>`",
            'view': "View file content\nUsage: `--view <filename>`",
            'list': "List repository files\nUsage: `--list`",
            'delete': "Delete a file\nUsage: `--delete <filename>`",
            'stats': "Show statistics\nUsage: `--stats [@user]`",
            'history': "Show file history\nUsage: `--history [limit]`",
            'template': "Manage templates\nUsage: `--template create|list|use|delete <name> [content]`",
            'prefix': "Set your prefix\nUsage: `--prefix [new_prefix]`",
            'logs': "Show failure logs (owner)\nUsage: `--logs [hours]`",
            'restart': "Restart bot (owner)\nUsage: `--restart`",
            'status': "Show bot status (owner)\nUsage: `--status`",
            'cleanup': "Clean database (owner)\nUsage: `--cleanup [days]`",
            'help': "Show this help\nUsage: `--help [command]`"
        }
        
        if command in commands_info:
            embed = discord.Embed(
                title=f"Command: {command}",
                description=commands_info[command],
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"Command `{command}` not found")
    else:
        embed = discord.Embed(
            title="GitCord Help",
            description="GitHub management bot for Discord",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Repository Management",
            value="`--repo` - Switch/create repository\n`--list` - List files\n`--current` - Show current repo",
            inline=False
        )
        
        embed.add_field(
            name="File Operations",
            value="`--create` - Create file\n`--edit` - Edit file\n`--view` - View file\n`--delete` - Delete file",
            inline=False
        )
        
        embed.add_field(
            name="Productivity",
            value="`--template` - File templates\n`--history` - File history\n`--stats` - User statistics\n`--prefix` - Set prefix",
            inline=False
        )
        
        embed.add_field(
            name="Administration (Owner)",
            value="`--logs` - Failure logs\n`--status` - Bot status\n`--cleanup` - Clean database\n`--restart` - Restart bot",
            inline=False
        )
        
        embed.add_field(
            name="Usage",
            value=f"• Prefix commands: `{PREFIX}command`\n• Slash commands also available\n• Use `{PREFIX}help <command>` for details",
            inline=False
        )
        
        embed.set_footer(text=f"Bot version 2.0 | Prefix: {PREFIX}")
        
        await ctx.send(embed=embed)

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"Command not found. Use `{PREFIX}help` for available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument. Use `{PREFIX}help {ctx.command.name}` for usage.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("This command is for bot owners only.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Command on cooldown. Try again in {error.retry_after:.1f} seconds.")
    else:
        print(f"Command Error: {error}")
        if bot.logger:
            bot.logger.log_failure(ctx.author.id, ctx.command.name if ctx.command else "unknown", 
                              "command_error", str(error))
        await ctx.send("An error occurred while executing the command.")

# ========== START BOT ==========

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN environment variable not set")
        sys.exit(1)
    elif not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN environment variable not set")
        sys.exit(1)
    elif not GITHUB_USERNAME:
        print("Error: GITHUB_USERNAME environment variable not set")
        sys.exit(1)
    else:
        print("Starting GitCord Bot...")
        try:
            bot.run(DISCORD_TOKEN)
        except KeyboardInterrupt:
            print("Bot stopped by user")
        except Exception as e:
            print(f"Bot crashed: {e}")
            if bot.db:
                asyncio.run(bot.db.close())
        finally:
            print("Bot shutdown complete")
