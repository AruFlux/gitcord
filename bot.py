import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import json
import sys
import asyncio
from typing import Optional, Dict, Any
from github import Github, GithubException
from dotenv import load_dotenv
import re
from datetime import datetime
import asyncpg
import aiosqlite
from contextlib import asynccontextmanager

# Disable voice support at OS level
os.environ["DISCORD_VOICE"] = "false"

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
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
tree = app_commands.CommandTree(bot)

# GitHub client
github_client = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None

# Database setup
db_pool = None
use_sqlite = False

# ========== DATABASE MANAGER ==========

class DatabaseManager:
    """Unified database manager with PostgreSQL/SQLite support"""
    
    def __init__(self):
        self.pool = None
        self.use_sqlite = False
        self.sqlite_conn = None
        
    async def initialize(self):
        """Initialize database connection"""
        global db_pool, use_sqlite
        
        if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
            try:
                # Try PostgreSQL with asyncpg
                self.pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    command_timeout=60
                )
                db_pool = self.pool
                await self._create_postgres_tables()
                print("✅ Connected to PostgreSQL database")
                return True
            except Exception as e:
                print(f"❌ PostgreSQL connection failed: {e}")
                print("🔄 Falling back to SQLite...")
                self.use_sqlite = True
                use_sqlite = True
        else:
            self.use_sqlite = True
            use_sqlite = True
        
        # Fallback to SQLite
        if self.use_sqlite:
            await self._initialize_sqlite()
            return True
        
        return False
    
    async def _create_postgres_tables(self):
        """Create PostgreSQL tables"""
        async with self.pool.acquire() as conn:
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
                    commit_message TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
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
                    execution_time FLOAT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
                )
            ''')
            
            # File templates table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS file_templates (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    template_name TEXT,
                    filename_pattern TEXT,
                    content_template TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id, template_name),
                    FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
                )
            ''')
            
            # User analytics table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_analytics (
                    user_id BIGINT PRIMARY KEY,
                    total_commands INTEGER DEFAULT 0,
                    successful_commands INTEGER DEFAULT 0,
                    files_created INTEGER DEFAULT 0,
                    files_edited INTEGER DEFAULT 0,
                    repos_created INTEGER DEFAULT 0,
                    last_active TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
                )
            ''')
    
    async def _initialize_sqlite(self):
        """Initialize SQLite database"""
        self.sqlite_conn = await aiosqlite.connect("gitcord.db")
        
        # Enable foreign keys
        await self.sqlite_conn.execute("PRAGMA foreign_keys = ON")
        
        # Create tables
        await self.sqlite_conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_repo TEXT,
                preferred_prefix TEXT DEFAULT '--',
                auto_create_repo BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self.sqlite_conn.execute('''
            CREATE TABLE IF NOT EXISTS repository_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                repo_name TEXT,
                action TEXT,
                filename TEXT,
                commit_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
            )
        ''')
        
        await self.sqlite_conn.execute('''
            CREATE TABLE IF NOT EXISTS command_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                command TEXT,
                arguments TEXT,
                success BOOLEAN,
                error_message TEXT,
                execution_time REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
            )
        ''')
        
        await self.sqlite_conn.execute('''
            CREATE TABLE IF NOT EXISTS file_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                template_name TEXT,
                filename_pattern TEXT,
                content_template TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, template_name),
                FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
            )
        ''')
        
        await self.sqlite_conn.execute('''
            CREATE TABLE IF NOT EXISTS user_analytics (
                user_id INTEGER PRIMARY KEY,
                total_commands INTEGER DEFAULT 0,
                successful_commands INTEGER DEFAULT 0,
                files_created INTEGER DEFAULT 0,
                files_edited INTEGER DEFAULT 0,
                repos_created INTEGER DEFAULT 0,
                last_active TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_settings(user_id) ON DELETE CASCADE
            )
        ''')
        
        await self.sqlite_conn.commit()
        print("✅ Connected to SQLite database")
    
    async def execute(self, query: str, *args):
        """Execute a query"""
        if self.use_sqlite:
            await self.sqlite_conn.execute(query, args)
            await self.sqlite_conn.commit()
        else:
            async with self.pool.acquire() as conn:
                await conn.execute(query, *args)
    
    async def fetchrow(self, query: str, *args):
        """Fetch a single row"""
        if self.use_sqlite:
            cursor = await self.sqlite_conn.execute(query, args)
            row = await cursor.fetchone()
            await cursor.close()
            return row
        else:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow(query, *args)
    
    async def fetch(self, query: str, *args):
        """Fetch multiple rows"""
        if self.use_sqlite:
            cursor = await self.sqlite_conn.execute(query, args)
            rows = await cursor.fetchall()
            await cursor.close()
            return rows
        else:
            async with self.pool.acquire() as conn:
                return await conn.fetch(query, *args)
    
    async def fetchval(self, query: str, *args):
        """Fetch a single value"""
        if self.use_sqlite:
            cursor = await self.sqlite_conn.execute(query, args)
            row = await cursor.fetchone()
            await cursor.close()
            return row[0] if row else None
        else:
            async with self.pool.acquire() as conn:
                return await conn.fetchval(query, *args)
    
    async def close(self):
        """Close database connections"""
        if self.use_sqlite and self.sqlite_conn:
            await self.sqlite_conn.close()
        elif self.pool:
            await self.pool.close()

# Initialize database manager
db = DatabaseManager()

# ========== HELPER FUNCTIONS ==========

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be GitHub-safe"""
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    return filename

def validate_github_access():
    """Validate GitHub access"""
    if not github_client:
        return False, "GitHub client not initialized"
    
    try:
        user = github_client.get_user()
        return True, f"Connected as {user.login}"
    except Exception as e:
        return False, f"GitHub error: {str(e)}"

async def get_user_settings(user_id: int) -> Dict[str, Any]:
    """Get user settings from database"""
    row = await db.fetchrow(
        'SELECT * FROM user_settings WHERE user_id = $1',
        user_id
    )
    return dict(row) if row else {}

async def update_user_settings(user_id: int, **kwargs):
    """Update user settings"""
    settings = await get_user_settings(user_id)
    
    if settings:
        # Update existing
        set_clause = ', '.join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
        values = [user_id] + list(kwargs.values())
        await db.execute(
            f'UPDATE user_settings SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = $1',
            *values
        )
    else:
        # Insert new
        columns = ['user_id'] + list(kwargs.keys())
        placeholders = ', '.join([f'${i+1}' for i in range(len(columns))])
        values = [user_id] + list(kwargs.values())
        
        await db.execute(
            f'INSERT INTO user_settings ({", ".join(columns)}) VALUES ({placeholders})',
            *values
        )

async def get_current_repo(user_id: int) -> str:
    """Get user's current repository"""
    settings = await get_user_settings(user_id)
    return settings.get('default_repo', DEFAULT_REPO)

async def set_current_repo(user_id: int, repo_name: str):
    """Set user's current repository"""
    await update_user_settings(user_id, default_repo=repo_name)

async def log_command(user_id: int, command: str, arguments: str, success: bool, error_message: str = None, execution_time: float = 0.0):
    """Log command usage"""
    await db.execute(
        'INSERT INTO command_logs (user_id, command, arguments, success, error_message, execution_time) VALUES ($1, $2, $3, $4, $5, $6)',
        user_id, command, arguments, success, error_message, execution_time
    )
    
    # Update analytics
    await db.execute('''
        INSERT INTO user_analytics (user_id, total_commands, successful_commands, last_active)
        VALUES ($1, 1, $2, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id) DO UPDATE SET
            total_commands = user_analytics.total_commands + 1,
            successful_commands = user_analytics.successful_commands + $2,
            last_active = CURRENT_TIMESTAMP
    ''', user_id, 1 if success else 0)

async def log_repo_action(user_id: int, repo_name: str, action: str, filename: str = None, commit_message: str = None):
    """Log repository actions"""
    await db.execute(
        'INSERT INTO repository_history (user_id, repo_name, action, filename, commit_message) VALUES ($1, $2, $3, $4, $5)',
        user_id, repo_name, action, filename, commit_message
    )
    
    # Update analytics based on action
    if action == 'create_file':
        await db.execute('''
            INSERT INTO user_analytics (user_id, files_created, last_active)
            VALUES ($1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                files_created = user_analytics.files_created + 1,
                last_active = CURRENT_TIMESTAMP
        ''', user_id)
    elif action == 'edit_file':
        await db.execute('''
            INSERT INTO user_analytics (user_id, files_edited, last_active)
            VALUES ($1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                files_edited = user_analytics.files_edited + 1,
                last_active = CURRENT_TIMESTAMP
        ''', user_id)
    elif action == 'create_repo':
        await db.execute('''
            INSERT INTO user_analytics (user_id, repos_created, last_active)
            VALUES ($1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                repos_created = user_analytics.repos_created + 1,
                last_active = CURRENT_TIMESTAMP
        ''', user_id)

async def get_user_stats(user_id: int) -> Dict[str, Any]:
    """Get user statistics"""
    row = await db.fetchrow('SELECT * FROM user_analytics WHERE user_id = $1', user_id)
    return dict(row) if row else {
        'user_id': user_id,
        'total_commands': 0,
        'successful_commands': 0,
        'files_created': 0,
        'files_edited': 0,
        'repos_created': 0,
        'last_active': None
    }

async def create_github_repo(repo_name: str, is_private: bool = True):
    """Create a new GitHub repository"""
    try:
        user = github_client.get_user()
        repo = user.create_repo(
            name=repo_name,
            private=is_private,
            auto_init=True,
            description="Created via GitCord Discord Bot",
            has_issues=True,
            has_wiki=True,
            has_downloads=True
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

async def github_api_request(method: str, endpoint: str, data: dict = None):
    """Make GitHub API request"""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitCord-Discord-Bot"
    }
    
    url = f"https://api.github.com{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=data) as response:
            return response.status, await response.json()

# ========== BOT EVENTS ==========

@bot.event
async def on_ready():
    """Bot startup handler"""
    print(f'🤖 Bot {bot.user} is online')
    print(f'📝 Prefix: {PREFIX}')
    
    # Initialize database
    if await db.initialize():
        print('✅ Database initialized')
    else:
        print('⚠️  Database not available, using memory storage')
    
    # Validate GitHub access
    valid, message = validate_github_access()
    if valid:
        print(f'✅ {message}')
    else:
        print(f'❌ {message}')
    
    # Sync slash commands
    try:
        synced = await tree.sync()
        print(f'✅ Synced {len(synced)} slash command(s)')
    except Exception as e:
        print(f'❌ Error syncing commands: {e}')
    
    # Set bot status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="GitHub repositories"
        )
    )

# ========== PREFIX COMMANDS ==========

@bot.command(name='repo')
async def prefix_repo(ctx, repo_name: str = None, private: str = "true"):
    """Switch to or create a repository"""
    start_time = datetime.now()
    
    try:
        if not repo_name:
            # Show current repo
            current = await get_current_repo(ctx.author.id)
            embed = discord.Embed(
                title="Current Repository",
                description=f"**{current}**",
                color=discord.Color.blue()
            )
            
            # Add GitHub link if possible
            try:
                repo = github_client.get_repo(f"{GITHUB_USERNAME}/{current}")
                embed.add_field(name="URL", value=repo.html_url, inline=False)
                embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
                embed.add_field(name="Stars", value=repo.stargazers_count, inline=True)
            except:
                pass
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'repo', "check", True, execution_time=(datetime.now() - start_time).total_seconds())
            return
        
        # Process repo name
        repo_name = sanitize_filename(repo_name)
        is_private = private.lower() in ['true', 'yes', '1', 'private']
        
        try:
            # Try to access existing repo
            repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
            await set_current_repo(ctx.author.id, repo_name)
            await log_repo_action(ctx.author.id, repo_name, 'switch')
            
            embed = discord.Embed(
                title="Repository Switched",
                description=f"Now working in **{repo_name}**",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=repo.html_url, inline=False)
            embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
            embed.add_field(name="Files", value=str(len(list(repo.get_contents("")))), inline=True)
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'repo', f"name={repo_name}, private={private}", True, execution_time=(datetime.now() - start_time).total_seconds())
            
        except GithubException:
            # Create new repository
            embed = discord.Embed(
                title="Create Repository",
                description=f"Repository `{repo_name}` doesn't exist. Create it?",
                color=discord.Color.yellow()
            )
            embed.add_field(name="Visibility", value="Private" if is_private else "Public", inline=True)
            
            message = await ctx.send(embed=embed)
            
            # Add reaction buttons
            await message.add_reaction("✅")
            await message.add_reaction("❌")
            
            # Wait for reaction
            def check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == message.id
            
            try:
                reaction, user = await bot.wait_for('reaction_add', timeout=30.0, check=check)
                
                if str(reaction.emoji) == "✅":
                    repo, error = await create_github_repo(repo_name, is_private)
                    if repo:
                        await set_current_repo(ctx.author.id, repo_name)
                        await log_repo_action(ctx.author.id, repo_name, 'create_repo')
                        
                        embed = discord.Embed(
                            title="Repository Created",
                            description=f"Created and switched to **{repo_name}**",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="URL", value=repo.html_url, inline=False)
                        embed.add_field(name="Visibility", value="Private" if is_private else "Public", inline=True)
                        
                        await message.edit(embed=embed)
                        await message.clear_reactions()
                        await log_command(ctx.author.id, 'repo', f"name={repo_name}, private={private}", True, execution_time=(datetime.now() - start_time).total_seconds())
                    else:
                        await ctx.send(f"Error creating repository: {error}")
                        await log_command(ctx.author.id, 'repo', f"name={repo_name}, private={private}", False, error, execution_time=(datetime.now() - start_time).total_seconds())
                else:
                    await message.edit(content="Repository creation cancelled.", embed=None)
                    await message.clear_reactions()
                    
            except asyncio.TimeoutError:
                await message.edit(content="Repository creation timed out.", embed=None)
                await message.clear_reactions()
                
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'repo', f"name={repo_name}, private={private}", False, str(e), execution_time=(datetime.now() - start_time).total_seconds())

@bot.command(name='create')
async def prefix_create(ctx, filename: str, *, content: str):
    """Create a new file"""
    start_time = datetime.now()
    
    try:
        repo_name = await get_current_repo(ctx.author.id)
        filename = sanitize_filename(filename)
        
        if len(content) > 10000:  # 10KB limit
            await ctx.send("File content too large (max 10KB)")
            await log_command(ctx.author.id, 'create', f"filename={filename}, size={len(content)}", False, "File too large", execution_time=(datetime.now() - start_time).total_seconds())
            return
        
        if await file_exists(repo_name, filename):
            embed = discord.Embed(
                title="File Exists",
                description=f"File `{filename}` already exists in `{repo_name}`",
                color=discord.Color.yellow()
            )
            embed.add_field(name="Suggestion", value="Use `--edit` to modify the file", inline=False)
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'create', f"filename={filename}, size={len(content)}", False, "File exists", execution_time=(datetime.now() - start_time).total_seconds())
            return
        
        status, response = await github_api_request(
            "PUT",
            f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
            {
                "message": f"Create {filename} via GitCord",
                "content": content.encode("utf-8").hex(),
                "branch": "main"
            }
        )
        
        if status == 201:
            repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
            await log_repo_action(ctx.author.id, repo_name, 'create_file', filename, f"Create {filename}")
            
            embed = discord.Embed(
                title="File Created",
                description=f"Created `{filename}` in `{repo_name}`",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=f"[View on GitHub]({repo_url})", inline=False)
            
            # Show preview for small files
            if len(content) <= 500:
                embed.add_field(name="Preview", value=f"```\n{content}\n```", inline=False)
            elif len(content) <= 1000:
                embed.add_field(name="Preview", value=f"```\n{content[:500]}...\n```", inline=False)
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'create', f"filename={filename}, size={len(content)}", True, execution_time=(datetime.now() - start_time).total_seconds())
        else:
            error_msg = response.get('message', 'Unknown error')
            await ctx.send(f"GitHub API Error: {error_msg}")
            await log_command(ctx.author.id, 'create', f"filename={filename}, size={len(content)}", False, error_msg, execution_time=(datetime.now() - start_time).total_seconds())
            
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'create', f"filename={filename}, size={len(content)}", False, str(e), execution_time=(datetime.now() - start_time).total_seconds())

@bot.command(name='edit')
async def prefix_edit(ctx, filename: str, *, content: str):
    """Edit an existing file"""
    start_time = datetime.now()
    
    try:
        repo_name = await get_current_repo(ctx.author.id)
        filename = sanitize_filename(filename)
        
        if len(content) > 10000:
            await ctx.send("File content too large (max 10KB)")
            await log_command(ctx.author.id, 'edit', f"filename={filename}, size={len(content)}", False, "File too large", execution_time=(datetime.now() - start_time).total_seconds())
            return
        
        # Get file SHA first
        status, file_data = await github_api_request(
            "GET",
            f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
        )
        
        if status != 200:
            await ctx.send(f"File `{filename}` not found in `{repo_name}`")
            await log_command(ctx.author.id, 'edit', f"filename={filename}, size={len(content)}", False, "File not found", execution_time=(datetime.now() - start_time).total_seconds())
            return
        
        sha = file_data['sha']
        
        # Update file
        status, response = await github_api_request(
            "PUT",
            f"/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}",
            {
                "message": f"Update {filename} via GitCord",
                "content": content.encode("utf-8").hex(),
                "sha": sha,
                "branch": "main"
            }
        )
        
        if status == 200:
            repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
            await log_repo_action(ctx.author.id, repo_name, 'edit_file', filename, f"Update {filename}")
            
            embed = discord.Embed(
                title="File Updated",
                description=f"Updated `{filename}` in `{repo_name}`",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=f"[View on GitHub]({repo_url})", inline=False)
            
            # Show diff preview (simplified)
            old_content = bytes.fromhex(file_data['content']).decode('utf-8')
            if old_content != content:
                changes = f"✓ Content changed ({len(old_content)} → {len(content)} chars)"
                embed.add_field(name="Changes", value=changes, inline=False)
            
            await ctx.send(embed=embed)
            await log_command(ctx.author.id, 'edit', f"filename={filename}, size={len(content)}", True, execution_time=(datetime.now() - start_time).total_seconds())
        else:
            error_msg = response.get('message', 'Unknown error')
            await ctx.send(f"GitHub API Error: {error_msg}")
            await log_command(ctx.author.id, 'edit', f"filename={filename}, size={len(content)}", False, error_msg, execution_time=(datetime.now() - start_time).total_seconds())
            
    except Exception as e:
        await ctx.send(f"Error: {str(e)}")
        await log_command(ctx.author.id, 'edit', f"filename={filename}, size={len(content)}", False, str(e), execution_time=(datetime.now() - start_time).total_seconds())

# ========== NEW ADVANCED COMMANDS ==========

@bot.command(name='template')
async def prefix_template(ctx, action: str = None, template_name: str = None, *, content: str = None):
    """Manage file templates"""
    if action == "create" and template_name and content:
        # Save template
        await db.execute(
            'INSERT INTO file_templates (user_id, template_name, content_template) VALUES ($1, $2, $3) ON CONFLICT (user_id, template_name) DO UPDATE SET content_template = $3',
            ctx.author.id, template_name, content
        )
        await ctx.send(f"Template `{template_name}` saved")
        
    elif action == "list":
        # List templates
        templates = await db.fetch('SELECT template_name FROM file_templates WHERE user_id = $1', ctx.author.id)
        if templates:
            template_list = "\n".join([f"• {t[0]}" for t in templates])
            await ctx.send(f"Your templates:\n{template_list}")
        else:
            await ctx.send("No templates saved")
            
    elif action == "use" and template_name:
        # Use template
        row = await db.fetchrow('SELECT content_template FROM file_templates WHERE user_id = $1 AND template_name = $2', ctx.author.id, template_name)
        if row:
            await ctx.send(f"Template `{template_name}`:\n```\n{row[0]}\n```")
        else:
            await ctx.send(f"Template `{template_name}` not found")
            
    elif action == "delete" and template_name:
        # Delete template
        await db.execute('DELETE FROM file_templates WHERE user_id = $1 AND template_name = $2', ctx.author.id, template_name)
        await ctx.send(f"Template `{template_name}` deleted")
        
    else:
        await ctx.send("Usage: `--template create <name> <content>` | `--template list` | `--template use <name>` | `--template delete <name>`")

@bot.command(name='stats')
async def prefix_stats(ctx, user: discord.Member = None):
    """Show user statistics"""
    target_user = user or ctx.author
    stats = await get_user_stats(target_user.id)
    
    embed = discord.Embed(
        title=f"Statistics for {target_user.display_name}",
        color=discord.Color.purple()
    )
    
    embed.add_field(name="Total Commands", value=stats['total_commands'], inline=True)
    embed.add_field(name="Success Rate", value=f"{(stats['successful_commands']/stats['total_commands']*100):.1f}%" if stats['total_commands'] > 0 else "N/A", inline=True)
    embed.add_field(name="Files Created", value=stats['files_created'], inline=True)
    embed.add_field(name="Files Edited", value=stats['files_edited'], inline=True)
    embed.add_field(name="Repos Created", value=stats['repos_created'], inline=True)
    
    if stats['last_active']:
        last_active = stats['last_active'].strftime("%Y-%m-%d %H:%M")
        embed.add_field(name="Last Active", value=last_active, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='history')
async def prefix_history(ctx, limit: int = 10):
    """Show your activity history"""
    rows = await db.fetch(
        'SELECT repo_name, action, filename, created_at FROM repository_history WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2',
        ctx.author.id, limit
    )
    
    if not rows:
        await ctx.send("No activity history found")
        return
    
    history_text = ""
    for i, row in enumerate(rows, 1):
        time = row['created_at'].strftime("%m/%d %H:%M")
        action_icon = {
            'create_repo': '📁',
            'switch': '🔄',
            'create_file': '📄',
            'edit_file': '✏️'
        }.get(row['action'], '📝')
        
        filename = f" - {row['filename']}" if row['filename'] else ""
        history_text += f"{i}. {time} {action_icon} {row['action']} - {row['repo_name']}{filename}\n"
    
    embed = discord.Embed(
        title="Recent Activity",
        description=history_text,
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

@bot.command(name='search')
async def prefix_search(ctx, *, query: str):
    """Search in current repository files"""
    repo_name = await get_current_repo(ctx.author.id)
    
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        contents = repo.get_contents("")
        
        results = []
        for content in contents:
            if content.type == "file" and query.lower() in content.name.lower():
                results.append(content.name)
        
        if results:
            await ctx.send(f"Found {len(results)} files matching '{query}':\n```\n" + "\n".join(results) + "\n```")
        else:
            await ctx.send(f"No files found matching '{query}'")
    except Exception as e:
        await ctx.send(f"Error searching: {str(e)}")

@bot.command(name='backup')
async def prefix_backup(ctx):
    """Create a backup of current repository state"""
    repo_name = await get_current_repo(ctx.author.id)
    
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        contents = repo.get_contents("")
        
        backup_data = []
        for content in contents:
            if content.type == "file":
                backup_data.append(f"{content.name}: {content.size} bytes")
        
        backup_text = f"Backup of {repo_name} ({len(backup_data)} files):\n```\n" + "\n".join(backup_data) + "\n```"
        
        # Save to a file in Discord (simulated)
        await ctx.send(f"Backup created for `{repo_name}`")
        await ctx.send(file=discord.File(
            fp=discord.utils.BytesIO(backup_text.encode()),
            filename=f"{repo_name}_backup.txt"
        ))
        
        await log_repo_action(ctx.author.id, repo_name, 'backup')
    except Exception as e:
        await ctx.send(f"Error creating backup: {str(e)}")

# ========== ADMIN COMMANDS ==========

@bot.command(name='restart')
@commands.is_owner()
async def restart_bot(ctx):
    """Restart the bot (owner only)"""
    await ctx.send("🔄 Restarting bot...")
    print("Bot restart initiated by owner")
    os.execv(sys.executable, ['python'] + sys.argv)

@bot.command(name='status')
@commands.is_owner()
async def bot_status(ctx):
    """Show bot status (owner only)"""
    embed = discord.Embed(
        title="Bot Status",
        color=discord.Color.gold()
    )
    
    # Bot info
    embed.add_field(name="Uptime", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="Users", value=len(bot.users), inline=True)
    
    # Database info
    if db.use_sqlite:
        embed.add_field(name="Database", value="SQLite", inline=True)
    else:
        embed.add_field(name="Database", value="PostgreSQL", inline=True)
    
    # GitHub status
    valid, gh_msg = validate_github_access()
    embed.add_field(name="GitHub", value="Connected" if valid else "Disconnected", inline=True)
    
    # Memory usage
    import psutil
    process = psutil.Process()
    memory_mb = process.memory_info().rss / 1024 / 1024
    embed.add_field(name="Memory", value=f"{memory_mb:.1f} MB", inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='cleanup')
@commands.is_owner()
async def cleanup_database(ctx, days: int = 30):
    """Clean up old database entries (owner only)"""
    deleted = await db.fetchval(
        'DELETE FROM command_logs WHERE created_at < NOW() - INTERVAL \'$1 days\' RETURNING COUNT(*)',
        days
    )
    await ctx.send(f"Cleaned up {deleted} old log entries (older than {days} days)")

# ========== SLASH COMMANDS ==========

@tree.command(name="create", description="Create a new file in GitHub")
@app_commands.describe(
    filename="Name of the file",
    content="Content of the file"
)
async def slash_create(interaction: discord.Interaction, filename: str, content: str):
    """Slash command version of create"""
    await interaction.response.defer()
    
    # Create a fake context for the prefix command
    class FakeContext:
        def __init__(self, interaction):
            self.author = interaction.user
            self.send = interaction.followup.send
            self.message = type('obj', (object,), {'content': ''})()
    
    ctx = FakeContext(interaction)
    await prefix_create(ctx, filename, content=content)

# Add other slash commands similarly...

# ========== HELP COMMAND ==========

@bot.command(name='help')
async def show_help(ctx, command: str = None):
    """Show help information"""
    if command:
        # Show specific command help
        help_texts = {
            'repo': "Switch to or create a repository\nUsage: `--repo <name> [private=true/false]`",
            'create': "Create a new file\nUsage: `--create <filename> <content>`",
            'edit': "Edit an existing file\nUsage: `--edit <filename> <content>`",
            'view': "View a file's content\nUsage: `--view <filename>`",
            'list': "List files in current repository\nUsage: `--list`",
            'template': "Manage file templates\nUsage: `--template create|list|use|delete <name> [content]`",
            'stats': "Show your statistics\nUsage: `--stats [@user]`",
            'history': "Show your activity history\nUsage: `--history [limit]`",
            'search': "Search in repository\nUsage: `--search <query>`",
            'backup': "Create repository backup\nUsage: `--backup`",
            'restart': "Restart bot (owner only)\nUsage: `--restart`",
            'status': "Show bot status (owner only)\nUsage: `--status`",
        }
        
        if command in help_texts:
            await ctx.send(f"**{command}**\n{help_texts[command]}")
        else:
            await ctx.send(f"Command `{command}` not found. Use `--help` for all commands.")
    else:
        # Show all commands
        embed = discord.Embed(
            title="GitCord Help",
            description="A Discord bot for GitHub management",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Repository Management",
            value="`--repo` - Switch/create repo\n`--list` - List files\n`--search` - Search files",
            inline=False
        )
        
        embed.add_field(
            name="File Operations",
            value="`--create` - Create file\n`--edit` - Edit file\n`--view` - View file\n`--backup` - Create backup",
            inline=False
        )
        
        embed.add_field(
            name="Productivity",
            value="`--template` - File templates\n`--history` - Activity history\n`--stats` - User statistics",
            inline=False
        )
        
        embed.add_field(
            name="Administration",
            value="`--restart` - Restart bot (owner)\n`--status` - Bot status (owner)\n`--cleanup` - Clean DB (owner)",
            inline=False
        )
        
        embed.add_field(
            name="Usage",
            value="Use `--help <command>` for specific help\nAll commands also available as `/` slash commands",
            inline=False
        )
        
        embed.set_footer(text=f"Prefix: {PREFIX} | Connected to GitHub as {GITHUB_USERNAME}")
        
        await ctx.send(embed=embed)

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
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
        await ctx.send(f"An error occurred: {str(error)[:100]}...")
        await log_command(ctx.author.id, ctx.command.name if ctx.command else "unknown", str(ctx.message.content), False, str(error))

# ========== START BOT ==========

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print(" Error: DISCORD_TOKEN environment variable not set")
    elif not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN environment variable not set")
    elif not GITHUB_USERNAME:
        print("Error: GITHUB_USERNAME environment variable not set")
    else:
        print("Starting GitCord Bot...")
        bot.run(DISCORD_TOKEN)
